"""Arduino JSON Lines telemetry protocol v1 validation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import math
from typing import Any, Callable
import uuid


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
        backend ``MeasurementMetric`` enum literally.
        """
        measurements = self.measurements()

        return {
            "schema_version": 2,
            "event_type": "telemetry.sample",
            "gateway_id": gateway_id,
            "event_id": self.event_id,
            "observed_at": self.captured_at_utc,
            "nodes": [
                {
                    "node_id": self.node_id,
                    "sequence": self.sequence,
                    "measurements": measurements,
                    "quality": {
                        # Ranges were already enforced by parse_line() before
                        # this Event was constructed, so every reading present
                        # here is valid by construction. An absent soil probe
                        # reports false, matching "unusable for irrigation".
                        "air_sensor_valid": True,
                        "light_sensor_valid": True,
                        "soil_sensor_valid": self.has_soil_reading,
                    },
                }
            ],
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
        }

    @classmethod
    def from_record(cls, record: dict[str, Any]) -> "Event":
        return cls(**record)


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
