"""Environment-backed service configuration."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import os
from pathlib import Path
import re
from typing import Callable, Mapping, TypeVar
from urllib.parse import urlparse

from .irrigation.volume import SUPPORTED_CROP_CODES


class ConfigError(ValueError):
    """Raised when required configuration is missing or invalid."""


NODE_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")


def _required(env: Mapping[str, str], name: str) -> str:
    value = env.get(name, "").strip()
    if not value:
        raise ConfigError(f"{name} must be set")
    return value


def _integer(
    env: Mapping[str, str], name: str, default: int, *, minimum: int = 1
) -> int:
    raw = env.get(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer") from exc
    if value < minimum:
        raise ConfigError(f"{name} must be at least {minimum}")
    return value


def _number(
    env: Mapping[str, str], name: str, default: float, *, minimum: float = 0.0
) -> float:
    raw = env.get(name, str(default)).strip()
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number") from exc
    if value < minimum:
        raise ConfigError(f"{name} must be at least {minimum}")
    return value


def _boolean(env: Mapping[str, str], name: str, default: bool = False) -> bool:
    raw = env.get(name, "true" if default else "false").strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    raise ConfigError(f"{name} must be true or false")


Value = TypeVar("Value")


def _node_keyed_map(
    env: Mapping[str, str],
    name: str,
    *,
    known_node_ids: frozenset[str],
    parse_value: Callable[[str], Value],
) -> dict[str, Value]:
    """Parse ``node-a:value,node-b:value`` into a mapping keyed by node id.

    Keyed rather than positional. A bare comma-separated list would have to be
    matched against the node allowlist by position, and a reordering there
    would hand one pot's settings to another pot without anything looking
    wrong. The node id travels with its value instead.

    Split on the *last* colon: node ids legitimately contain colons (the
    firmware's safe set includes ``:``, e.g. ``node_A-1.2:usb``) while neither
    a volume nor a crop code does.

    An entry naming a node this gateway does not serve is rejected, not
    ignored. It is a typo, and a typo that sizes doses for the wrong pot is the
    exact failure this configuration exists to prevent — so it must stop the
    service at startup rather than quietly do nothing.
    """

    raw = env.get(name, "").strip()
    if not raw:
        return {}
    parsed: dict[str, Value] = {}
    for chunk in raw.split(","):
        entry = chunk.strip()
        if not entry:
            continue
        node_id, separator, value = entry.rpartition(":")
        node_id = node_id.strip()
        if not separator or not node_id:
            raise ConfigError(f"{name} entries must be node_id:value")
        if NODE_ID.fullmatch(node_id) is None:
            raise ConfigError(f"{name} has a node id with unsupported characters")
        if node_id not in known_node_ids:
            raise ConfigError(
                f"{name} names node id {node_id!r}, which this gateway does not "
                "serve; expected one of " + ", ".join(sorted(known_node_ids))
            )
        if node_id in parsed:
            raise ConfigError(f"{name} lists node id {node_id!r} twice")
        parsed[node_id] = parse_value(value.strip())
    return parsed


def _substrate_ml(name: str) -> Callable[[str], int]:
    def parse(raw: str) -> int:
        try:
            value = int(raw)
        except ValueError as exc:
            raise ConfigError(f"{name} volumes must be whole millilitres") from exc
        if value <= 0:
            raise ConfigError(f"{name} volumes must be positive")
        return value

    return parse


def _crop_code(name: str) -> Callable[[str], str]:
    def parse(raw: str) -> str:
        if raw not in SUPPORTED_CROP_CODES:
            # An unrecognised code would silently fall back to the default
            # moisture target, which is indistinguishable from a correctly
            # configured default crop. Fail instead.
            raise ConfigError(
                f"{name} has unknown crop code {raw!r}; supported: "
                + ", ".join(sorted(SUPPORTED_CROP_CODES))
            )
        return raw

    return parse


def _utc_timestamp(env: Mapping[str, str], name: str, default: str) -> datetime:
    raw = env.get(name, default).strip()
    if not raw.endswith("Z"):
        raise ConfigError(f"{name} must be an ISO-8601 UTC timestamp ending in Z")
    try:
        value = datetime.fromisoformat(raw[:-1] + "+00:00")
    except ValueError as exc:
        raise ConfigError(f"{name} must be an ISO-8601 UTC timestamp") from exc
    return value.astimezone(timezone.utc)


def _load_token(env: Mapping[str, str], *, required: bool) -> str:
    direct = env.get("TB_DEVICE_TOKEN", "").strip()
    token_file = env.get("TB_DEVICE_TOKEN_FILE", "").strip()
    if direct and token_file:
        raise ConfigError(
            "set at most one of TB_DEVICE_TOKEN or TB_DEVICE_TOKEN_FILE"
        )
    if not direct and not token_file:
        if required:
            raise ConfigError(
                "set exactly one of TB_DEVICE_TOKEN or TB_DEVICE_TOKEN_FILE"
            )
        # Under MQTT transport there is no HTTP fallback in play, so the
        # device token is unused; leave it empty rather than forcing
        # MQTT-only deployments to provision an HTTP credential they never
        # send.
        return ""
    if direct:
        return direct
    try:
        token = Path(token_file).read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise ConfigError("cannot read TB_DEVICE_TOKEN_FILE") from exc
    if not token:
        raise ConfigError("TB_DEVICE_TOKEN_FILE is empty")
    return token


@dataclass(frozen=True)
class Settings:
    serial_port: str
    serial_baud: int
    serial_timeout_seconds: float
    serial_reconnect_seconds: float
    serial_max_line_bytes: int
    database_path: Path
    transport: str
    backend_base_url: str
    crop_context_id: str
    device_id: str
    expected_node_id: str
    device_token: str
    clock_minimum_utc: datetime
    http_timeout_seconds: float
    upload_batch_size: int
    outbox_max_rows: int
    upload_interval_seconds: float
    retry_base_seconds: float
    retry_max_seconds: float
    log_level: str
    mqtt_host: str
    mqtt_port: int
    mqtt_username: str | None
    mqtt_password: str | None
    mqtt_tls: bool
    mqtt_tls_ca_cert: str | None
    mqtt_topic_prefix: str
    mqtt_keepalive_seconds: int
    mqtt_publish_timeout_seconds: float
    # Physical config the edge is authoritative for: which pot is plugged in is
    # something the person at the bench knows and the server only records. Both
    # maps are optional — a node missing from them simply gets no suggestion
    # (or the default crop target), which the backend answers with its pot-size
    # fallback table (docs/design/irrigation_volume.md §2, §3.2).
    pot_substrate_ml: dict[str, int]
    pot_crop_codes: dict[str, str]

    def substrate_volume_ml_for(self, node_id: str) -> int | None:
        return self.pot_substrate_ml.get(node_id)

    def crop_code_for(self, node_id: str) -> str | None:
        return self.pot_crop_codes.get(node_id)

    def mqtt_telemetry_topic(self) -> str:
        return f"{self.mqtt_topic_prefix}/{self.device_id}/up/telemetry"

    def mqtt_status_topic(self) -> str:
        return f"{self.mqtt_topic_prefix}/{self.device_id}/up/status"

    def telemetry_url(self) -> str:
        """Envelope v2 debug/fallback endpoint.

        The same path and the same body the MQTT subscriber consumes. v1 aimed
        at a per-crop-context observation URL that the backend never exposed,
        which is why no telemetry could arrive over HTTP at all.
        """

        return f"{self.backend_base_url}/api/telemetry"

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Settings":
        values = os.environ if env is None else env

        transport = values.get("TB_TRANSPORT", "mqtt").strip().lower()
        if transport not in {"mqtt", "http"}:
            raise ConfigError("TB_TRANSPORT must be mqtt or http")

        # TB_BACKEND_BASE_URL / TB_DEVICE_TOKEN are the HTTP publisher's
        # settings. Under the default MQTT transport there is no HTTP
        # fallback in play, so an MQTT-only deployment must not be forced to
        # provision unused HTTP credentials.
        if transport == "http":
            base_url = _required(values, "TB_BACKEND_BASE_URL").rstrip("/")
            parsed = urlparse(base_url)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                raise ConfigError("TB_BACKEND_BASE_URL must be an HTTP(S) URL")
            if parsed.scheme != "https" and not _boolean(
                values, "TB_ALLOW_INSECURE_HTTP"
            ):
                raise ConfigError(
                    "HTTP backend requires TB_ALLOW_INSECURE_HTTP=true; use HTTPS in production"
                )
            device_token = _load_token(values, required=True)
        else:
            raw_base_url = values.get("TB_BACKEND_BASE_URL", "").strip()
            base_url = raw_base_url.rstrip("/") if raw_base_url else ""
            if base_url:
                parsed = urlparse(base_url)
                if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                    raise ConfigError("TB_BACKEND_BASE_URL must be an HTTP(S) URL")
            device_token = _load_token(values, required=False)

        if transport == "mqtt":
            mqtt_host = _required(values, "TB_MQTT_HOST")
        else:
            mqtt_host = values.get("TB_MQTT_HOST", "").strip()
        mqtt_username = values.get("TB_MQTT_USERNAME", "").strip() or None
        mqtt_password = values.get("TB_MQTT_PASSWORD", "").strip() or None
        mqtt_topic_prefix = values.get("TB_MQTT_TOPIC_PREFIX", "tb/v2").strip().rstrip("/")
        if not mqtt_topic_prefix:
            raise ConfigError("TB_MQTT_TOPIC_PREFIX must not be empty")

        log_level = values.get("TB_LOG_LEVEL", "INFO").strip().upper()
        if log_level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ConfigError("TB_LOG_LEVEL is invalid")

        retry_base = _number(values, "TB_RETRY_BASE_SECONDS", 2.0, minimum=0.1)
        retry_max = _number(values, "TB_RETRY_MAX_SECONDS", 300.0, minimum=0.1)
        if retry_max < retry_base:
            raise ConfigError("TB_RETRY_MAX_SECONDS must not be below retry base")

        expected_node_id = _required(values, "TB_EXPECTED_NODE_ID")
        if NODE_ID.fullmatch(expected_node_id) is None:
            raise ConfigError("TB_EXPECTED_NODE_ID contains unsupported characters")

        # One Arduino per gateway today, so the allowlist has one member. Kept
        # as a set because the pot maps are written to survive a multi-node
        # gateway without their parsing changing.
        known_node_ids = frozenset({expected_node_id})

        return cls(
            serial_port=_required(values, "TB_SERIAL_PORT"),
            serial_baud=_integer(values, "TB_SERIAL_BAUD", 115200),
            serial_timeout_seconds=_number(
                values, "TB_SERIAL_TIMEOUT_SECONDS", 1.0, minimum=0.1
            ),
            serial_reconnect_seconds=_number(
                values, "TB_SERIAL_RECONNECT_SECONDS", 2.0, minimum=0.1
            ),
            serial_max_line_bytes=_integer(
                values, "TB_SERIAL_MAX_LINE_BYTES", 4096, minimum=128
            ),
            database_path=Path(
                values.get(
                    "TB_DATABASE_PATH", "/var/lib/terrabyte-edge/outbox.sqlite3"
                )
            ),
            transport=transport,
            backend_base_url=base_url,
            crop_context_id=_required(values, "TB_CROP_CONTEXT_ID"),
            device_id=_required(values, "TB_DEVICE_ID"),
            expected_node_id=expected_node_id,
            device_token=device_token,
            clock_minimum_utc=_utc_timestamp(
                values, "TB_CLOCK_MINIMUM_UTC", "2025-01-01T00:00:00Z"
            ),
            http_timeout_seconds=_number(
                values, "TB_HTTP_TIMEOUT_SECONDS", 10.0, minimum=0.1
            ),
            upload_batch_size=_integer(values, "TB_UPLOAD_BATCH_SIZE", 20),
            outbox_max_rows=_integer(values, "TB_OUTBOX_MAX_ROWS", 100_000),
            upload_interval_seconds=_number(
                values, "TB_UPLOAD_INTERVAL_SECONDS", 2.0, minimum=0.1
            ),
            retry_base_seconds=retry_base,
            retry_max_seconds=retry_max,
            log_level=log_level,
            mqtt_host=mqtt_host,
            mqtt_port=_integer(values, "TB_MQTT_PORT", 1883, minimum=1),
            mqtt_username=mqtt_username,
            mqtt_password=mqtt_password,
            mqtt_tls=_boolean(values, "TB_MQTT_TLS", False),
            mqtt_tls_ca_cert=(values.get("TB_MQTT_TLS_CA_CERT", "").strip() or None),
            mqtt_topic_prefix=mqtt_topic_prefix,
            mqtt_keepalive_seconds=_integer(
                values, "TB_MQTT_KEEPALIVE_SECONDS", 30, minimum=5
            ),
            mqtt_publish_timeout_seconds=_number(
                values, "TB_MQTT_PUBLISH_TIMEOUT_SECONDS", 10.0, minimum=0.1
            ),
            pot_substrate_ml=_node_keyed_map(
                values,
                "TB_POT_SUBSTRATE_ML",
                known_node_ids=known_node_ids,
                parse_value=_substrate_ml("TB_POT_SUBSTRATE_ML"),
            ),
            pot_crop_codes=_node_keyed_map(
                values,
                "TB_POT_CROPS",
                known_node_ids=known_node_ids,
                parse_value=_crop_code("TB_POT_CROPS"),
            ),
        )
