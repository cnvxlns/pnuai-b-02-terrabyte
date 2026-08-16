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
    """Raised for valid protocol messages that carry no measurements."""


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
    ppfd_umol_m2_s: float | None

    def backend_body(self) -> dict[str, object]:
        return {
            "capturedAtUtc": self.captured_at_utc,
            "airTemperatureC": self.air_temperature_c,
            "relativeHumidityPct": self.relative_humidity_pct,
            "ppfdUmolM2S": self.ppfd_umol_m2_s,
            "inputContract": "perfect_calibrated_v1",
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


def _nullable_reading(
    message: dict[str, Any], name: str, minimum: float, maximum: float
) -> float | None:
    if name not in message:
        raise ProtocolError(f"{name} is required")
    if message[name] is None:
        return None
    return _reading(message, name, minimum, maximum)


def parse_line(
    line: bytes,
    *,
    context_id: str,
    expected_node_id: str,
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
        raise NonTelemetryMessage(str(message_type))
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
    if node_id != expected_node_id:
        raise ProtocolError("node_id does not match the provisioned Arduino")
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
        ppfd_umol_m2_s=_nullable_reading(
            message, "ppfd_umol_m2_s", 0.0, 5000.0
        ),
    )
