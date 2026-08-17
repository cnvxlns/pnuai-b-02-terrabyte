"""An Arduino that exists only in software.

Speaks the serial side of the command contract — short keys, because the real
ATmega328P has 2 KB of SRAM — so the whole
``backend -> MQTT -> pi -> serial -> ack -> MQTT -> backend`` loop can be
exercised before a pump, a relay, or the firmware exist. No water moves and the
loop is still fully covered: every translation, every state transition, and
every budget effect is software.

It is a **test double and only that.** There is deliberately no environment
variable and no CLI flag that turns it on: a gateway that could be configured
into loopback would, on one typo, publish invented sensor readings that look
exactly like measurements. Callers construct it explicitly instead::

    reader = SerialLineReader(
        port="loopback-0",
        baudrate=115200,
        timeout_seconds=0.1,
        reconnect_seconds=1.0,
        max_line_bytes=512,
        factory=LoopbackArduino(node_id="terrabyte-node-01").as_factory(),
    )

What it models, and why only this much:

* ``accepted`` then ``completed`` as two separate lines, so a consumer really
  does observe the intermediate state rather than collapsing straight to the
  terminal one.
* duplicate command ids, via the same 8-entry ring buffer the firmware
  specifies. This one is not optional: QoS 1 permits a duplicate at both hops
  and the ring is the last line of defence, so the path has to be reachable
  without hardware.
* the absolute maximum run time (G1). A command asking for longer is clamped
  and reports ``stop:"max_runtime"``, which is the single hard safety number in
  the contract.

Everything else — real cooldown timing, the deadman watchdog firing, sensor
faults — is left to ``respond``, an override that returns whatever ack sequence
a test wants. Modelling firmware timing here would produce a second
implementation of the interlocks that could drift from the first, and a test
double that disagrees with the firmware is worse than no double at all.
"""

from __future__ import annotations

from collections import deque
import json
import logging
from threading import Condition
from typing import Callable, Iterable


LOGGER = logging.getLogger(__name__)

# Mirrors TB_PUMP_ABS_MAX_MS in the firmware. Duplicated rather than imported
# because it lives in C++; if the two ever disagree the firmware wins and this
# constant is the one that is wrong.
ABS_MAX_RUN_MS = 30_000

# The firmware remembers this many command ids. Beyond it, an old id is
# forgotten and would execute a second time.
COMMAND_ID_RING = 8

Responder = Callable[[dict], Iterable[dict]]


class LoopbackArduino:
    """A ``SerialHandle`` that answers commands instead of running a pump.

    Thread-safe: the ingest thread calls ``readline`` while the relay thread
    calls ``write``, exactly as they will against a real port.
    """

    def __init__(
        self,
        *,
        node_id: str,
        protocol_version: int = 1,
        respond: Responder | None = None,
        announce_hello: bool = True,
        read_timeout: float = 0.0,
    ) -> None:
        self.node_id = node_id
        self.protocol_version = protocol_version
        self.respond = respond
        # How long an empty read blocks, mirroring pyserial's timeout. It is not
        # cosmetic: the bridge's read loop treats an empty read as "nothing this
        # tick" and comes straight back, so a handle that returns instantly
        # turns that loop into a spin at 100% of a core. as_factory() overwrites
        # this with the reader's own timeout; the 0.0 default keeps direct unit
        # tests instant.
        self.read_timeout = read_timeout
        self._condition = Condition()
        self._outbound: deque[bytes] = deque()
        self._write_buffer = bytearray()
        self._seen_command_ids: deque[str] = deque(maxlen=COMMAND_ID_RING)
        self._closed = False
        # Everything the double was told, in order, for assertions.
        self.received: list[dict] = []
        self.unparsable: list[bytes] = []
        self.sequence = 0
        if announce_hello:
            # Real firmware announces itself on boot, and the display depends on
            # it to name a port before the first reading arrives.
            self.queue(
                {
                    "message_type": "hello",
                    "protocol_version": protocol_version,
                    "node_id": node_id,
                }
            )

    # -- construction ----------------------------------------------------

    def as_factory(self) -> Callable[..., "LoopbackArduino"]:
        """A ``SerialFactory`` handing out this same instance on every reconnect.

        One instance, not one per connect, so the ring buffer and the received
        log survive a simulated cable pull — which is the point of simulating
        one.
        """

        def factory(**kwargs: object) -> "LoopbackArduino":
            timeout = kwargs.get("timeout")
            if isinstance(timeout, (int, float)):
                self.read_timeout = float(timeout)
            with self._condition:
                self._closed = False
            return self

        return factory

    # -- inbound side (what the bridge reads) ----------------------------

    def queue(self, message: dict) -> None:
        """Make one JSON Lines frame available to the next ``readline``."""

        self.queue_raw(json.dumps(message, separators=(",", ":")).encode() + b"\n")

    def queue_raw(self, line: bytes) -> None:
        with self._condition:
            self._outbound.append(line)
            self._condition.notify_all()

    def queue_telemetry(self, **overrides: object) -> dict:
        """One plausible reading. Values are unremarkable on purpose.

        A test that cares about a specific value passes it; a test that only
        needs traffic gets numbers that pass validation without sitting on any
        threshold, so it cannot accidentally depend on a dry-gate boundary.
        """

        self.sequence += 1
        message: dict = {
            "message_type": "telemetry",
            "protocol_version": self.protocol_version,
            "node_id": self.node_id,
            "sequence": self.sequence,
            "uptime_ms": self.sequence * 1000,
            "air_temperature_c": 24.0,
            "relative_humidity_pct": 55.0,
            "ppfd_umol_m2_s": 300.0,
            "soil_moisture_pct": 30.0,
        }
        message.update(overrides)
        self.queue(message)
        return message

    # -- SerialHandle ----------------------------------------------------

    def readline(self, size: int = 0) -> bytes:
        """One queued frame, or ``b""`` after ``read_timeout`` with none.

        Blocks like a real port rather than returning instantly. The bridge
        cannot tell the difference in what it receives, but it can in what it
        costs: an instant empty read makes its ingest loop spin.
        """

        with self._condition:
            if not self._outbound and self.read_timeout > 0:
                self._condition.wait(self.read_timeout)
            if not self._outbound:
                return b""
            return self._outbound.popleft()

    def write(self, data: bytes) -> int:
        """Accept command bytes, answering each complete line.

        Bytes are buffered until a newline because that is how a serial link
        behaves; a caller is entitled to write a frame in pieces.
        """

        with self._condition:
            if self._closed:
                raise OSError("loopback arduino is closed")
            self._write_buffer.extend(data)
            frames = []
            while b"\n" in self._write_buffer:
                raw, _, rest = bytes(self._write_buffer).partition(b"\n")
                self._write_buffer = bytearray(rest)
                frames.append(raw)
        for raw in frames:
            self._handle_frame(raw)
        return len(data)

    def flush(self) -> None:
        return None

    def close(self) -> None:
        with self._condition:
            self._closed = True

    # -- command handling ------------------------------------------------

    def _handle_frame(self, raw: bytes) -> None:
        if not raw.strip():
            return
        try:
            message = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            self.unparsable.append(raw)
            LOGGER.warning("loopback received an unparsable frame")
            return
        if not isinstance(message, dict):
            self.unparsable.append(raw)
            return

        self.received.append(message)
        for ack in self._acks_for(message):
            self.queue(ack)

    def _acks_for(self, message: dict) -> list[dict]:
        if self.respond is not None:
            return list(self.respond(message))

        kind = message.get("t")
        if kind == "ka":
            # The deadman tick. Answering it would invent traffic the contract
            # does not define; silence is the correct response.
            return []
        if kind != "cmd":
            LOGGER.warning("loopback ignoring unknown frame t=%s", kind)
            return []

        command_id = message.get("id")
        if not isinstance(command_id, str) or not command_id:
            # No id means no ack can be correlated, so there is nothing to
            # answer to. Dropping it is what the firmware would do.
            LOGGER.warning("loopback ignoring a command with no id")
            return []
        if command_id in self._seen_command_ids:
            return [{"t": "ack", "id": command_id, "ph": "rejected", "r": "duplicate"}]
        self._seen_command_ids.append(command_id)

        requested_ms = message.get("ms")
        if not isinstance(requested_ms, int) or requested_ms <= 0:
            return [
                {"t": "ack", "id": command_id, "ph": "rejected", "r": "bad_request"}
            ]

        # G1: the firmware ignores an over-long ms rather than trusting the
        # sender. Reporting the clamped value, not the requested one, is what
        # lets the backend record what actually happened.
        run_ms = min(requested_ms, ABS_MAX_RUN_MS)
        stop = "max_runtime" if run_ms < requested_ms else "volume_reached"
        return [
            {"t": "ack", "id": command_id, "ph": "accepted"},
            {"t": "ack", "id": command_id, "ph": "completed", "ms": run_ms, "stop": stop},
        ]
