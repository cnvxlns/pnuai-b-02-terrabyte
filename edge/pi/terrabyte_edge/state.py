"""Thread-safe bridge status and atomic JSON snapshot I/O."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import sys
import tempfile
from threading import Lock
import time
from typing import Literal


SNAPSHOT_SCHEMA = 1


def _default_snapshot_path() -> Path:
    """Where the status snapshot lives, per platform.

    The snapshot is ephemeral runtime state: a reader wants to know what the
    gateway is doing *now*, and a stale file left over from a previous boot is
    worse than no file. On Linux ``/run`` is exactly that — a tmpfs cleared on
    every boot — so the Orange Pi keeps it.

    macOS has no ``/run`` and its root filesystem is read-only, so that path
    raises ``OSError: Read-only file system`` before the service finishes
    starting. That is not hypothetical: the gateway has had to run on a Mac
    while the Orange Pi was out of service, and the hard-coded path crash-looped
    it. The per-user temporary directory is the closest portable equivalent —
    ephemeral, writable without privileges, and cleaned by the OS.

    ``TB_SNAPSHOT_PATH`` overrides both, for a deployment that wants the file
    somewhere specific. ``--snapshot-path`` on the CLI still wins over this.
    """

    override = os.environ.get("TB_SNAPSHOT_PATH", "").strip()
    if override:
        return Path(override)
    if sys.platform.startswith("linux"):
        return Path("/run/terrabyte-edge/status.json")
    return Path(tempfile.gettempdir()) / "terrabyte-edge" / "status.json"


DEFAULT_SNAPSHOT_PATH = _default_snapshot_path()
MAX_EVENTS = 20

LinkState = Literal["up", "down", "never_seen"]
PortFault = Literal["duplicate_node", "unknown_node"]


class PortSnapshot(dict[str, object]):
    """JSON-compatible port state with attributes for in-process consumers."""

    def __init__(
        self,
        *,
        path: str,
        node_id: str | None,
        link: LinkState,
        last_frame_epoch: float | None,
        frames: int,
        errors: int,
        fault: PortFault | None,
        fault_detail: str | None,
        measurements: dict[str, float],
    ) -> None:
        super().__init__(
            path=path,
            node_id=node_id,
            link=link,
            last_frame_epoch=last_frame_epoch,
            frames=frames,
            errors=errors,
            fault=fault,
            fault_detail=fault_detail,
            measurements=measurements,
        )

    @property
    def path(self) -> str:
        return str(self["path"])

    @property
    def node_id(self) -> str | None:
        value = self["node_id"]
        return value if isinstance(value, str) else None

    @property
    def errors(self) -> int:
        return int(self["errors"])


class GatewaySnapshot(dict[str, object]):
    """A dict on the snapshot wire and an object inside the relay.

    The dashboard contract on develop already consumes dictionaries. The relay
    needs stable attribute access while holding a snapshot. A dict subclass
    preserves both without teaching either side a second representation.
    """

    def __init__(
        self,
        *,
        generated_at_epoch: float,
        started_at_epoch: float,
        gateway_id: str,
        claim_code: str,
        transport: str,
        transport_connected: bool,
        transport_last_error: str | None,
        last_delivery_epoch: float | None,
        outbox_pending: int,
        outbox_dead: int,
        ports: tuple[PortSnapshot, ...],
        events: tuple[tuple[float, str, str], ...],
    ) -> None:
        super().__init__(
            schema=SNAPSHOT_SCHEMA,
            generated_at_epoch=generated_at_epoch,
            started_at_epoch=started_at_epoch,
            gateway_id=gateway_id,
            claim_code=claim_code,
            transport={
                "kind": transport,
                "connected": transport_connected,
                "last_error": transport_last_error,
                "last_delivery_epoch": last_delivery_epoch,
            },
            outbox={"pending": outbox_pending, "dead": outbox_dead},
            ports=list(ports),
            events=[
                {"at_epoch": at, "level": level, "text": text}
                for at, level, text in events
            ],
        )

    @property
    def ports(self) -> tuple[PortSnapshot, ...]:
        return tuple(self["ports"])  # type: ignore[arg-type]

@dataclass
class _PortRecord:
    path: str
    node_id: str | None = None
    link: LinkState = "never_seen"
    last_frame_epoch: float | None = None
    frames: int = 0
    errors: int = 0
    fault: PortFault | None = None
    fault_detail: str | None = None
    measurements: dict[str, float] = field(default_factory=dict)


class GatewayState:
    """Mutable live state shared by serial, relay, uploader, and dashboard."""

    def __init__(
        self,
        *,
        gateway_id: str,
        port: str | None = None,
        claim_code: str = "",
        transport: str = "",
        ports: tuple[str, ...] | None = None,
        clock=time.time,
    ) -> None:
        if ports is None:
            ports = () if port is None else (port,)
        self._lock = Lock()
        self._clock = clock
        self._gateway_id = gateway_id
        # 기기 등록용 6자리 코드. 미프로비저닝 게이트웨이는 빈 값이고,
        # 그때는 화면이 render.py 의 자리표시자를 대신 보여준다.
        self._claim_code = claim_code
        self._transport = transport
        self._started_at = clock()
        self._ports: dict[str, _PortRecord] = {
            path: _PortRecord(path=path) for path in ports
        }
        self._default_port = ports[0] if len(ports) == 1 else None
        self._transport_connected = False
        self._transport_last_error: str | None = None
        self._last_delivery_epoch: float | None = None
        self._outbox_pending = 0
        self._outbox_dead = 0
        self._events: deque[tuple[float, str, str]] = deque(maxlen=MAX_EVENTS)

    def record_link(self, port: str, *, up: bool, error: str | None = None) -> None:
        with self._lock:
            record = self._ports.setdefault(port, _PortRecord(path=port))
            record.link = "up" if up else "down"
            if error:
                record.errors += 1

    def record_frame(
        self,
        port_or_event: str | object,
        *,
        node_id: str | None = None,
        measurements: dict[str, float] | None = None,
    ) -> None:
        """Record either develop's event form or the relay's port-aware form."""

        if isinstance(port_or_event, str):
            port = port_or_event
            if not node_id:
                raise ValueError("node_id is required for a port frame")
            values = dict(measurements or {})
        else:
            event = port_or_event
            port = self._default_port
            if port is None:
                raise ValueError("an event-only frame requires exactly one port")
            node_id = str(getattr(event, "node_id"))
            pairs = (
                ("air_temperature_c", "air_temperature_c"),
                ("air_humidity_pct", "relative_humidity_pct"),
                ("plant_light_ppfd_umol_m2_s", "ppfd_umol_m2_s"),
                ("soil_temperature_c", "soil_temperature_c"),
                ("soil_moisture_pct", "soil_moisture_pct"),
            )
            values = {
                output: float(value)
                for output, attribute in pairs
                if (value := getattr(event, attribute, None)) is not None
            }

        with self._lock:
            record = self._ports.setdefault(port, _PortRecord(path=port))
            duplicate = self._find_other_port_with_node(port, node_id)
            if duplicate is not None:
                # Two boards with one node id make routing ambiguous. Refuse to
                # update the mapping so the relay can fail closed.
                record.fault = "duplicate_node"
                record.fault_detail = f"{node_id} 이(가) {duplicate} 에서도 보입니다"
                record.errors += 1
                return
            record.node_id = node_id
            record.link = "up"
            record.last_frame_epoch = self._clock()
            record.frames += 1
            record.fault = None
            record.fault_detail = None
            record.measurements = values

    def record_announcement(self, port: str, node_id: str) -> None:
        with self._lock:
            record = self._ports.setdefault(port, _PortRecord(path=port))
            if record.node_id is None:
                record.node_id = node_id
            record.link = "up"

    def record_unknown_node(self, port: str, node_id: str) -> None:
        with self._lock:
            record = self._ports.setdefault(port, _PortRecord(path=port))
            record.fault = "unknown_node"
            record.fault_detail = f"{node_id} 은(는) 허용 목록에 없습니다"
            record.errors += 1

    def record_error(self, port: str) -> None:
        with self._lock:
            self._ports.setdefault(port, _PortRecord(path=port)).errors += 1

    def record_transport(self, *, connected: bool, error: str | None = None) -> None:
        with self._lock:
            self._transport_connected = connected
            self._transport_last_error = error
            if connected:
                self._last_delivery_epoch = self._clock()

    def record_outbox(self, pending: int, dead: int) -> None:
        with self._lock:
            self._outbox_pending = pending
            self._outbox_dead = dead

    def add_event(self, level: str, text: str) -> None:
        with self._lock:
            self._events.append((self._clock(), level, text))

    def snapshot(self) -> GatewaySnapshot:
        with self._lock:
            ports = tuple(
                PortSnapshot(
                    path=record.path,
                    node_id=record.node_id,
                    link=record.link,
                    last_frame_epoch=record.last_frame_epoch,
                    frames=record.frames,
                    errors=record.errors,
                    fault=record.fault,
                    fault_detail=record.fault_detail,
                    measurements=dict(record.measurements),
                )
                for record in self._ports.values()
            )
            return GatewaySnapshot(
                generated_at_epoch=self._clock(),
                started_at_epoch=self._started_at,
                gateway_id=self._gateway_id,
                claim_code=self._claim_code,
                transport=self._transport,
                transport_connected=self._transport_connected,
                transport_last_error=self._transport_last_error,
                last_delivery_epoch=self._last_delivery_epoch,
                outbox_pending=self._outbox_pending,
                outbox_dead=self._outbox_dead,
                ports=ports,
                events=tuple(self._events),
            )

    def _find_other_port_with_node(self, port: str, node_id: str) -> str | None:
        for path, record in self._ports.items():
            if path != port and record.node_id == node_id and record.fault is None:
                return path
        return None


def write_snapshot(path: Path, snapshot: dict[str, object]) -> None:
    """Publish a dashboard snapshot atomically on the destination filesystem."""

    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=path.parent,
        prefix=path.name + ".",
        suffix=".tmp",
        delete=False,
    )
    try:
        with handle:
            json.dump(snapshot, handle, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        # The bridge unit has a restrictive umask, while the display runs as a
        # separate unprivileged user.
        os.chmod(handle.name, 0o644)
        os.replace(handle.name, path)
    except BaseException:
        try:
            os.unlink(handle.name)
        except OSError:
            pass
        raise


def read_snapshot(path: Path) -> dict[str, object] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or payload.get("schema") != SNAPSHOT_SCHEMA:
        return None
    return payload
