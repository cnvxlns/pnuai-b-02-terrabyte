"""Arduino JSON Lines protocol validation, both directions.

Telemetry v1 (long keys, Arduino -> Pi) and the command ack family (short keys,
Arduino -> Pi) both live here because both are decided from serial bytes alone.
The MQTT side of the command contract is ``command_relay.py``'s: this module
never decides whether a dose may run, only what the wire said.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
import logging
import math
from typing import Any, Callable
import uuid


LOGGER = logging.getLogger(__name__)


class ProtocolError(ValueError):
    """Raised for malformed or invalid telemetry messages."""


class NonTelemetryMessage(ProtocolError):
    """Raised for valid protocol messages that carry no measurements.

    ``node_id`` is carried alongside the type because the ``hello`` frame is
    how a port learns which Arduino is on the other end, before that Arduino
    has produced its first reading. Discarding it would leave a freshly
    plugged-in node invisible on screen until its first sample arrives.
    """

    def __init__(self, message_type: str, node_id: str | None = None) -> None:
        super().__init__(message_type)
        self.message_type = message_type
        self.node_id = node_id


class UnknownNodeError(ProtocolError):
    """Raised when a line carries a node_id outside the configured allowlist.

    Distinct from a plain ProtocolError so the service can count it separately
    and name the offending id on screen: an Arduino flashed with the wrong
    TB_NODE_ID looks identical to a dead port in the logs otherwise.
    """

    def __init__(self, node_id: str) -> None:
        super().__init__(f"node_id is not in the configured allowlist: {node_id}")
        self.node_id = node_id


@dataclass(frozen=True)
class IrrigationSuggestion:
    """Edge-computed watering volume for this node's pot.

    ``assumed_crop_code`` and ``assumed_substrate_volume_ml`` are not inputs the
    backend needs in order to act -- they are the edge's own configuration,
    shipped so the backend can compare them against its ``pot`` record and log
    a loud WARN when the two disagree. Pot volume is physical config the edge is
    authoritative for, so on a mismatch the backend keeps the suggestion and
    reports the drift rather than silently overriding it
    (docs/design/irrigation_volume.md §2, §3.1). Without these two fields a
    dose sized for the wrong pot looks exactly like a correct one.
    """

    volume_ml: int
    model_version: str
    assumed_crop_code: str | None = None
    assumed_substrate_volume_ml: int | None = None

    def to_payload(self) -> dict[str, object]:
        """Wire form. Unknowns are omitted, never sent as null.

        Same rule the soil measurements follow below: an absent key says "not
        configured", where ``null`` would read as a decision that was made.
        """

        payload: dict[str, object] = {
            "volume_ml": self.volume_ml,
            "model_version": self.model_version,
        }
        if self.assumed_crop_code is not None:
            payload["assumed_crop_code"] = self.assumed_crop_code
        if self.assumed_substrate_volume_ml is not None:
            payload["assumed_substrate_volume_ml"] = self.assumed_substrate_volume_ml
        return payload


@dataclass(frozen=True)
class Event:
    event_id: str
    context_id: str
    captured_at_utc: str
    node_id: str
    sequence: int
    uptime_ms: int
    air_temperature_c: float
    relative_humidity_pct: float
    ppfd_umol_m2_s: float
    # Optional: the DS18B20 and the capacitive probe are physically optional,
    # and the firmware only emits these fields when they are compiled in.
    # Defaulted so an event queued by an older build still deserialises from
    # the outbox — the SQLite payload is a JSON blob, so old rows simply lack
    # these keys.
    soil_temperature_c: float | None = None
    soil_moisture_pct: float | None = None
    soil_moisture_raw_adc: int | None = None
    # Not from the wire: computed by the service from this event's readings and
    # the node's configured pot. Defaulted for the same reason as the soil
    # fields above — an outbox row queued by a build that predates this field
    # must still deserialise.
    irrigation_suggestion: IrrigationSuggestion | None = None

    @property
    def has_soil_reading(self) -> bool:
        return self.soil_temperature_c is not None or self.soil_moisture_pct is not None

    def measurements(self) -> dict[str, float]:
        """The metric map, keyed by backend ``MeasurementMetric`` field names.

        Soil metrics are omitted entirely rather than sent as null or zero when
        the probes are absent: a 0.0 would reach the irrigation path as a
        confident "bone dry", which is the one reading that must never be
        fabricated.

        Both the wire envelope and the on-screen status read this, so the
        display can never disagree with what was actually published.
        """

        values: dict[str, float] = {
            "air_temperature_c": self.air_temperature_c,
            "air_humidity_pct": self.relative_humidity_pct,
            "plant_light_ppfd_umol_m2_s": self.ppfd_umol_m2_s,
        }
        if self.soil_temperature_c is not None:
            values["soil_temperature_c"] = self.soil_temperature_c
        if self.soil_moisture_pct is not None:
            values["soil_moisture_pct"] = self.soil_moisture_pct
        if self.soil_moisture_raw_adc is not None:
            values["soil_moisture_raw_adc"] = self.soil_moisture_raw_adc
        return values

    def envelope_v2(self, *, gateway_id: str) -> dict[str, object]:
        """Build a telemetry.sample envelope v2 body (one node per envelope).

        ``gateway_id`` is a transport-time parameter rather than a stored
        field: it is deployment-wide config (``Settings.device_id``), not a
        property of a single sensor reading, so passing it in here avoids
        duplicating it into every queued outbox row and avoids changing the
        SQLite outbox schema (an explicit design constraint — see
        docs/design/device_model_and_telemetry_contract.md §6.1).

        Field names come from :meth:`measurements`, which must match the
        backend ``MeasurementMetric`` enum literally — including its rule that
        absent soil metrics are omitted rather than sent as null or zero,
        because a 0.0 would reach the irrigation path as a confident "bone
        dry", the one reading that must never be fabricated.
        """
        node: dict[str, object] = {
            "node_id": self.node_id,
            "sequence": self.sequence,
            "measurements": self.measurements(),
            "quality": {
                # Ranges were already enforced by parse_line() before this
                # Event was constructed, so every reading present here is valid
                # by construction. An absent soil probe reports false, matching
                # "unusable for irrigation".
                "air_sensor_valid": True,
                "light_sensor_valid": True,
                "soil_sensor_valid": self.has_soil_reading,
            },
        }
        # Omitted whole when the edge could not compute it (no moisture reading,
        # or no configured pot volume). The backend then uses its pot-size
        # fallback table; a fabricated number would displace that fallback and
        # look exactly as trustworthy as a real one (§3.1).
        if self.irrigation_suggestion is not None:
            node["irrigation_suggestion"] = self.irrigation_suggestion.to_payload()

        return {
            "schema_version": 2,
            "event_type": "telemetry.sample",
            "gateway_id": gateway_id,
            "event_id": self.event_id,
            "observed_at": self.captured_at_utc,
            "nodes": [node],
        }

    def to_record(self) -> dict[str, object]:
        return {
            "event_id": self.event_id,
            "context_id": self.context_id,
            "captured_at_utc": self.captured_at_utc,
            "node_id": self.node_id,
            "sequence": self.sequence,
            "uptime_ms": self.uptime_ms,
            "air_temperature_c": self.air_temperature_c,
            "relative_humidity_pct": self.relative_humidity_pct,
            "ppfd_umol_m2_s": self.ppfd_umol_m2_s,
            "soil_temperature_c": self.soil_temperature_c,
            "soil_moisture_pct": self.soil_moisture_pct,
            "soil_moisture_raw_adc": self.soil_moisture_raw_adc,
            # Every field, including the ones ``to_payload`` omits: this is
            # storage, and it has to round-trip back into the same object.
            "irrigation_suggestion": (
                None
                if self.irrigation_suggestion is None
                else asdict(self.irrigation_suggestion)
            ),
        }

    @classmethod
    def from_record(cls, record: dict[str, Any]) -> "Event":
        # Popped rather than passed through: the nested suggestion is a dict in
        # the stored JSON blob and a dataclass in memory. A row written before
        # this field existed simply has no key, and defaults to None.
        fields = dict(record)
        suggestion = fields.pop("irrigation_suggestion", None)
        return cls(
            **fields,
            irrigation_suggestion=(
                None if suggestion is None else IrrigationSuggestion(**suggestion)
            ),
        )


def _uint32(message: dict[str, Any], name: str) -> int:
    value = message.get(name)
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not 0 <= value <= 0xFFFFFFFF
    ):
        raise ProtocolError(f"{name} must be a uint32")
    return value


def _reading(
    message: dict[str, Any], name: str, minimum: float, maximum: float
) -> float:
    value = message.get(name)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ProtocolError(f"{name} must be numeric")
    reading = float(value)
    if not math.isfinite(reading) or not minimum <= reading <= maximum:
        raise ProtocolError(f"{name} is outside canonical range")
    return reading


def _optional_reading(
    message: dict[str, Any], name: str, minimum: float, maximum: float
) -> float | None:
    """Read a field the firmware only emits when that probe is compiled in.

    Absent means "no probe", which is legitimate. Present but out of range is
    a fault, and is rejected rather than clamped: a bad soil reading silently
    coerced into range would feed the irrigation decision as if it were real.
    """

    if name not in message:
        return None
    return _reading(message, name, minimum, maximum)


def _optional_uint32(message: dict[str, Any], name: str) -> int | None:
    if name not in message:
        return None
    return _uint32(message, name)


def parse_line(
    line: bytes,
    *,
    context_id: str,
    allowed_node_ids: frozenset[str],
    clock_minimum_utc: datetime,
    clock: Callable[[], datetime] | None = None,
    event_id_factory: Callable[[], uuid.UUID] = uuid.uuid4,
) -> Event:
    """Parse one complete UTF-8 JSON line and assign stable outbox identity/time."""

    try:
        text = line.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ProtocolError("line is not UTF-8") from exc
    try:
        message = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ProtocolError("line is not valid JSON") from exc
    if not isinstance(message, dict):
        raise ProtocolError("message must be a JSON object")

    message_type = message.get("message_type")
    if message_type in {"hello", "sensor_status", "configuration_error"}:
        announced = message.get("node_id")
        raise NonTelemetryMessage(
            str(message_type),
            announced if isinstance(announced, str) and announced else None,
        )
    if message_type != "telemetry":
        raise ProtocolError("message_type must be telemetry")
    if message.get("protocol_version") != 1:
        raise ProtocolError("unsupported protocol_version")

    node_id = message.get("node_id")
    if not isinstance(node_id, str) or not 1 <= len(node_id) <= 64:
        raise ProtocolError("node_id must be a 1..64 character string")
    if any(
        not (character.isascii() and (character.isalnum() or character in "-_.:"))
        for character in node_id
    ):
        raise ProtocolError("node_id contains unsupported characters")
    # The allowlist stays a hard reject rather than widening to "any node".
    # It is the only thing stopping an Arduino cabled into the wrong gateway
    # from injecting readings against someone else's pot.
    if node_id not in allowed_node_ids:
        raise UnknownNodeError(node_id)
    if not context_id:
        raise ProtocolError("context_id must not be empty")

    now = (clock or (lambda: datetime.now(timezone.utc)))()
    if now.tzinfo is None:
        raise ProtocolError("clock must return a timezone-aware datetime")
    now_utc = now.astimezone(timezone.utc)
    if now_utc < clock_minimum_utc:
        raise ProtocolError("system clock is earlier than the configured minimum")
    captured_at = now_utc.isoformat(timespec="seconds").replace("+00:00", "Z")
    return Event(
        event_id=str(event_id_factory()),
        context_id=context_id,
        captured_at_utc=captured_at,
        node_id=node_id,
        sequence=_uint32(message, "sequence"),
        uptime_ms=_uint32(message, "uptime_ms"),
        air_temperature_c=_reading(message, "air_temperature_c", -50.0, 80.0),
        relative_humidity_pct=_reading(
            message, "relative_humidity_pct", 0.0, 100.0
        ),
        ppfd_umol_m2_s=_reading(message, "ppfd_umol_m2_s", 0.0, 5000.0),
        soil_temperature_c=_optional_reading(
            message, "soil_temperature_c", -20.0, 80.0
        ),
        soil_moisture_pct=_optional_reading(
            message, "soil_moisture_pct", 0.0, 100.0
        ),
        soil_moisture_raw_adc=_optional_uint32(message, "soil_moisture_raw_adc"),
    )


# --- command acks -----------------------------------------------------------
#
# The four phases of one command's life. A single schema carries all four
# (docs/design/edge_ai_hardening.md §MQTT contract), and the backend decides
# state from ``phase`` alone — never from ``reason``, whose vocabulary differs
# per layer. See command_relay.REASONS for why that separation matters.
ACK_PHASES = ("accepted", "rejected", "completed", "aborted")

# ``device_command.stop_cause`` is VARCHAR(30) on the backend. Truncating here
# rather than there is deliberate: an over-long firmware token would otherwise
# fail the INSERT and lose the whole ack — the one message that tells the server
# a pump ran. A truncated diagnostic beats a dropped outcome.
STOP_CAUSE_MAX_CHARS = 30


def epoch_to_iso8601(epoch: float) -> str:
    """Epoch seconds -> ``2026-08-04T10:00:18Z``.

    Always UTC with a literal ``Z``: the backend parses these as ``Instant``,
    and a local-offset timestamp from a box whose TZ is Asia/Seoul would be read
    nine hours off.
    """

    return (
        datetime.fromtimestamp(epoch, timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def parse_iso8601_utc(raw: str) -> datetime:
    """Parse an ISO-8601 instant, **refusing a naive one**.

    This function exists because of one specific bug class. ``expires_at``
    arrives as ``2026-08-04T10:02:00Z`` and is compared against ``time.time()``
    (epoch seconds, UTC). If the parse silently produced a *naive* datetime,
    ``.timestamp()`` would interpret it in the box's **local** zone — on the
    Orange Pi that is Asia/Seoul, i.e. nine hours early, so every command would
    look long expired and no plant would ever be watered. Written the other way
    round (``datetime.utcnow() < naive``) the same naivety makes commands look
    nine hours *younger*, so nothing would ever expire and a delayed command
    would still run. The failure is silent in both directions and the sign
    depends on which side of the comparison the naive value lands.

    So a value with no offset is a hard error rather than an assumption: the
    contract says the instant is qualified, and guessing would pick one of the
    two failure modes above.
    """

    if not isinstance(raw, str) or not raw.strip():
        raise ProtocolError("timestamp must be a non-empty string")
    text = raw.strip()
    # fromisoformat only learned to accept a trailing Z in 3.11, and the
    # deployed Orange Pi image is 3.10.
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        value = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ProtocolError(f"timestamp is not ISO-8601: {raw!r}") from exc
    if value.tzinfo is None or value.utcoffset() is None:
        raise ProtocolError(
            f"timestamp {raw!r} carries no UTC offset; refusing to guess one"
        )
    return value.astimezone(timezone.utc)


@dataclass(frozen=True)
class SerialAck:
    """One ``{"t":"ack", ...}`` line, exactly as the firmware phrased it.

    Deliberately not translated: ``stop_cause`` holds the firmware's own
    lowercase token (``cooldown``, ``watchdog``, ``busy``, ...) verbatim. The
    three layers of this system use three different reason vocabularies, and the
    only way a diagnosis survives two translations is for the raw token to be
    carried through untouched.
    """

    command_id: str
    phase: str
    runtime_ms: int | None = None
    stop_cause: str | None = None
    # Only if the firmware measures it. Absent is the normal case; the relay
    # then derives an estimate from the commanded volume and the runtime.
    volume_ml: int | None = None


# The strictness split below is deliberate, and it is a safety judgement rather
# than a style one. ``id`` and ``ph`` are what make an ack *mean* something: an
# ack that cannot be correlated, or whose phase is unknown, cannot be applied to
# any command and is rejected. Everything else is detail, and dropping the whole
# frame over a bad detail is the worse outcome — the backend would then expire
# the command and charge its granted volume to the daily budget, so the pot is
# billed for water whose actual amount we merely failed to parse.


def _lenient_token(message: dict[str, Any], name: str) -> str | None:
    value = message.get(name)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        LOGGER.warning("ignoring unusable ack field %s=%r", name, value)
        return None
    return value.strip()[:STOP_CAUSE_MAX_CHARS]


def _lenient_uint32(message: dict[str, Any], name: str) -> int | None:
    try:
        return _optional_uint32(message, name)
    except ProtocolError:
        LOGGER.warning(
            "ignoring unusable ack field %s=%r", name, message.get(name)
        )
        return None


def parse_serial_ack(message: dict[str, Any]) -> SerialAck:
    """Validate one decoded short-key ack frame.

    Takes the decoded dict rather than bytes because ``service._ingest_line``
    has already decoded it to route on the envelope key — the asymmetric-link
    trap documented there. Telemetry keeps its bytes-only parser; an ack has no
    such requirement because its envelope key *is* the routing decision.
    """

    if message.get("t") != "ack":
        raise ProtocolError("frame is not an ack")
    command_id = message.get("id")
    if not isinstance(command_id, str) or not 1 <= len(command_id.strip()) <= 64:
        raise ProtocolError("ack id must be a 1..64 character string")
    phase = message.get("ph")
    if phase not in ACK_PHASES:
        raise ProtocolError(f"ack ph must be one of {ACK_PHASES}")
    # ``r`` on a rejection, ``stop`` on a completion or abort. Both name why,
    # and both land in the same field: the backend stores one free-text cause.
    stop_cause = _lenient_token(message, "stop") or _lenient_token(message, "r")
    return SerialAck(
        command_id=command_id.strip(),
        phase=str(phase),
        runtime_ms=_lenient_uint32(message, "ms"),
        stop_cause=stop_cause,
        volume_ml=_lenient_uint32(message, "ml"),
    )


@dataclass(frozen=True)
class CommandAck:
    """A command outcome on its way to the backend, durable in the outbox.

    Queued rather than published directly for the same reason telemetry is: the
    broker may be unreachable at the exact moment a pump stops. An ack that is
    merely logged becomes a phantom budget deduction — the backend expires the
    command and charges its granted volume to the daily budget anyway, so the
    pot is billed for water it may never have received.

    ``gateway_id`` is a publish-time parameter, not a field, matching
    :meth:`Event.envelope_v2`: it is deployment-wide config rather than a
    property of this outcome.
    """

    command_id: str
    phase: str
    at_utc: str
    reason: str
    correlation_id: str | None = None
    node_id: str | None = None
    pot_id: int | None = None
    runtime_ms: int | None = None
    estimated_ml: int | None = None
    stop_cause: str | None = None

    @property
    def event_id(self) -> str:
        """Outbox primary key, derived rather than random.

        This is the pi-side half of the QoS 1 duplicate defence. A repeated
        firmware ack, or the same line re-ingested after a reconnect, collapses
        onto this key and the outbox's ``INSERT OR IGNORE`` makes the second
        enqueue a no-op. A random uuid here would publish the same outcome twice
        and the backend would see two transitions for one physical event.

        Keyed by phase as well as command, because the four phases are four
        distinct outcomes of the same command and all four must reach the server.
        """

        return f"ack:{self.command_id}:{self.phase}"

    def ack_payload(self, *, gateway_id: str) -> dict[str, object]:
        body: dict[str, object] = {
            "schema_version": 2,
            "message_type": "command_ack",
            "command_id": self.command_id,
            "gateway_id": gateway_id,
            "phase": self.phase,
            "at": self.at_utc,
            "reason": self.reason,
        }
        # Echoed only when known. A rejection built from a command the pi could
        # barely parse still carries command_id, which is all the backend needs
        # to resolve the pot; omitting the rest says "unknown" where a null or a
        # zero would read as a decision.
        if self.correlation_id is not None:
            body["correlation_id"] = self.correlation_id
        if self.node_id is not None:
            body["node_id"] = self.node_id
        if self.pot_id is not None:
            body["pot_id"] = self.pot_id
        actual: dict[str, object] = {}
        if self.runtime_ms is not None:
            actual["runtime_ms"] = self.runtime_ms
        if self.estimated_ml is not None:
            actual["estimated_ml"] = self.estimated_ml
        if self.stop_cause is not None:
            actual["stop_cause"] = self.stop_cause
        if actual:
            body["actual"] = actual
        return body

    def to_record(self) -> dict[str, object]:
        return asdict(self)

    @classmethod
    def from_record(cls, record: dict[str, Any]) -> "CommandAck":
        return cls(**record)


# What the durable queue can hold. Two families, two dataclasses, and the outbox
# picks the decoder from the row's ``kind`` — see outbox._RECORD_CODECS.
QueuedMessage = Event | CommandAck
