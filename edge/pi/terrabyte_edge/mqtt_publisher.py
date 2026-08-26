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
import time
from typing import Any, Callable

from .protocol import CommandAck, EdgeIrrigationRecord, Event
from .publisher import CommandHandler, Delivery, DeliveryResult

LOGGER = logging.getLogger(__name__)

try:
    import paho.mqtt.client as mqtt
except ImportError:  # pragma: no cover - exercised only when paho-mqtt is absent
    # Kept import-safe so the rest of the package (and the test suite, via an
    # injected client_factory) works in environments without paho-mqtt
    # installed. Constructing MqttPublisher for real still requires it.
    mqtt = None  # type: ignore[assignment]


# The online half is built per-publish by ``_status_payload`` because it now
# carries the link state, which changes while the process runs. The Last Will
# stays a constant: the broker sends it on our behalf when we are not around to
# build anything, so it can only say the one thing that is certainly true.
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
        max_consecutive_link_failures: int = 3,
        max_seconds_since_success: float = 90.0,
        min_rebuild_interval_seconds: float = 30.0,
        client_factory: Callable[[], Any] | None = None,
        state_provider: Callable[[], str] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if mqtt is None and client_factory is None:
            raise RuntimeError(
                "paho-mqtt is not installed; add paho-mqtt to requirements.txt"
            )
        if max_consecutive_link_failures < 1:
            raise ValueError("max_consecutive_link_failures must be at least 1")
        if max_seconds_since_success <= 0:
            raise ValueError("max_seconds_since_success must be positive")
        if min_rebuild_interval_seconds < 0:
            raise ValueError("min_rebuild_interval_seconds must not be negative")
        self._gateway_id = gateway_id
        self._telemetry_topic = f"{topic_prefix}/{gateway_id}/up/telemetry"
        self._status_topic = f"{topic_prefix}/{gateway_id}/up/status"
        self._ack_topic = f"{topic_prefix}/{gateway_id}/up/ack"
        # Water this gateway delivered with no command behind it. Its own
        # topic rather than a phase on up/ack, because there is no
        # command_id to acknowledge and the server writes it another way.
        self._irrigation_topic = f"{topic_prefix}/{gateway_id}/up/irrigation"
        self._command_topic = f"{topic_prefix}/{gateway_id}/dn/command"
        # Proof the application behind the broker is alive. See cloud_link.
        self._heartbeat_topic = f"{topic_prefix}/{gateway_id}/dn/heartbeat"
        self._host = host
        self._port = port
        self._username = username
        self._password = password
        self._tls = tls
        self._tls_ca_cert = tls_ca_cert
        self._keepalive_seconds = keepalive_seconds
        self._publish_timeout_seconds = publish_timeout_seconds
        self._max_consecutive_link_failures = max_consecutive_link_failures
        self._max_seconds_since_success = max_seconds_since_success
        self._min_rebuild_interval_seconds = min_rebuild_interval_seconds
        self._state_provider = state_provider
        self._clock = clock
        self._connected = threading.Event()
        self._publish_reasons: dict[int, Any] = {}
        self._publish_lock = threading.Lock()
        self._command_handler: CommandHandler | None = None
        self._heartbeat_handler: Callable[[], None] | None = None
        self._command_lock = threading.Lock()
        # This lock protects the active-client pointer and generation checks.
        # Never hold it across a paho operation that may wait for its network
        # thread or invoke a callback: paho can call us while holding one of
        # its own mutexes, and taking the two locks in opposite orders wedges
        # both threads. Snapshot the client here, then call paho after release.
        self._client_lock = threading.Lock()
        self._health_lock = threading.Lock()
        self._client: Any | None = None
        self._closed = False
        self._consecutive_link_failures = 0
        # Until the first PUBACK, construction is the only honest lower bound
        # on how long this generation has gone without proving the link works.
        self._last_success_at = self._clock()
        self._last_rebuild_at: float | None = None
        self._rebuild_in_progress = False

        # MQTT v5, not 3.1.1, for one specific reason: a broker that refuses a
        # publish on ACL grounds still returns a PUBACK under 3.1.1, so a
        # gateway misconfigured to publish outside its own namespace sees
        # "delivered", drops the event from the outbox, and loses the data with
        # no error anywhere on this side. v5 carries a reason code on the PUBACK
        # ("Not authorized"), which makes that failure detectable — see
        # _publish_failure below. Silent success is the exact failure mode the
        # v1 contract had, and it must not be reintroduced here.
        self._client_factory = client_factory or (
            lambda: mqtt.Client(
                mqtt.CallbackAPIVersion.VERSION2, protocol=mqtt.MQTTv5
            )
        )
        client = self._build_client()
        with self._client_lock:
            self._client = client
        self._start_client(client)

    def _build_client(self) -> Any:
        client = self._client_factory()

        # The will must be registered before connect() — brokers only honour
        # a will captured at CONNECT time, and a dropped connection (the
        # whole point of the will) happens after that.
        client.will_set(self._status_topic, OFFLINE_PAYLOAD, qos=1, retain=True)
        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect
        client.on_publish = self._on_publish
        client.on_message = self._on_message
        if self._username:
            client.username_pw_set(self._username, self._password)
        if self._tls:
            # Without a CA file paho trusts only the system bundle, which never
            # contains a self-signed broker certificate — so TB_MQTT_TLS would
            # be unusable against the demo broker. Passing the CA explicitly is
            # what makes a self-signed deployment work, and it still verifies
            # the certificate rather than disabling the check.
            client.tls_set(ca_certs=self._tls_ca_cert)
        # paho's own reconnect backoff keeps the socket from hammering the
        # broker; the outbox's exponential backoff in service.py remains the
        # authoritative retry schedule at the application level.
        client.reconnect_delay_set(min_delay=1, max_delay=30)
        return client

    def _start_client(self, client: Any) -> None:
        # connect_async, never connect: a synchronous connect raises when the
        # broker is unreachable, which would abort service start-up. The edge
        # is expected to boot before the broker is reachable — that is exactly
        # what the durable outbox exists for — so an unreachable broker has to
        # degrade into retries, not kill the process. loop_start() drives the
        # connection and paho's reconnect backoff in the background.
        client.connect_async(
            self._host, self._port, keepalive=self._keepalive_seconds
        )
        client.loop_start()

    def _on_disconnect(self, client, userdata, *args) -> None:
        with self._client_lock:
            if client is self._client:
                self._connected.clear()

    def _on_publish(self, client, userdata, mid, reason_code=None, properties=None) -> None:
        # paho invokes this callback while holding its outgoing-message mutex.
        # It must therefore never acquire _client_lock: a publisher can have
        # snapshotted this client and be waiting for that paho mutex. The plain
        # identity read is atomic in CPython, and doing it under _publish_lock
        # orders an old generation's callback with _rebuild_client's clear.
        with self._publish_lock:
            if client is not self._client:
                return
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
        with self._client_lock:
            # A stopped generation can finish a callback while its replacement
            # is starting. Letting it mark the publisher connected would route
            # the next upload to a client whose network loop is already gone.
            if client is not self._client:
                return
        client.publish(
            self._status_topic, self._status_payload(), qos=1, retain=True
        )
        # Subscriptions have to be renewed after reconnect. Store the handlers
        # independently so a broker blip cannot leave telemetry flowing while
        # silently disabling commands.
        with self._command_lock:
            command_handler = self._command_handler
            heartbeat_handler = self._heartbeat_handler
        if command_handler is not None:
            client.subscribe(self._command_topic, qos=1)
            LOGGER.info("subscribed to commands topic=%s", self._command_topic)
        # Renewed alongside commands, never instead of them: a blip that
        # restored commands but not heartbeats would leave this gateway
        # deciding the cloud is dead while it is actively being commanded.
        if heartbeat_handler is not None:
            client.subscribe(self._heartbeat_topic, qos=0)
            LOGGER.info(
                "subscribed to heartbeats topic=%s", self._heartbeat_topic
            )
        with self._client_lock:
            if client is self._client:
                self._connected.set()

    def subscribe_commands(self, handler: CommandHandler) -> None:
        """Register the relay without doing command work on paho's thread."""

        with self._command_lock:
            self._command_handler = handler
        with self._client_lock:
            client = self._client
            is_connected = getattr(client, "is_connected", None)
            connected = (
                client is not None and callable(is_connected) and is_connected()
            )
        # subscribe() enters paho and may contend with its network thread, so
        # it follows the same snapshot-then-call rule as publish(). If this
        # generation is replaced meanwhile, _on_connect renews the subscription
        # on the replacement because the handler above remains registered.
        if connected:
            client.subscribe(self._command_topic, qos=1)
            LOGGER.info("subscribed to commands topic=%s", self._command_topic)

    def subscribe_heartbeats(self, handler: Callable[[], None]) -> None:
        """Register the cloud-liveness listener. Same discipline as commands."""

        with self._command_lock:
            self._heartbeat_handler = handler
        with self._client_lock:
            client = self._client
            is_connected = getattr(client, "is_connected", None)
            connected = (
                client is not None and callable(is_connected) and is_connected()
            )
        if connected:
            client.subscribe(self._heartbeat_topic, qos=0)
            LOGGER.info("subscribed to heartbeats topic=%s", self._heartbeat_topic)

    def _status_payload(self) -> bytes:
        """The retained ``up/status`` body.

        Carries the link state when one is available, because the backend reads
        it to decide whether publishing a command is safe at all: a gateway in
        RESYNC owes it irrigation records it has not seen yet.
        """

        body: dict[str, object] = {"online": True}
        provider = self._state_provider
        if provider is not None:
            try:
                body["state"] = str(provider())
            except Exception:
                # Status must go out even when the state machine is unhappy.
                # "Online with no state" degrades better than no status at all,
                # because up/status is also what clears a stale Last Will.
                LOGGER.exception("link state provider raised; omitting state")
        return json.dumps(body, separators=(",", ":")).encode("utf-8")

    def publish_status(self) -> None:
        """Re-announce the retained status, for when the link state changes."""

        with self._client_lock:
            client = self._client
            is_connected = getattr(client, "is_connected", None)
            connected = (
                client is not None and callable(is_connected) and is_connected()
            )
        if not connected:
            return
        # Called outside the lock, like every other entry into paho here:
        # holding _client_lock across publish() is the AB-BA deadlock this file
        # has already been fixed for once.
        client.publish(self._status_topic, self._status_payload(), qos=1, retain=True)

    def _on_message(self, client, userdata, message) -> None:
        with self._client_lock:
            if client is not self._client:
                return
        topic = getattr(message, "topic", "") or ""
        with self._command_lock:
            command_handler = self._command_handler
            heartbeat_handler = self._heartbeat_handler

        # Routed by topic rather than by payload shape. Handing a heartbeat to
        # the relay would have it publish a rejected ack for a command_id that
        # does not exist, and counting a command as a heartbeat would let a
        # queued command from a since-dead application look like liveness.
        if topic == self._heartbeat_topic:
            if heartbeat_handler is None:
                return
            try:
                heartbeat_handler()
            except Exception:
                LOGGER.exception("heartbeat handler raised; dropping message")
            return

        if command_handler is None:
            return
        try:
            command_handler(
                bytes(message.payload or b""),
                bool(getattr(message, "retain", False)),
            )
        except Exception:
            # A relay bug must not kill paho's network loop and telemetry path.
            LOGGER.exception("command handler raised; dropping message")

    def send(self, event: Event) -> DeliveryResult:
        try:
            body = event.envelope_v2(gateway_id=self._gateway_id)
        except Exception as exc:  # local schema-validation failure only
            return DeliveryResult(Delivery.DEAD, f"invalid_envelope:{exc}")

        return self._publish(self._telemetry_topic, body)

    def send_ack(self, ack: CommandAck) -> DeliveryResult:
        try:
            body = ack.ack_payload(gateway_id=self._gateway_id)
        except Exception as exc:  # local schema-validation failure only
            return DeliveryResult(Delivery.DEAD, f"invalid_envelope:{exc}")
        return self._publish(self._ack_topic, body)

    def send_edge_irrigation(self, record: EdgeIrrigationRecord) -> DeliveryResult:
        try:
            body = record.payload(gateway_id=self._gateway_id)
        except Exception as exc:  # local schema-validation failure only
            return DeliveryResult(Delivery.DEAD, f"invalid_envelope:{exc}")
        return self._publish(self._irrigation_topic, body)

    def _publish(self, topic: str, body: dict[str, object]) -> DeliveryResult:
        """Publish one QoS 1 message, never retained, and judge its PUBACK."""

        payload = json.dumps(body, separators=(",", ":")).encode("utf-8")

        # Fail fast while the session is down. Without this, publishing to a
        # disconnected client queues the message locally and the caller waits
        # the whole PUBACK timeout before learning nothing happened — which
        # also mislabels an authentication rejection as "puback_timeout".
        with self._client_lock:
            client = self._client
            is_connected = getattr(client, "is_connected", None)
            connected = client is not None and not (
                callable(is_connected) and not is_connected()
            )

        if not connected:
            self._record_link_failure("not_connected")
            return DeliveryResult(Delivery.RETRY, "not_connected")

        # publish() can wait for paho's outgoing-message mutex. Its network
        # thread holds that mutex while invoking _on_publish, so _client_lock
        # must be released before entering paho or the two threads deadlock.
        try:
            info = client.publish(topic, payload, qos=1, retain=False)
        except Exception as exc:
            # Broker/socket errors arrive as whichever exception the
            # installed paho version chooses to expose.
            return DeliveryResult(Delivery.RETRY, type(exc).__name__)

        rc = getattr(info, "rc", 0)
        if rc != 0:
            return DeliveryResult(Delivery.RETRY, f"mqtt_rc_{rc}")

        try:
            info.wait_for_publish(timeout=self._publish_timeout_seconds)
        except (ValueError, RuntimeError):
            # paho raises when wait_for_publish() times out before a PUBACK.
            self._record_link_failure("puback_timeout")
            return DeliveryResult(Delivery.RETRY, "puback_timeout")

        if not info.is_published():
            self._record_link_failure("puback_timeout")
            return DeliveryResult(Delivery.RETRY, "puback_timeout")

        rejection = self._publish_failure(getattr(info, "mid", -1))
        if rejection is not None:
            # RETRY, not DEAD: the payload is fine and the fix is operational
            # (ACL or credentials). Keeping it queued means a corrected config
            # drains the backlog, whereas quarantining would discard readings
            # that were never actually invalid. If it stays broken the outbox
            # fills and logs CRITICAL — loud, but not silent data loss.
            LOGGER.error(
                "broker rejected publish topic=%s reason=%s", topic, rejection
            )
            return DeliveryResult(Delivery.RETRY, f"rejected:{rejection}")
        self._record_publish_success()
        return DeliveryResult(Delivery.DELIVERED, "puback")

    def _record_publish_success(self) -> None:
        with self._health_lock:
            self._consecutive_link_failures = 0
            self._last_success_at = self._clock()

    def _record_link_failure(self, failure: str) -> None:
        now = self._clock()
        with self._health_lock:
            self._consecutive_link_failures += 1
            dead_seconds = max(0.0, now - self._last_success_at)
            tripped = []
            if (
                self._consecutive_link_failures
                >= self._max_consecutive_link_failures
            ):
                tripped.append(
                    "consecutive_link_failures"
                    f"({self._consecutive_link_failures}>="
                    f"{self._max_consecutive_link_failures})"
                )
            if dead_seconds >= self._max_seconds_since_success:
                tripped.append(
                    "seconds_since_success"
                    f"({dead_seconds:.1f}>={self._max_seconds_since_success:.1f})"
                )
            if not tripped or self._rebuild_in_progress:
                return
            if (
                self._last_rebuild_at is not None
                and now - self._last_rebuild_at
                < self._min_rebuild_interval_seconds
            ):
                return
            # Reserve the interval before touching paho. Two uploader threads
            # can discover the same dead client together, and only one of them
            # may be allowed to replace it.
            self._last_rebuild_at = now
            self._rebuild_in_progress = True

        LOGGER.warning(
            "rebuilding stalled MQTT client failure=%s threshold=%s "
            "link_dead_seconds=%.1f",
            failure,
            ",".join(tripped),
            dead_seconds,
        )
        try:
            self._rebuild_client()
        finally:
            with self._health_lock:
                self._consecutive_link_failures = 0
                self._last_rebuild_at = self._clock()
                self._rebuild_in_progress = False

    def _rebuild_client(self) -> None:
        # Detach before stopping the loop so uploads fail fast instead of racing
        # a generation whose socket and callback thread are being dismantled.
        with self._client_lock:
            old_client = self._client
            self._client = None
            self._connected.clear()

        if old_client is not None:
            self._stop_client(old_client)
        with self._publish_lock:
            self._publish_reasons.clear()

        new_client = None
        try:
            new_client = self._build_client()
            with self._client_lock:
                if self._closed:
                    return
                self._client = new_client
            # loop_start() launches the paho thread, which may immediately run
            # a callback. Starting it while holding _client_lock would make
            # that thread wait on us from inside paho, violating the same lock
            # boundary as publish() and subscribe().
            try:
                self._start_client(new_client)
            except Exception:
                with self._client_lock:
                    if self._client is new_client:
                        self._client = None
                raise
            with self._client_lock:
                orphaned = self._client is not new_client
            if orphaned:
                # close() may detach the generation while startup is in paho.
                # Stop it again after startup returns so that race cannot leave
                # a network loop running after the publisher has been closed.
                self._stop_client(new_client)
        except Exception:
            # A failed repair still observes the rebuild interval. Otherwise a
            # broken local TLS file or exhausted resource can turn every outbox
            # attempt into a tight client-construction loop.
            if new_client is not None:
                self._stop_client(new_client)
            LOGGER.exception("failed to rebuild MQTT client")

    @staticmethod
    def _stop_client(client: Any) -> None:
        try:
            client.loop_stop()
        except Exception:
            LOGGER.debug("MQTT loop_stop failed during teardown", exc_info=True)
        try:
            client.disconnect()
        except Exception:
            LOGGER.debug("MQTT disconnect failed during teardown", exc_info=True)

    def close(self) -> None:
        with self._client_lock:
            self._closed = True
            client = self._client
            self._client = None
            self._connected.clear()
        if client is not None:
            self._stop_client(client)
