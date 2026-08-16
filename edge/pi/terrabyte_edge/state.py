"""Live gateway state, and the snapshot file the display reads.

The bridge runs as a hardened systemd service; the display runs as a separate
process. They cannot share memory, so the bridge publishes a JSON snapshot and
the display polls it. Two layers, two different problems:

* in-process: reader threads and the uploader mutate one ``GatewayState``
  behind a single lock, and ``snapshot()`` hands out a frozen copy.
* cross-process: the snapshot is written to a temp file and ``os.replace``d
  into place, which is atomic on the same filesystem, so a reader can never
  observe a half-written file and needs no lock of its own.

The snapshot lives under ``/run`` rather than ``/var/lib`` deliberately.
systemd deletes ``RuntimeDirectory`` when the unit stops, so a stale file
cannot outlive the bridge and fool the display into presenting dead readings
as live ones.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import tempfile
import time
from threading import Lock
from typing import Literal

SNAPSHOT_SCHEMA = 1

# Rendered "what just happened" list. Bounded because this is the one
# unbounded-growth risk in a process that runs for weeks.
MAX_EVENTS = 20

LinkState = Literal["up", "down", "never_seen"]
PortFault = Literal["duplicate_node", "unknown_node"]


@dataclass(frozen=True)
class PortSnapshot:
    path: str
    node_id: str | None
    link: LinkState
    last_frame_epoch: float | None
    frames: int
    errors: int
    fault: PortFault | None
    fault_detail: str | None
    measurements: dict[str, float]


@dataclass(frozen=True)
class GatewaySnapshot:
    schema: int
    generated_at_epoch: float
    started_at_epoch: float
    gateway_id: str
    claim_code: str
    transport: str
    transport_connected: bool
    transport_last_error: str | None
    last_delivery_epoch: float | None
    outbox_pending: int
    outbox_dead: int
    ports: tuple[PortSnapshot, ...]
    events: tuple[tuple[float, str, str], ...]

    def to_json_dict(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "generated_at_epoch": self.generated_at_epoch,
            "started_at_epoch": self.started_at_epoch,
            "gateway_id": self.gateway_id,
            # Not a secret at this trust boundary: it is displayed on a monitor
            # to whoever is standing in front of the box. Putting it here keeps
            # the display from having to read root-owned /etc/terrabyte-edge.env.
            "claim_code": self.claim_code,
            "transport": {
                "kind": self.transport,
                "connected": self.transport_connected,
                "last_error": self.transport_last_error,
                "last_delivery_epoch": self.last_delivery_epoch,
            },
            "outbox": {"pending": self.outbox_pending, "dead": self.outbox_dead},
            "ports": [
                {
                    "path": port.path,
                    "node_id": port.node_id,
                    "link": port.link,
                    "last_frame_epoch": port.last_frame_epoch,
                    "frames": port.frames,
                    "errors": port.errors,
                    "fault": port.fault,
                    "fault_detail": port.fault_detail,
                    "measurements": port.measurements,
                }
                for port in self.ports
            ],
            "events": [
                {"at_epoch": at, "level": level, "text": text}
                for at, level, text in self.events
            ],
        }


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
    """Mutable live state. Every public method is safe to call from any thread.

    One coarse lock rather than per-port locks: contention is a handful of
    writes per minute against a 1 Hz reader, and a single lock is the version
    that stays obviously correct when someone adds a field later.
    """

    def __init__(
        self,
        *,
        gateway_id: str,
        claim_code: str,
        transport: str,
        ports: tuple[str, ...],
        clock=time.time,
    ) -> None:
        self._lock = Lock()
        self._clock = clock
        self._gateway_id = gateway_id
        self._claim_code = claim_code
        self._transport = transport
        self._started_at = clock()
        self._ports: dict[str, _PortRecord] = {
            path: _PortRecord(path=path) for path in ports
        }
        self._transport_connected = False
        self._transport_last_error: str | None = None
        self._last_delivery_epoch: float | None = None
        self._outbox_pending = 0
        self._outbox_dead = 0
        self._events: deque[tuple[float, str, str]] = deque(maxlen=MAX_EVENTS)

    # -- writers ---------------------------------------------------------

    def record_link(self, port: str, *, up: bool, error: str | None = None) -> None:
        with self._lock:
            record = self._ports.setdefault(port, _PortRecord(path=port))
            record.link = "up" if up else "down"
            if error:
                record.errors += 1

    def record_frame(self, port: str, *, node_id: str, measurements: dict[str, float]) -> None:
        with self._lock:
            record = self._ports.setdefault(port, _PortRecord(path=port))
            duplicate = self._find_other_port_with_node(port, node_id)
            if duplicate is not None:
                # Two Arduinos flashed with the same TB_NODE_ID. The readings
                # would interleave into one pot's history and look plausible,
                # so this has to be visible rather than merely logged.
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
            record.measurements = dict(measurements)

    def record_announcement(self, port: str, node_id: str) -> None:
        """A hello/status frame naming its node, before any reading arrives."""

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

    def record_delivery(self) -> None:
        with self._lock:
            self._last_delivery_epoch = self._clock()
            self._transport_connected = True
            self._transport_last_error = None

    def record_outbox(self, *, pending: int, dead: int) -> None:
        with self._lock:
            self._outbox_pending = pending
            self._outbox_dead = dead

    def add_event(self, level: str, text: str) -> None:
        with self._lock:
            self._events.append((self._clock(), level, text))

    # -- reader ----------------------------------------------------------

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
                    # Copied, not referenced: the caller must not be able to
                    # observe a later mutation through the snapshot it holds.
                    measurements=dict(record.measurements),
                )
                for record in self._ports.values()
            )
            return GatewaySnapshot(
                schema=SNAPSHOT_SCHEMA,
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


def write_snapshot(path: Path, snapshot: GatewaySnapshot) -> None:
    """Publish a snapshot atomically.

    The temp file is created in the destination directory on purpose:
    ``os.replace`` is only atomic within one filesystem, and ``/run`` is a
    separate tmpfs from ``/tmp`` (which is a private mount under
    ``PrivateTmp=true`` anyway).
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=str(path.parent),
        prefix=path.name + ".",
        suffix=".tmp",
        delete=False,
    )
    try:
        with handle:
            json.dump(snapshot.to_json_dict(), handle, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(handle.name, path)
    except BaseException:
        try:
            os.unlink(handle.name)
        except OSError:
            pass
        raise


def read_snapshot(path: Path) -> dict[str, object] | None:
    """Read a published snapshot, or None when it is absent or unreadable.

    Returning None rather than raising is deliberate: a missing snapshot is
    the expected state while the bridge is starting or stopped, and the
    display renders that as a diagnostic instead of crashing.
    """

    try:
        with path.open(encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or payload.get("schema") != SNAPSHOT_SCHEMA:
        return None
    return payload
