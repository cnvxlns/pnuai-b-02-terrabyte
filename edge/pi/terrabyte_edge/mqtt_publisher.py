"""MQTT transport for the backend telemetry contract (envelope v2).

Operational transport per design doc §6.3/§6.5/§8.1: publishes to
``{prefix}/{gateway_id}/up/telemetry`` and announces presence on
``{prefix}/{gateway_id}/up/status`` via a Last Will (offline) plus a retained
"online" publish on connect.
"""

from __future__ import annotations

import json
import logging
import threading
from typing import Any, Callable

from .protocol import Event
from .publisher import Delivery, DeliveryResult

LOGGER = logging.getLogger(__name__)

try:
    import paho.mqtt.client as mqtt
except ImportError:  # pragma: no cover - exercised only when paho-mqtt is absent
    # Kept import-safe so the rest of the package (and the test suite, via an
    # injected client_factory) works in environments without paho-mqtt
    # installed. Constructing MqttPublisher for real still requires it.
    mqtt = None  # type: ignore[assignment]


ONLINE_PAYLOAD = json.dumps({"online": True}, separators=(",", ":")).encode("utf-8")
OFFLINE_PAYLOAD = json.dumps({"online": False}, separators=(",", ":")).encode("utf-8")


class MqttPublisher:
    """Publishes envelope v2 telemetry over MQTT with LWT-based presence.

    ``Delivery.DEAD`` vs ``Delivery.RETRY``: MQTT has no 4xx-equivalent
    response. A PUBACK only confirms the broker accepted the bytes onto the
    topic — it never means "this payload is permanently invalid" the way an
    HTTP 400 did in the old ``BackendClient``. That quarantine signal is
    gone with HTTP. So ``Delivery.DEAD`` here is reserved *only* for a local
    schema-validation failure raised while building the envelope, before
    anything is sent to the broker. Every broker/transport-level failure —
    not connected, PUBACK timeout, socket error — is ``Delivery.RETRY``,
    because there is no way to tell "never going to work" apart from
    "temporarily unavailable" once the bytes are only judged by the broker.
    """

    def __init__(
        self,
        *,
        host: str,
        port: int,
        gateway_id: str,
        topic_prefix: str,
        username: str | None = None,
        password: str | None = None,
        tls: bool = False,
        tls_ca_cert: str | None = None,
        keepalive_seconds: int = 30,
        publish_timeout_seconds: float = 10.0,
        client_factory: Callable[[], Any] | None = None,
    ) -> None:
        if mqtt is None and client_factory is None:
            raise RuntimeError(
                "paho-mqtt is not installed; add paho-mqtt to requirements.txt"
            )
        self._gateway_id = gateway_id
        self._telemetry_topic = f"{topic_prefix}/{gateway_id}/up/telemetry"
        self._status_topic = f"{topic_prefix}/{gateway_id}/up/status"
        self._publish_timeout_seconds = publish_timeout_seconds
        self._connected = threading.Event()
        self._publish_reasons: dict[int, Any] = {}
        self._publish_lock = threading.Lock()

        # MQTT v5, not 3.1.1, for one specific reason: a broker that refuses a
        # publish on ACL grounds still returns a PUBACK under 3.1.1, so a
        # gateway misconfigured to publish outside its own namespace sees
        # "delivered", drops the event from the outbox, and loses the data with
        # no error anywhere on this side. v5 carries a reason code on the PUBACK
        # ("Not authorized"), which makes that failure detectable — see
        # _publish_failure below. Silent success is the exact failure mode the
        # v1 contract had, and it must not be reintroduced here.
        factory = client_factory or (
            lambda: mqtt.Client(
                mqtt.CallbackAPIVersion.VERSION2, protocol=mqtt.MQTTv5
            )
        )
        self._client = factory()

        # The will must be registered before connect() — brokers only honour
        # a will captured at CONNECT time, and a dropped connection (the
        # whole point of the will) happens after that.
        self._client.will_set(self._status_topic, OFFLINE_PAYLOAD, qos=1, retain=True)
        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        self._client.on_publish = self._on_publish
        if username:
            self._client.username_pw_set(username, password)
        if tls:
            # Without a CA file paho trusts only the system bundle, which never
            # contains a self-signed broker certificate — so TB_MQTT_TLS would
            # be unusable against the demo broker. Passing the CA explicitly is
            # what makes a self-signed deployment work, and it still verifies
            # the certificate rather than disabling the check.
            self._client.tls_set(ca_certs=tls_ca_cert)
        # paho's own reconnect backoff keeps the socket from hammering the
        # broker; the outbox's exponential backoff in service.py remains the
        # authoritative retry schedule at the application level.
        self._client.reconnect_delay_set(min_delay=1, max_delay=30)
        # connect_async, never connect: a synchronous connect raises when the
        # broker is unreachable, which would abort service start-up. The edge
        # is expected to boot before the broker is reachable — that is exactly
        # what the durable outbox exists for — so an unreachable broker has to
        # degrade into retries, not kill the process. loop_start() drives the
        # connection and paho's reconnect backoff in the background.
        self._client.connect_async(host, port, keepalive=keepalive_seconds)
        self._client.loop_start()

    def _on_disconnect(self, client, userdata, *args) -> None:
        self._connected.clear()

    def _on_publish(self, client, userdata, mid, reason_code=None, properties=None) -> None:
        with self._publish_lock:
            self._publish_reasons[mid] = reason_code

    def _publish_failure(self, mid: int) -> str | None:
        """Return the broker's rejection reason for ``mid``, or None if it accepted.

        A v5 reason code of 0x80 or above is a refusal. The common one here is
        0x87 "Not authorized", meaning the ACL blocked the topic — almost always
        a gateway id that disagrees with the MQTT credentials it is using.
        """

        with self._publish_lock:
            reason = self._publish_reasons.pop(mid, None)
        if reason is None:
            return None
        is_failure = getattr(reason, "is_failure", None)
        if is_failure:
            return str(reason)
        value = getattr(reason, "value", None)
        if isinstance(value, int) and value >= 0x80:
            return str(reason)
        return None

    def _on_connect(self, client, userdata, flags, reason_code, properties=None) -> None:
        # Retained so a subscriber that connects later still learns the
        # gateway is online instead of waiting for the next sample.
        # Commands (dn/command) must NEVER be retained — a retained command
        # would re-execute stale irrigation on every reconnect. Only this
        # up/status publish is retained.
        client.publish(self._status_topic, ONLINE_PAYLOAD, qos=1, retain=True)
        self._connected.set()

    def send(self, event: Event) -> DeliveryResult:
        try:
            body = event.envelope_v2(gateway_id=self._gateway_id)
        except Exception as exc:  # local schema-validation failure only
            return DeliveryResult(Delivery.DEAD, f"invalid_envelope:{exc}")

        payload = json.dumps(body, separators=(",", ":")).encode("utf-8")

        # Fail fast while the session is down. Without this, publishing to a
        # disconnected client queues the message locally and the caller waits
        # the whole PUBACK timeout before learning nothing happened — which
        # also mislabels an authentication rejection as "puback_timeout".
        is_connected = getattr(self._client, "is_connected", None)
        if callable(is_connected) and not is_connected():
            return DeliveryResult(Delivery.RETRY, "not_connected")

        try:
            info = self._client.publish(
                self._telemetry_topic, payload, qos=1, retain=False
            )
        except Exception as exc:  # broker/socket errors, however paho raises them
            return DeliveryResult(Delivery.RETRY, type(exc).__name__)

        rc = getattr(info, "rc", 0)
        if rc != 0:
            return DeliveryResult(Delivery.RETRY, f"mqtt_rc_{rc}")

        try:
            info.wait_for_publish(timeout=self._publish_timeout_seconds)
        except (ValueError, RuntimeError):
            # paho raises when wait_for_publish() times out before a PUBACK.
            return DeliveryResult(Delivery.RETRY, "puback_timeout")

        if not info.is_published():
            return DeliveryResult(Delivery.RETRY, "puback_timeout")

        rejection = self._publish_failure(getattr(info, "mid", -1))
        if rejection is not None:
            # RETRY, not DEAD: the payload is fine and the fix is operational
            # (ACL or credentials). Keeping it queued means a corrected config
            # drains the backlog, whereas quarantining would discard readings
            # that were never actually invalid. If it stays broken the outbox
            # fills and logs CRITICAL — loud, but not silent data loss.
            LOGGER.error(
                "broker rejected publish topic=%s reason=%s", self._telemetry_topic, rejection
            )
            return DeliveryResult(Delivery.RETRY, f"rejected:{rejection}")
        return DeliveryResult(Delivery.DELIVERED, "puback")

    def close(self) -> None:
        self._client.loop_stop()
        self._client.disconnect()
