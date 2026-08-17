"""Command relay: MQTT ``dn/command`` in, serial ``{"t":"cmd"}`` out, acks back.

The gateway is the **translator between two frozen contracts**
(docs/design/edge_ai_hardening.md): long-key JSON over MQTT on one side, short
keys over serial on the other, because the ATmega328P has 2 KB of SRAM. Nothing
else in the system speaks both, so every mismatch between them has to be
resolved here.

Three responsibilities that must not be moved elsewhere:

1. **TTL is judged here and only here** (D19 / §"TTL 3중 판정"). Spring refuses
   to publish an already-expired command; the Arduino has no RTC and therefore
   cannot compare wall clocks at all, and only ever handles the relative ``ms``.
   That leaves the Pi as the sole layer with both a synchronised clock and the
   command in hand. If this check is wrong, the delayed-bomb failure returns: a
   gateway that was offline for two hours reconnects, receives a queue of
   commands the user asked for long ago, and waters six times in a row.
2. **The reason vocabulary is reconciled here.** See :data:`FIRMWARE_REASONS`.
3. **The deadman tick is sent from here.** The firmware stops the pump if it
   hears nothing from the host for 3 s (G3), which is the last defence when this
   process dies mid-dose — so the ticking has to be driven by *this* module's
   knowledge of what is running, not by a timer that ticks unconditionally.

Threading, which is the part that unit tests cannot catch. ``MqttPublisher``
runs paho's ``loop_start()``, so ``on_message`` executes on paho's network
thread. Judging a command there — a wall-clock comparison, a SQLite dedup claim,
a serial write that blocks on flush — starves the MQTT keepalive and can
deadlock the client. Written inline it passes every unit test, because a test
calls the handler on its own thread, and then dies on real hardware. So:

    paho network thread   offer()          -> bounded queue, returns at once
    command-relay         run()            -> parse, TTL, dedup, serial write
    serial-ingest-N       handle_serial_ack() -> translate ack, queue it
    command-deadman       run_deadman()    -> {"t":"ka"} while a pump runs
    ack-upload            (service.py)     -> drains the ack kind from the outbox
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import logging
import math
from pathlib import Path
import queue
import sqlite3
import threading
import time
from typing import Any, Callable, Iterable, Sequence

from .outbox import KIND_ACK, OutboxFullError
from .protocol import (
    CommandAck,
    ProtocolError,
    epoch_to_iso8601,
    parse_iso8601_utc,
    parse_serial_ack,
)
from .publisher import CommandTransport
from .serial_reader import SerialLineReader
from .state import GatewayState


LOGGER = logging.getLogger(__name__)


# Mirrors TB_PUMP_ABS_MAX_MS in the firmware, and it is a mirror rather than the
# source: the firmware enforces it whatever this file says. Used here only to
# size the deadman window, never to clamp an outgoing command — see
# serial_command_frame for why clamping here would destroy evidence.
PUMP_ABS_MAX_MS = 30_000

# The deadman frame. Content-free on purpose: G3 counts *bytes received*, not
# messages understood, and the firmware answers it with nothing.
DEADMAN_FRAME = b'{"t":"ka"}'

SUPPORTED_ACTUATORS = ("pump",)
# "dose" is the only action in the contract today. Abort is listed as future
# work, so an unknown action is refused rather than guessed at: guessing which
# verb means "run the pump" is the one mistake that moves water by accident.
SUPPORTED_ACTIONS = ("dose",)

TERMINAL_PHASES = ("rejected", "completed", "aborted")


# --- the three reason vocabularies -----------------------------------------
#
# The same concept has a different name at every layer, and one name means two
# different things depending on the layer, so this is where they are reconciled.
#
#   Arduino    lowercase firmware tokens: cooldown, duplicate, volume_reached,
#              watchdog, max_runtime, busy
#   MQTT       eight UPPER_SNAKE values, closed set (§:462)
#   Java       DenyReason, a *different* enum: its COOLDOWN is a pre-publish
#              server gate (gate 4, six hours), whereas the firmware's cooldown
#              is a post-publish refusal by the hardware (ten minutes). Same
#              word, different event, opposite side of the wire. Its
#              SENSOR_INVALID is likewise specifically the soil-probe gate, which
#              is why nothing here maps onto that value.
#
# The rule that keeps this from being brittle: ``phase`` decides state, ``reason``
# is a coarse diagnostic, and the firmware's raw token is preserved verbatim in
# ``stop_cause``. A mapping that loses the raw token loses the diagnosis.
REASON_OK = "OK"
REASON_EXPIRED = "EXPIRED"
REASON_DUPLICATE = "DUPLICATE"
REASON_INTERLOCK_COOLDOWN = "INTERLOCK_COOLDOWN"
REASON_SENSOR_INVALID = "SENSOR_INVALID"
REASON_NODE_OFFLINE = "NODE_OFFLINE"
REASON_ABORT_REQUESTED = "ABORT_REQUESTED"
REASON_WATCHDOG = "WATCHDOG"

MQTT_REASONS = (
    REASON_OK,
    REASON_EXPIRED,
    REASON_DUPLICATE,
    REASON_INTERLOCK_COOLDOWN,
    REASON_SENSOR_INVALID,
    REASON_NODE_OFFLINE,
    REASON_ABORT_REQUESTED,
    REASON_WATCHDOG,
)

FIRMWARE_REASONS: dict[str, str] = {
    # A firmware interlock refused the command.
    "cooldown": REASON_INTERLOCK_COOLDOWN,
    # ``busy`` has no counterpart among the eight. The firmware grew it locally
    # for "a dose is already running", which is a different event from a
    # cooldown, and the MQTT vocabulary is frozen. It is filed under
    # INTERLOCK_COOLDOWN because that is the only member meaning "the hardware
    # refused on its own interlock grounds", and NOT under NODE_OFFLINE, which
    # would claim the Arduino never answered when in fact it did. The
    # distinguishing fact survives regardless: stop_cause carries "busy"
    # verbatim, and the backend decides state from phase alone.
    "busy": REASON_INTERLOCK_COOLDOWN,
    "duplicate": REASON_DUPLICATE,
    "watchdog": REASON_WATCHDOG,
    # Not a fault. G1 stopping a 60 s request at 30 s is the safety limit doing
    # exactly its job, so the outcome is OK and stop_cause says what shortened
    # it. Reporting a failure reason here would make correct behaviour look like
    # a malfunction in the operator's log.
    "max_runtime": REASON_OK,
    "volume_reached": REASON_OK,
}

# Used when the firmware sends a token this table does not know — a newer
# firmware, or a local token like the loopback's ``bad_request``. Chosen per
# phase so an unmapped value can never invent an event that did not happen:
# a rejection stays a refusal, an abort stays a safety cut, a completion stays
# a completion.
PHASE_FALLBACK_REASONS: dict[str, str] = {
    "accepted": REASON_OK,
    "completed": REASON_OK,
    "rejected": REASON_INTERLOCK_COOLDOWN,
    "aborted": REASON_WATCHDOG,
}

# Pi-side refusals. Prefixed so a log reader can tell at a glance that the
# Arduino was never involved — "cooldown" and "pi_link_down" send whoever is
# debugging to opposite ends of the system.
STOP_PI_DUPLICATE = "pi_duplicate"
STOP_PI_EXPIRED = "pi_expired"
STOP_PI_NO_EXPIRY = "pi_no_expires_at"
STOP_PI_BAD_SCHEMA = "pi_bad_schema"
STOP_PI_BAD_ACTUATOR = "pi_bad_actuator"
STOP_PI_BAD_PARAMS = "pi_bad_params"
STOP_PI_UNKNOWN_NODE = "pi_unknown_node"
STOP_PI_AMBIGUOUS_NODE = "pi_ambiguous_node"
STOP_PI_LINK_DOWN = "pi_link_down"
STOP_PI_FRAME_TOO_LONG = "pi_frame_too_long"


def mqtt_reason(phase: str, firmware_token: str | None) -> str:
    """Translate one firmware token into the MQTT vocabulary.

    Total by construction: an unknown token falls back per phase and logs, so a
    firmware that grows a new token degrades into a coarse-but-true reason
    instead of raising inside the ingest thread and dropping the ack.
    """

    fallback = PHASE_FALLBACK_REASONS.get(phase, REASON_OK)
    if firmware_token is None:
        return fallback
    mapped = FIRMWARE_REASONS.get(firmware_token)
    if mapped is None:
        LOGGER.warning(
            "firmware reason token has no MQTT counterpart token=%s phase=%s "
            "falling_back_to=%s",
            firmware_token,
            phase,
            fallback,
        )
        return fallback
    return mapped


class CommandError(ValueError):
    """The payload cannot be correlated to a command at all.

    Distinct from a *rejection*: a rejection is answerable and gets an ack, this
    is not. Without a usable ``command_id`` there is nothing for the backend to
    apply an outcome to, so the only honest response is to drop and log.
    """


def _optional_str(message: dict[str, Any], name: str) -> str | None:
    value = message.get(name)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _optional_int(message: dict[str, Any], name: str) -> int | None:
    """A whole number, or None when absent or unusable.

    ``120.0`` is accepted because a JSON serialiser on the other side may emit a
    float for an integral value; ``120.5`` is not, because a fractional
    millilitre or millisecond means the sender's model disagrees with ours and
    rounding it would hide that.
    """

    value = message.get(name)
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and math.isfinite(value) and value.is_integer():
        return int(value)
    return None


@dataclass(frozen=True)
class CommandRequest:
    """One ``dn/command`` payload, parsed as permissively as correlation allows.

    Only ``command_id`` is required to construct this. Everything else may be
    None, and that is the point: a command whose ``params`` are nonsense still
    has to receive a ``rejected`` ack, or the backend sits on ISSUED until the
    sweep expires it and charges the pot's budget for water that never moved. So
    the envelope is evaluated first and validation failures become rejections
    rather than parse errors.
    """

    command_id: str
    schema_version: int | None = None
    message_type: str | None = None
    gateway_id: str | None = None
    node_id: str | None = None
    pot_id: int | None = None
    correlation_id: str | None = None
    actuator: str | None = None
    action: str | None = None
    volume_ml: int | None = None
    max_runtime_ms: int | None = None
    expires_at_epoch: float | None = None
    expires_at_raw: str | None = None

    def context(self) -> dict[str, object]:
        """The fields an ack has to echo back, for the journal.

        Persisted so a gateway that restarts between ``accepted`` and
        ``completed`` can still publish a fully-formed ack for the dose that was
        running while it was down.
        """

        return {
            "correlation_id": self.correlation_id,
            "node_id": self.node_id,
            "pot_id": self.pot_id,
            "volume_ml": self.volume_ml,
            "max_runtime_ms": self.max_runtime_ms,
        }


def parse_command(payload: bytes) -> CommandRequest:
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CommandError("command payload is not UTF-8") from exc
    try:
        message = json.loads(text)
    except json.JSONDecodeError as exc:
        raise CommandError("command payload is not valid JSON") from exc
    if not isinstance(message, dict):
        raise CommandError("command payload must be a JSON object")

    command_id = message.get("command_id")
    if not isinstance(command_id, str) or not 1 <= len(command_id.strip()) <= 64:
        raise CommandError("command_id must be a 1..64 character string")

    expires_at_raw = _optional_str(message, "expires_at")
    expires_at_epoch: float | None = None
    if expires_at_raw is not None:
        try:
            expires_at_epoch = parse_iso8601_utc(expires_at_raw).timestamp()
        except ProtocolError as exc:
            # Left as None, which the relay treats as already expired. Logged
            # here because this is the only place that saw the raw text.
            LOGGER.error(
                "command has an unusable expires_at command_id=%s reason=%s",
                command_id.strip(),
                exc,
            )

    raw_params = message.get("params")
    params: dict[str, Any] = raw_params if isinstance(raw_params, dict) else {}

    return CommandRequest(
        command_id=command_id.strip(),
        schema_version=_optional_int(message, "schema_version"),
        message_type=_optional_str(message, "message_type"),
        gateway_id=_optional_str(message, "gateway_id"),
        node_id=_optional_str(message, "node_id"),
        pot_id=_optional_int(message, "pot_id"),
        correlation_id=_optional_str(message, "correlation_id"),
        actuator=_optional_str(message, "actuator"),
        action=_optional_str(message, "action"),
        volume_ml=_optional_int(params, "volume_ml"),
        # The rename that is easy to get wrong: MQTT spells it max_runtime_ms,
        # the serial link spells it ms, and they are the same number.
        max_runtime_ms=_optional_int(params, "max_runtime_ms"),
        expires_at_epoch=expires_at_epoch,
        expires_at_raw=expires_at_raw,
    )


def serial_command_frame(request: CommandRequest) -> bytes:
    """Build the short-key serial command. Key order matters for readability only.

    ``ms`` is passed through **unclamped** even though the firmware caps it at
    PUMP_ABS_MAX_MS. Clamping here would make the firmware report
    ``stop:"volume_reached"`` for a run that was actually cut short, so the
    backend would record a full dose for a partial one and the operator would
    never learn that a command asked for more than the hardware allows. Letting
    the over-long value through preserves that evidence: the firmware answers
    ``stop:"max_runtime"`` with the runtime it really achieved.
    """

    frame: dict[str, object] = {
        "t": "cmd",
        "id": request.command_id,
        "act": request.actuator,
        "ms": request.max_runtime_ms,
    }
    if request.volume_ml is not None:
        frame["ml"] = request.volume_ml
    return json.dumps(frame, separators=(",", ":")).encode("utf-8")


class CommandJournal:
    """Durable record of every command id this gateway has ever claimed.

    In SQLite rather than a set in memory because the duplicate window outlives
    the process. QoS 1 permits a duplicate at *both* hops (broker to gateway, and
    gateway to broker), the firmware's ring buffer only remembers eight ids, and
    a restart in between would clear an in-memory set — so a redelivered command
    would water a second time and every layer would think it was behaving.

    Its own table in the outbox database, not a column on the outbox: an outbox
    row is deleted once delivered, which would make it useless as a memory of
    what has been seen.
    """

    def __init__(
        self,
        path: Path,
        *,
        retention_seconds: float,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.path = path
        self.retention_seconds = retention_seconds
        self.clock = clock

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = self._connect()
        try:
            with connection:
                # WAL again rather than assuming the outbox got there first: the
                # two open independent connections to the same file, and a reader
                # blocking a writer here would stall the relay thread.
                connection.execute("PRAGMA journal_mode = WAL")
                connection.execute("PRAGMA synchronous = FULL")
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS command_journal (
                        command_id TEXT PRIMARY KEY,
                        received_at_epoch REAL NOT NULL,
                        expires_at_epoch REAL,
                        decision TEXT NOT NULL,
                        reason TEXT,
                        context_json TEXT
                    )
                    """
                )
                connection.execute(
                    """
                    CREATE INDEX IF NOT EXISTS command_journal_age
                        ON command_journal(received_at_epoch)
                    """
                )
        finally:
            connection.close()

    def claim(self, request: CommandRequest, *, now: float | None = None) -> bool:
        """Record this command id, or return False if it was already recorded.

        The INSERT is the claim: SQLite's primary key does the mutual exclusion,
        so two deliveries arriving close together cannot both win, and the answer
        survives a power cut. Claiming happens *before* the TTL check so that a
        redelivered expired command is answered once as a duplicate rather than
        expiring twice.
        """

        moment = self.clock() if now is None else now
        connection = self._connect()
        try:
            with connection:
                cursor = connection.execute(
                    """
                    INSERT OR IGNORE INTO command_journal(
                        command_id, received_at_epoch, expires_at_epoch, decision,
                        context_json
                    ) VALUES (?, ?, ?, 'claimed', ?)
                    """,
                    (
                        request.command_id,
                        moment,
                        request.expires_at_epoch,
                        json.dumps(request.context(), separators=(",", ":")),
                    ),
                )
            return cursor.rowcount == 1
        finally:
            connection.close()

    def mark(self, command_id: str, decision: str, reason: str | None = None) -> None:
        connection = self._connect()
        try:
            with connection:
                connection.execute(
                    """
                    UPDATE command_journal SET decision = ?, reason = ?
                    WHERE command_id = ?
                    """,
                    (decision, reason, command_id),
                )
        finally:
            connection.close()

    def context(self, command_id: str) -> dict[str, object] | None:
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT context_json FROM command_journal WHERE command_id = ?",
                (command_id,),
            ).fetchone()
        finally:
            connection.close()
        if row is None or not row["context_json"]:
            return None
        try:
            context = json.loads(row["context_json"])
        except json.JSONDecodeError:
            return None
        return context if isinstance(context, dict) else None

    def prune(self, *, now: float | None = None) -> int:
        """Drop entries older than the retention window.

        Bounded growth matters on a board with an SD card, but the window has to
        comfortably exceed any plausible broker redelivery — a day, against a
        two-minute command TTL.
        """

        moment = self.clock() if now is None else now
        connection = self._connect()
        try:
            with connection:
                cursor = connection.execute(
                    "DELETE FROM command_journal WHERE received_at_epoch < ?",
                    (moment - self.retention_seconds,),
                )
            return cursor.rowcount
        finally:
            connection.close()


@dataclass
class _InFlight:
    """A command believed to be running on the Arduino right now."""

    command_id: str
    port: str
    node_id: str | None
    pot_id: int | None
    correlation_id: str | None
    volume_ml: int | None
    max_runtime_ms: int
    deadman_until_epoch: float


class CommandRelay:
    def __init__(
        self,
        *,
        gateway_id: str,
        transport: CommandTransport,
        outbox: Any,
        state: GatewayState,
        readers: Sequence[SerialLineReader],
        journal: CommandJournal,
        stop_event: threading.Event,
        queue_max: int = 32,
        deadman_interval_seconds: float = 1.0,
        deadman_grace_seconds: float = 5.0,
        max_serial_bytes: int = 120,
        journal_prune_interval_seconds: float = 3600.0,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._gateway_id = gateway_id
        self._transport = transport
        self._outbox = outbox
        self._state = state
        self._readers = {reader.port: reader for reader in readers}
        self._journal = journal
        self._stop = stop_event
        self._deadman_interval_seconds = deadman_interval_seconds
        self._deadman_grace_seconds = deadman_grace_seconds
        self._max_serial_bytes = max_serial_bytes
        self._journal_prune_interval_seconds = journal_prune_interval_seconds
        self._clock = clock
        # Bounded. An unbounded queue would turn a wedged relay thread into
        # unbounded memory growth in a process that runs for weeks, and the
        # overflow path below is strictly better than that: the command is
        # refused loudly instead of being remembered forever.
        self._queue: queue.Queue[bytes] = queue.Queue(maxsize=queue_max)
        self._lock = threading.Lock()
        self._in_flight: dict[str, _InFlight] = {}
        self._next_prune_epoch = 0.0
        # Counters for the display and for tests: "how many commands did this
        # gateway actually put on the wire" is the number the delayed-bomb
        # scenario is asserted against.
        self.relayed = 0
        self.rejected = 0
        self.dropped = 0

    # -- wiring ----------------------------------------------------------

    def workers(self) -> list[tuple[str, Callable[[], None]]]:
        """The two threads this relay needs, for ``BridgeService._critical_workers``.

        The deadman is a separate thread but not an independent collaborator: it
        reads this object's in-flight table, which is the only place that knows a
        pump is running. A standalone keepalive worker would either tick forever
        (defeating G3) or need the same state passed to it anyway.
        """

        return [
            ("command-relay", self.run),
            ("command-deadman", self.run_deadman),
        ]

    def offer(self, payload: bytes, retained: bool = False) -> None:
        """Accept one raw command. **Runs on paho's network thread.**

        Everything here is O(1) and non-blocking on purpose; see the module
        docstring. ``put_nowait`` rather than ``put``: blocking here would block
        paho's network loop, which is how the MQTT keepalive dies.
        """

        if retained:
            # By contract dn/command is never retained. A retained one is a
            # fossil that the broker will redeliver on *every* reconnect, so
            # obeying it would re-run an old irrigation each time the network
            # blips. Dropped rather than rejected-with-an-ack: an ack per
            # reconnect would be a stream of noise about a command the server
            # settled long ago, and the fix is on the publisher's side.
            self.dropped += 1
            LOGGER.error(
                "dropping RETAINED command; dn/command must never be retained. "
                "Clear it with an empty retained publish on that topic."
            )
            self._state.add_event("error", "retain된 관수 명령을 폐기했습니다")
            return
        try:
            self._queue.put_nowait(payload)
        except queue.Full:
            self.dropped += 1
            LOGGER.critical(
                "command queue is full; dropping a command. The relay thread is "
                "not draining — check for a blocked serial write."
            )

    # -- the relay thread -------------------------------------------------

    def run(self) -> None:
        self._journal.initialize()
        # Subscribing from this thread, not from the constructor, keeps
        # BridgeService.__init__ free of network work and guarantees the queue
        # has a consumer before any command can arrive.
        self._transport.subscribe_commands(self.offer)
        while not self._stop.is_set():
            self._maybe_prune()
            try:
                payload = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                self._process(payload)
            except Exception:
                # One malformed command must not take the relay down: the thread
                # is in _critical_workers, so its death restarts the service and
                # would drop telemetry from every pot.
                LOGGER.exception("command processing failed")

    def _maybe_prune(self) -> None:
        now = self._clock()
        if now < self._next_prune_epoch:
            return
        self._next_prune_epoch = now + self._journal_prune_interval_seconds
        try:
            removed = self._journal.prune(now=now)
        except sqlite3.Error as exc:
            LOGGER.warning("command journal prune failed reason=%s", exc)
            return
        if removed:
            LOGGER.info("pruned command journal rows=%d", removed)

    def _process(self, payload: bytes) -> None:
        try:
            request = parse_command(payload)
        except CommandError as exc:
            # Unanswerable: no command_id, so no ack can be correlated.
            self.dropped += 1
            LOGGER.error("discarding unusable command reason=%s", exc)
            return

        if request.gateway_id is not None and request.gateway_id != self._gateway_id:
            # Not ours to answer. The backend authenticates an ack by resolving
            # command_id -> pot -> gateway and comparing that with the topic, so
            # an ack from us would be rejected as a forgery anyway; publishing
            # one would only inject noise into another gateway's command
            # lifecycle. Dropped, and loudly, because it means a misrouted
            # publish or a gateway id that disagrees with the MQTT credentials.
            self.dropped += 1
            LOGGER.error(
                "dropping command addressed to another gateway command_id=%s "
                "addressed_to=%s we_are=%s",
                request.command_id,
                request.gateway_id,
                self._gateway_id,
            )
            return

        now = self._clock()
        if not self._journal.claim(request, now=now):
            LOGGER.warning(
                "duplicate command id, not executing command_id=%s", request.command_id
            )
            self._reject(request, REASON_DUPLICATE, STOP_PI_DUPLICATE)
            return

        if request.schema_version != 2 or request.message_type != "command":
            LOGGER.error(
                "refusing command with unexpected envelope command_id=%s "
                "schema_version=%s message_type=%s",
                request.command_id,
                request.schema_version,
                request.message_type,
            )
            self._reject(request, REASON_NODE_OFFLINE, STOP_PI_BAD_SCHEMA)
            return

        # --- TTL, the whole reason this layer exists (D19) ---
        if request.expires_at_epoch is None:
            # A command whose deadline cannot be read is treated as already
            # expired rather than as unlimited. Fail-safe: the alternative is
            # running a dose with no deadline at all.
            LOGGER.error(
                "refusing command with no usable expires_at command_id=%s raw=%r",
                request.command_id,
                request.expires_at_raw,
            )
            self._reject(request, REASON_EXPIRED, STOP_PI_NO_EXPIRY)
            return
        if request.expires_at_epoch <= now:
            LOGGER.warning(
                "discarding expired command command_id=%s expires_at=%s "
                "late_by_seconds=%.1f",
                request.command_id,
                request.expires_at_raw,
                now - request.expires_at_epoch,
            )
            self._reject(request, REASON_EXPIRED, STOP_PI_EXPIRED)
            self._state.add_event("warn", "만료된 관수 명령을 폐기했습니다")
            return

        if request.actuator not in SUPPORTED_ACTUATORS:
            self._reject(request, REASON_NODE_OFFLINE, STOP_PI_BAD_ACTUATOR)
            LOGGER.error(
                "refusing unsupported actuator command_id=%s actuator=%s",
                request.command_id,
                request.actuator,
            )
            return
        if request.action not in SUPPORTED_ACTIONS:
            self._reject(request, REASON_NODE_OFFLINE, STOP_PI_BAD_ACTUATOR)
            LOGGER.error(
                "refusing unsupported action command_id=%s action=%s",
                request.command_id,
                request.action,
            )
            return
        runtime_ms = request.max_runtime_ms
        if runtime_ms is None or not 0 < runtime_ms <= 0xFFFFFFFF:
            # uint32 because the firmware's millis() arithmetic is uint32; a
            # value beyond it would wrap into a short run or an instant stop.
            LOGGER.error(
                "refusing command with unusable max_runtime_ms command_id=%s ms=%s",
                request.command_id,
                runtime_ms,
            )
            self._reject(request, REASON_NODE_OFFLINE, STOP_PI_BAD_PARAMS)
            return
        if request.volume_ml is not None and request.volume_ml <= 0:
            LOGGER.error(
                "refusing command with unusable volume_ml command_id=%s ml=%s",
                request.command_id,
                request.volume_ml,
            )
            self._reject(request, REASON_NODE_OFFLINE, STOP_PI_BAD_PARAMS)
            return

        port, failure = self._resolve_port(request.node_id)
        if port is None:
            self._reject(request, REASON_NODE_OFFLINE, failure)
            return

        frame = serial_command_frame(request)
        if len(frame) > self._max_serial_bytes:
            # The firmware reads into a fixed line buffer on a 2 KB device, so an
            # over-long frame is a memory-safety event there rather than a
            # parse error. Refused here, where the consequence is a logged
            # rejection instead of a corrupted stack.
            LOGGER.error(
                "refusing command whose serial frame is too long command_id=%s "
                "bytes=%d limit=%d",
                request.command_id,
                len(frame),
                self._max_serial_bytes,
            )
            self._reject(request, REASON_NODE_OFFLINE, STOP_PI_FRAME_TOO_LONG)
            return

        reader = self._readers.get(port)
        # Registered *before* the write, not after: the firmware may start the
        # pump the instant the bytes land, and a deadman that only starts once
        # the write call returns leaves a window in which G3 would cut a
        # legitimate dose short.
        self._register_in_flight(request, port=port, runtime_ms=runtime_ms)
        if reader is None or not reader.write_line(frame):
            self._discard_in_flight(request.command_id)
            LOGGER.error(
                "could not deliver command to the node command_id=%s port=%s",
                request.command_id,
                port,
            )
            self._reject(request, REASON_NODE_OFFLINE, STOP_PI_LINK_DOWN)
            return

        self.relayed += 1
        self._journal.mark(request.command_id, "relayed")
        LOGGER.info(
            "command relayed command_id=%s node_id=%s port=%s ms=%d ml=%s",
            request.command_id,
            request.node_id,
            port,
            runtime_ms,
            request.volume_ml,
        )
        self._state.add_event(
            "info", f"관수 명령 전달 {request.node_id} {request.volume_ml or '?'} mL"
        )

    def _resolve_port(self, node_id: str | None) -> tuple[str | None, str]:
        """Which cable this node is on, from what the link has actually reported.

        Resolved from observed traffic rather than from configuration because
        ports and nodes are matched at runtime by the node_id the firmware
        announces — a gateway cabled for four pots may have two Arduinos powered
        on. A node nobody has heard from is genuinely unreachable, and
        NODE_OFFLINE is exactly the right answer; guessing "the only port we
        have" would water whichever pot happened to be plugged in.
        """

        if not node_id:
            return None, STOP_PI_BAD_PARAMS
        matches = [
            port.path
            for port in self._state.snapshot().ports
            if port.node_id == node_id
        ]
        if not matches:
            LOGGER.error("no port has reported node_id=%s", node_id)
            return None, STOP_PI_UNKNOWN_NODE
        if len(matches) > 1:
            # Two Arduinos flashed with the same TB_NODE_ID. Watering both pots
            # from one command is worse than watering neither.
            LOGGER.error(
                "refusing to guess a port: node_id=%s is claimed by %s",
                node_id,
                ",".join(sorted(matches)),
            )
            return None, STOP_PI_AMBIGUOUS_NODE
        return matches[0], ""

    def _register_in_flight(
        self, request: CommandRequest, *, port: str, runtime_ms: int
    ) -> None:
        window = min(runtime_ms, PUMP_ABS_MAX_MS) / 1000.0
        with self._lock:
            self._in_flight[request.command_id] = _InFlight(
                command_id=request.command_id,
                port=port,
                node_id=request.node_id,
                pot_id=request.pot_id,
                correlation_id=request.correlation_id,
                volume_ml=request.volume_ml,
                max_runtime_ms=runtime_ms,
                deadman_until_epoch=self._clock()
                + window
                + self._deadman_grace_seconds,
            )

    def _discard_in_flight(self, command_id: str) -> _InFlight | None:
        with self._lock:
            return self._in_flight.pop(command_id, None)

    # -- the deadman thread ----------------------------------------------

    def run_deadman(self) -> None:
        while not self._stop.is_set():
            try:
                self.tick_deadman()
            except Exception:
                LOGGER.exception("deadman tick failed")
            self._stop.wait(self._deadman_interval_seconds)

    def tick_deadman(self) -> None:
        """Send ``{"t":"ka"}`` to every port with a dose believed to be running.

        This is the *sending* side of the firmware's G3 watchdog: three seconds
        of silence from the host and the pump stops immediately. The interval
        must therefore stay well under that — config refuses anything above 2 s.

        When a command's window closes without a terminal ack, ticking for it
        stops. That is deliberate and it is the safe direction: if the pump were
        somehow still running, silence makes G3 stop it within three seconds,
        whereas ticking forever on a command whose ack was lost would hold the
        watchdog open indefinitely — the one outcome the watchdog exists to
        prevent.
        """

        now = self._clock()
        with self._lock:
            stale = [
                command_id
                for command_id, flight in self._in_flight.items()
                if flight.deadman_until_epoch <= now
            ]
            for command_id in stale:
                LOGGER.warning(
                    "no terminal ack within the run window; stopping the deadman "
                    "tick command_id=%s",
                    command_id,
                )
                del self._in_flight[command_id]
            ports = sorted({flight.port for flight in self._in_flight.values()})
        for port in ports:
            reader = self._readers.get(port)
            if reader is None:
                continue
            if not reader.write_line(DEADMAN_FRAME):
                # Not escalated to a rejection: the firmware is about to stop the
                # pump by itself, which is the correct outcome, and the ack for
                # that abort will carry stop_cause "watchdog".
                LOGGER.warning("deadman tick could not reach port=%s", port)

    # -- the ingest thread ------------------------------------------------

    def handle_serial_ack(self, port: str, message: dict[str, Any]) -> None:
        """Translate one short-key ack and queue it for the backend.

        **Runs on the serial ingest thread**, so it must not block: the outbox
        enqueue is a local SQLite write and the actual publish happens on the
        ack-upload worker. Publishing from here would put a broker round-trip in
        the path that also reads telemetry.
        """

        try:
            ack = parse_serial_ack(message)
        except ProtocolError as exc:
            LOGGER.warning("discarding malformed ack port=%s reason=%s", port, exc)
            self._state.record_error(port)
            return

        with self._lock:
            flight = self._in_flight.get(ack.command_id)
            if flight is not None:
                if ack.phase in TERMINAL_PHASES:
                    del self._in_flight[ack.command_id]
                elif ack.phase == "accepted":
                    # The pump starts at acceptance, so the run window is
                    # measured from here rather than from the write.
                    flight.deadman_until_epoch = (
                        self._clock()
                        + min(flight.max_runtime_ms, PUMP_ABS_MAX_MS) / 1000.0
                        + self._deadman_grace_seconds
                    )

        context = self._context_for(ack.command_id, flight)
        node_id = context.get("node_id") or self._node_on(port)
        pot_id = context.get("pot_id")
        command_ack = CommandAck(
            command_id=ack.command_id,
            phase=ack.phase,
            at_utc=epoch_to_iso8601(self._clock()),
            reason=mqtt_reason(ack.phase, ack.stop_cause),
            correlation_id=context.get("correlation_id"),
            node_id=node_id if isinstance(node_id, str) else None,
            pot_id=pot_id if isinstance(pot_id, int) else None,
            runtime_ms=ack.runtime_ms,
            estimated_ml=self._estimated_ml(ack, context),
            # Verbatim. This is the only field in which the firmware's own word
            # survives two translations.
            stop_cause=ack.stop_cause,
        )
        LOGGER.info(
            "command ack command_id=%s phase=%s reason=%s stop_cause=%s runtime_ms=%s",
            command_ack.command_id,
            command_ack.phase,
            command_ack.reason,
            command_ack.stop_cause,
            command_ack.runtime_ms,
        )
        self._enqueue_ack(command_ack)

    def _context_for(
        self, command_id: str, flight: _InFlight | None
    ) -> dict[str, object]:
        if flight is not None:
            return {
                "correlation_id": flight.correlation_id,
                "node_id": flight.node_id,
                "pot_id": flight.pot_id,
                "volume_ml": flight.volume_ml,
                "max_runtime_ms": flight.max_runtime_ms,
            }
        # No live record: either the ack is late, or this process restarted while
        # the dose was running. The journal is why the second case still produces
        # a fully-formed ack instead of a bare command_id.
        try:
            stored = self._journal.context(command_id)
        except sqlite3.Error as exc:
            LOGGER.warning("command journal read failed reason=%s", exc)
            stored = None
        if stored is None:
            LOGGER.warning(
                "ack for a command this gateway has no record of command_id=%s",
                command_id,
            )
            return {}
        return stored

    def _node_on(self, port: str) -> str | None:
        for snapshot in self._state.snapshot().ports:
            if snapshot.path == port:
                return snapshot.node_id
        return None

    @staticmethod
    def _estimated_ml(ack: Any, context: dict[str, object]) -> int | None:
        """What the pot actually received, measured if possible and derived if not.

        The firmware has no flow meter, so unless it reports ``ml`` itself the
        volume is prorated from the runtime it achieved against the runtime that
        was commanded. That matters specifically because of G1: a 60 s command
        clamped to 30 s delivered roughly half the water, and recording the full
        granted volume would charge the budget for water that never left the
        reservoir.

        Rounded **up**. Over-reporting delays the next dose by a millilitre's
        worth; under-reporting adds water the daily budget never saw, and the
        budget is the only thing standing between a bad model and a drowned
        plant.
        """

        if ack.volume_ml is not None:
            return ack.volume_ml
        if ack.phase not in ("completed", "aborted"):
            return None
        volume = context.get("volume_ml")
        commanded = context.get("max_runtime_ms")
        if not isinstance(volume, int) or not isinstance(commanded, int):
            return None
        if commanded <= 0 or ack.runtime_ms is None:
            return None
        ran = max(0, min(ack.runtime_ms, commanded))
        return math.ceil(volume * ran / commanded)

    # -- outcomes ---------------------------------------------------------

    def _reject(
        self, request: CommandRequest, reason: str, stop_cause: str
    ) -> None:
        self.rejected += 1
        self._journal.mark(request.command_id, "rejected", reason)
        self._enqueue_ack(
            CommandAck(
                command_id=request.command_id,
                phase="rejected",
                at_utc=epoch_to_iso8601(self._clock()),
                reason=reason,
                correlation_id=request.correlation_id,
                node_id=request.node_id,
                pot_id=request.pot_id,
                stop_cause=stop_cause or None,
            )
        )

    def _enqueue_ack(self, ack: CommandAck) -> None:
        try:
            enqueued = self._outbox.enqueue(ack, kind=KIND_ACK)
        except OutboxFullError as exc:
            # Louder than a dropped reading. A lost ack leaves the backend to
            # expire the command and charge its granted volume to the daily
            # budget anyway — water the pot never got, subtracted from what it
            # may get.
            LOGGER.critical(
                "command ack dropped because the durable outbox is full "
                "command_id=%s reason=%s",
                ack.command_id,
                exc,
            )
            self._state.add_event("error", "저장 큐가 가득 차 관수 결과를 버렸습니다")
            return
        if not enqueued:
            LOGGER.info(
                "ack already queued, not duplicating command_id=%s phase=%s",
                ack.command_id,
                ack.phase,
            )

    def in_flight_ids(self) -> Iterable[str]:
        with self._lock:
            return tuple(self._in_flight)
