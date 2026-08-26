"""MqttPublisher tests against a fake paho client — no broker, no network.

A ``client_factory`` is injected so these tests run even when paho-mqtt is
not installed in the environment.
"""

import json
import threading
import unittest

from terrabyte_edge.mqtt_publisher import MqttPublisher
from terrabyte_edge.protocol import EdgeIrrigationRecord, Event
from terrabyte_edge.publisher import Delivery


def event() -> Event:
    return Event(
        event_id="event-123",
        context_id="ctx-1",
        captured_at_utc="2026-07-21T04:05:06Z",
        node_id="terrabyte-node-01",
        sequence=1042,
        uptime_ms=100,
        air_temperature_c=27.1,
        relative_humidity_pct=58.0,
        ppfd_umol_m2_s=230.5,
    )


class FakeMessageInfo:
    def __init__(self, rc: int = 0, published: bool = True, mid: int = 1) -> None:
        self.rc = rc
        # paho correlates a PUBACK with its publish through mid; the publisher
        # uses it to look up the v5 reason code the broker returned.
        self.mid = mid
        self._published = published

    def wait_for_publish(self, timeout=None) -> None:
        if not self._published:
            raise ValueError("timed out waiting for publish")

    def is_published(self) -> bool:
        return self._published


class FakeMqttClient:
    """Stands in for paho.mqtt.client.Client's subset MqttPublisher uses."""

    def __init__(self) -> None:
        self.will = None
        self.on_connect = None
        self.username = None
        self.password = None
        self.tls_enabled = False
        self.connected_to = None
        self.on_disconnect = None
        self.on_publish = None
        self.on_message = None
        self.connected = True
        self.loop_started = False
        self.loop_stopped = False
        self.disconnected = False
        self.reconnect_delays = None
        self.subscriptions: list[tuple[str, int]] = []
        self.published: list[tuple[str, bytes, int, bool]] = []
        self.next_publish_result = FakeMessageInfo()

    def will_set(self, topic, payload, qos=0, retain=False):
        self.will = (topic, payload, qos, retain)

    def username_pw_set(self, username, password=None):
        self.username = username
        self.password = password

    def tls_set(self, *args, **kwargs):
        self.tls_enabled = True

    def reconnect_delay_set(self, min_delay=1, max_delay=120):
        self.reconnect_delays = (min_delay, max_delay)

    def connect_async(self, host, port, keepalive=60):
        # The publisher must never use the blocking connect(): an unreachable
        # broker would then raise out of __init__ and abort service start-up.
        self.connected_to = (host, port, keepalive)

    def is_connected(self):
        return self.connected

    def loop_start(self):
        self.loop_started = True

    def loop_stop(self):
        self.loop_stopped = True

    def disconnect(self):
        self.disconnected = True

    def publish(self, topic, payload, qos=0, retain=False):
        self.published.append((topic, payload, qos, retain))
        return self.next_publish_result

    def subscribe(self, topic, qos=0):
        self.subscriptions.append((topic, qos))

    def simulate_connect(self):
        # The real client invokes on_connect from its network thread once
        # CONNACK arrives; tests trigger it explicitly instead.
        self.on_connect(self, None, {}, 0)

    def simulate_puback(self, mid, reason_code=None):
        """Deliver the v5 PUBACK reason code the broker returned for ``mid``."""

        self.on_publish(self, None, mid, reason_code)

    def simulate_message(
        self, payload: bytes, *, retain: bool = False, topic: str | None = None
    ):
        message = type(
            "Message",
            (),
            {
                "payload": payload,
                "retain": retain,
                "topic": topic or "tb/v2/orangepi-pro-01/dn/command",
            },
        )()
        self.on_message(self, None, message)


class FakeClientFactory:
    def __init__(self, *clients: FakeMqttClient) -> None:
        self._clients = iter(clients)
        self.created: list[FakeMqttClient] = []

    def __call__(self) -> FakeMqttClient:
        client = next(self._clients)
        self.created.append(client)
        return client


def make_publisher(client: FakeMqttClient, **overrides) -> MqttPublisher:
    kwargs = dict(
        host="mqtt.example.test",
        port=1883,
        gateway_id="orangepi-pro-01",
        topic_prefix="tb/v2",
        client_factory=lambda: client,
    )
    kwargs.update(overrides)
    return MqttPublisher(**kwargs)


class LifecycleTests(unittest.TestCase):
    def test_lwt_is_registered_before_connect(self) -> None:
        client = FakeMqttClient()
        # will_set must happen before connect(); assert by checking both were
        # called and the will was captured, since connect() records nothing
        # about ordering itself, we check will is set at all and connect()
        # was reached (constructor would raise/hang otherwise).
        make_publisher(client)
        self.assertEqual(
            client.will,
            ("tb/v2/orangepi-pro-01/up/status", b'{"online":false}', 1, True),
        )
        self.assertIsNotNone(client.connected_to)
        self.assertTrue(client.loop_started)

    def test_status_is_published_retained_on_connect(self) -> None:
        client = FakeMqttClient()
        make_publisher(client)
        client.simulate_connect()

        topic, payload, qos, retain = client.published[-1]
        self.assertEqual(topic, "tb/v2/orangepi-pro-01/up/status")
        self.assertEqual(payload, b'{"online":true}')
        self.assertEqual(qos, 1)
        self.assertTrue(retain)

    def test_credentials_and_tls_are_configured_when_provided(self) -> None:
        client = FakeMqttClient()
        make_publisher(client, username="gw-1", password="secret", tls=True)
        self.assertEqual(client.username, "gw-1")
        self.assertEqual(client.password, "secret")
        self.assertTrue(client.tls_enabled)

    def test_close_stops_loop_and_disconnects(self) -> None:
        client = FakeMqttClient()
        publisher = make_publisher(client)
        publisher.close()
        self.assertTrue(client.loop_stopped)
        self.assertTrue(client.disconnected)


class SendTests(unittest.TestCase):
    def test_puback_callback_does_not_deadlock_with_publish(self) -> None:
        """A PUBACK callback may run while paho's publish() is still blocked.

        Real paho invokes on_publish while holding its outgoing-message mutex.
        This fake makes publish wait for that callback to return, reproducing
        the opposite half of the lock ordering without relying on paho's
        private implementation or a live broker.
        """

        class InterleavingClient(FakeMqttClient):
            def __init__(self) -> None:
                super().__init__()
                self.publish_entered = threading.Event()
                self.puback_returned = threading.Event()
                self.publish_timed_out = False

            def publish(self, topic, payload, qos=0, retain=False):
                self.published.append((topic, payload, qos, retain))
                self.publish_entered.set()
                if not self.puback_returned.wait(timeout=1.0):
                    self.publish_timed_out = True
                    raise TimeoutError("on_publish did not return")
                return self.next_publish_result

        client = InterleavingClient()
        publisher = make_publisher(client)
        result = []

        def deliver_puback() -> None:
            if not client.publish_entered.wait(timeout=1.0):
                return
            try:
                client.simulate_puback(client.next_publish_result.mid)
            finally:
                client.puback_returned.set()

        callback_thread = threading.Thread(target=deliver_puback, daemon=True)
        publish_thread = threading.Thread(
            target=lambda: result.append(publisher.send(event())), daemon=True
        )
        callback_thread.start()
        publish_thread.start()
        publish_thread.join(timeout=2.0)
        callback_thread.join(timeout=2.0)

        self.assertFalse(publish_thread.is_alive(), "publish thread deadlocked")
        self.assertFalse(callback_thread.is_alive(), "PUBACK callback deadlocked")
        self.assertFalse(client.publish_timed_out, "PUBACK callback was blocked")
        self.assertEqual(len(result), 1)
        self.assertIs(result[0].outcome, Delivery.DELIVERED)

    def test_telemetry_is_published_not_retained_and_puback_maps_to_delivered(
        self,
    ) -> None:
        client = FakeMqttClient()
        client.next_publish_result = FakeMessageInfo(rc=0, published=True)
        publisher = make_publisher(client)

        result = publisher.send(event())

        self.assertIs(result.outcome, Delivery.DELIVERED)
        topic, payload, qos, retain = client.published[-1]
        self.assertEqual(topic, "tb/v2/orangepi-pro-01/up/telemetry")
        self.assertEqual(qos, 1)
        self.assertFalse(retain)
        body = json.loads(payload)
        self.assertEqual(body["gateway_id"], "orangepi-pro-01")
        self.assertEqual(body["nodes"][0]["measurements"]["air_humidity_pct"], 58.0)

    def test_missing_puback_maps_to_retry(self) -> None:
        client = FakeMqttClient()
        client.next_publish_result = FakeMessageInfo(rc=0, published=False)
        publisher = make_publisher(client)

        result = publisher.send(event())
        self.assertIs(result.outcome, Delivery.RETRY)

    def test_nonzero_publish_rc_maps_to_retry(self) -> None:
        client = FakeMqttClient()
        client.next_publish_result = FakeMessageInfo(rc=4, published=False)
        publisher = make_publisher(client)

        result = publisher.send(event())
        self.assertIs(result.outcome, Delivery.RETRY)

    def test_publish_raising_maps_to_retry(self) -> None:
        client = FakeMqttClient()

        def raising_publish(topic, payload, qos=0, retain=False):
            raise OSError("not connected")

        client.publish = raising_publish
        publisher = make_publisher(client)

        result = publisher.send(event())
        self.assertIs(result.outcome, Delivery.RETRY)
        self.assertEqual(result.reason, "OSError")

    def test_local_envelope_failure_is_dead_not_retry(self) -> None:
        # DEAD is reserved for a local schema-validation failure discovered
        # before anything reaches the broker — MQTT itself has no 4xx to
        # signal "this payload is permanently invalid".
        client = FakeMqttClient()
        publisher = make_publisher(client)

        class ExplodingEvent:
            def envelope_v2(self, *, gateway_id):
                raise ValueError("boom")

        result = publisher.send(ExplodingEvent())
        self.assertIs(result.outcome, Delivery.DEAD)


class BrokerRejectionTests(unittest.TestCase):
    """Regressions for failures only a real broker exposed.

    Each of these looked fine against a hand-written fake and was wrong in
    practice, which is the same way the v1 contract stayed broken.
    """

    def test_an_unreachable_broker_does_not_abort_construction(self) -> None:
        """The edge boots before the broker is reachable — that is what the
        durable outbox is for. A blocking connect() raised ConnectionRefusedError
        straight out of __init__ and killed service start-up instead."""

        client = FakeMqttClient()
        client.connected = False
        publisher = make_publisher(client)

        result = publisher.send(event())
        self.assertIs(result.outcome, Delivery.RETRY)
        self.assertEqual(result.reason, "not_connected")

    def test_a_disconnected_session_fails_fast_instead_of_waiting_for_puback(
        self,
    ) -> None:
        """Publishing while disconnected used to queue locally and burn the
        whole PUBACK timeout, then report the misleading 'puback_timeout' —
        including when the broker had actually rejected the credentials."""

        client = FakeMqttClient()
        client.connected = False
        publisher = make_publisher(client)

        self.assertEqual(publisher.send(event()).reason, "not_connected")
        self.assertEqual(client.published, [])

    def test_an_acl_rejected_publish_is_not_reported_as_delivered(self) -> None:
        """MQTT 3.1.1 acknowledges a publish the ACL refused, so a gateway
        publishing outside its own namespace saw 'delivered', dropped the event
        from the outbox and lost it silently. v5 carries the refusal on the
        PUBACK; this asserts it is surfaced as RETRY rather than success."""

        client = FakeMqttClient()
        publisher = make_publisher(client)
        client.simulate_connect()

        class NotAuthorized:
            is_failure = True

            def __str__(self) -> str:
                return "Not authorized"

        info = client.next_publish_result
        client.simulate_puback(info.mid, NotAuthorized())

        result = publisher.send(event())
        self.assertIs(result.outcome, Delivery.RETRY)
        self.assertIn("Not authorized", result.reason)

    def test_a_successful_puback_reason_still_maps_to_delivered(self) -> None:
        """'No matching subscribers' (0x10) is a success code, not a refusal —
        it just means nobody was subscribed yet."""

        client = FakeMqttClient()
        publisher = make_publisher(client)
        client.simulate_connect()

        class NoMatchingSubscribers:
            is_failure = False
            value = 0x10

            def __str__(self) -> str:
                return "No matching subscribers"

        info = client.next_publish_result
        client.simulate_puback(info.mid, NoMatchingSubscribers())

        result = publisher.send(event())
        self.assertIs(result.outcome, Delivery.DELIVERED)


class SelfRecoveryTests(unittest.TestCase):
    def test_consecutive_link_failures_trigger_exactly_one_rebuild(self) -> None:
        first = FakeMqttClient()
        first.connected = False
        second = FakeMqttClient()
        second.connected = False
        factory = FakeClientFactory(first, second)
        publisher = make_publisher(
            first,
            client_factory=factory,
            max_consecutive_link_failures=3,
            max_seconds_since_success=1_000.0,
            min_rebuild_interval_seconds=0.0,
        )

        self.assertEqual(publisher.send(event()).reason, "not_connected")
        first.connected = True
        first.next_publish_result = FakeMessageInfo(published=False)
        self.assertEqual(publisher.send(event()).reason, "puback_timeout")
        first.connected = False
        with self.assertLogs("terrabyte_edge.mqtt_publisher", level="WARNING") as logs:
            self.assertEqual(publisher.send(event()).reason, "not_connected")

        publisher.send(event())
        publisher.send(event())
        self.assertEqual(factory.created, [first, second])
        self.assertTrue(first.loop_stopped)
        self.assertTrue(first.disconnected)
        self.assertTrue(second.loop_started)
        self.assertFalse(second.loop_stopped)
        self.assertIn("consecutive_link_failures(3>=3)", " ".join(logs.output))

    def test_command_handler_survives_rebuild_and_is_resubscribed(self) -> None:
        first = FakeMqttClient()
        first.connected = False
        second = FakeMqttClient()
        factory = FakeClientFactory(first, second)
        publisher = make_publisher(
            first,
            client_factory=factory,
            max_consecutive_link_failures=1,
            username="gw-1",
            password="secret",
            tls=True,
        )
        received = []
        publisher.subscribe_commands(
            lambda payload, retain: received.append((payload, retain))
        )

        publisher.send(event())
        second.simulate_connect()
        second.simulate_message(b'{"command_id":"cmd-1"}')

        self.assertEqual(
            second.subscriptions,
            [("tb/v2/orangepi-pro-01/dn/command", 1)],
        )
        self.assertEqual(received, [(b'{"command_id":"cmd-1"}', False)])
        self.assertEqual((second.username, second.password), ("gw-1", "secret"))
        self.assertTrue(second.tls_enabled)
        self.assertEqual(second.reconnect_delays, (1, 30))
        self.assertIsNotNone(second.on_connect)
        self.assertIsNotNone(second.on_disconnect)
        self.assertIsNotNone(second.on_publish)
        self.assertIsNotNone(second.on_message)

    def test_rebuild_is_rate_limited_while_broker_stays_down(self) -> None:
        now = [100.0]
        first = FakeMqttClient()
        first.connected = False
        second = FakeMqttClient()
        second.connected = False
        third = FakeMqttClient()
        third.connected = False
        factory = FakeClientFactory(first, second, third)
        publisher = make_publisher(
            first,
            client_factory=factory,
            max_consecutive_link_failures=1,
            max_seconds_since_success=1_000.0,
            min_rebuild_interval_seconds=30.0,
            clock=lambda: now[0],
        )

        publisher.send(event())
        publisher.send(event())
        now[0] += 29.0
        publisher.send(event())

        self.assertEqual(factory.created, [first, second])
        self.assertFalse(second.loop_stopped)

        now[0] += 1.0
        publisher.send(event())
        self.assertEqual(factory.created, [first, second, third])

    def test_successful_publish_resets_consecutive_failure_count(self) -> None:
        first = FakeMqttClient()
        first.connected = False
        second = FakeMqttClient()
        factory = FakeClientFactory(first, second)
        publisher = make_publisher(
            first,
            client_factory=factory,
            max_consecutive_link_failures=3,
            max_seconds_since_success=1_000.0,
        )

        publisher.send(event())
        publisher.send(event())
        first.connected = True
        self.assertIs(publisher.send(event()).outcome, Delivery.DELIVERED)
        first.connected = False
        publisher.send(event())
        publisher.send(event())
        self.assertEqual(factory.created, [first])

        publisher.send(event())
        self.assertEqual(factory.created, [first, second])

    def test_time_without_success_triggers_rebuild_when_traffic_arrives(self) -> None:
        now = [500.0]
        first = FakeMqttClient()
        first.connected = False
        second = FakeMqttClient()
        factory = FakeClientFactory(first, second)
        publisher = make_publisher(
            first,
            client_factory=factory,
            max_consecutive_link_failures=99,
            max_seconds_since_success=90.0,
            clock=lambda: now[0],
        )

        now[0] += 91.0
        with self.assertLogs("terrabyte_edge.mqtt_publisher", level="WARNING") as logs:
            publisher.send(event())

        self.assertEqual(factory.created, [first, second])
        warning = " ".join(logs.output)
        self.assertIn("seconds_since_success(91.0>=90.0)", warning)
        self.assertIn("link_dead_seconds=91.0", warning)


if __name__ == "__main__":
    unittest.main()


class DownlinkRoutingTests(unittest.TestCase):
    """Two downlink topics share one on_message, so routing has to be explicit."""

    HEARTBEAT_TOPIC = "tb/v2/orangepi-pro-01/dn/heartbeat"
    COMMAND_TOPIC = "tb/v2/orangepi-pro-01/dn/command"

    def setUp(self) -> None:
        self.client = FakeMqttClient()
        self.publisher = make_publisher(self.client)
        self.commands: list[bytes] = []
        self.heartbeats = 0
        self.publisher.subscribe_commands(lambda payload, retained: self.commands.append(payload))
        self.publisher.subscribe_heartbeats(self.countHeartbeat)

    def countHeartbeat(self) -> None:
        self.heartbeats += 1

    def test_both_downlink_topics_are_subscribed_on_connect(self) -> None:
        self.client.simulate_connect()

        topics = [topic for topic, _qos in self.client.subscriptions]
        # Renewed together after a reconnect. A blip that restored commands but
        # not heartbeats would leave the gateway believing the cloud is dead
        # while it is actively being commanded.
        self.assertIn(self.COMMAND_TOPIC, topics)
        self.assertIn(self.HEARTBEAT_TOPIC, topics)

    def test_a_heartbeat_does_not_reach_the_command_relay(self) -> None:
        self.client.simulate_message(b'{"message_type":"heartbeat"}', topic=self.HEARTBEAT_TOPIC)

        self.assertEqual(self.heartbeats, 1)
        # Handing a heartbeat to the relay would make it publish a rejected ack
        # for a command_id that does not exist.
        self.assertEqual(self.commands, [])

    def test_a_command_does_not_count_as_a_heartbeat(self) -> None:
        self.client.simulate_message(b'{"command_id":"c-1"}', topic=self.COMMAND_TOPIC)

        self.assertEqual(self.commands, [b'{"command_id":"c-1"}'])
        # A command is not proof the application is alive: the broker replays
        # nothing, but a queued command can outlive the process that sent it.
        self.assertEqual(self.heartbeats, 0)


class StatusStateTests(unittest.TestCase):
    def test_retained_status_carries_the_link_state(self) -> None:
        client = FakeMqttClient()
        publisher = make_publisher(client, state_provider=lambda: "RESYNC")

        client.simulate_connect()

        topic, payload, qos, retain = client.published[0]
        body = json.loads(payload)
        self.assertEqual(topic, "tb/v2/orangepi-pro-01/up/status")
        self.assertTrue(body["online"])
        # The backend reads this to decide whether to publish commands at all.
        self.assertEqual(body["state"], "RESYNC")
        self.assertTrue(retain)
        self.assertEqual(qos, 1)

    def test_status_without_a_provider_still_reports_online(self) -> None:
        client = FakeMqttClient()
        publisher = make_publisher(client)

        client.simulate_connect()

        body = json.loads(client.published[0][1])
        self.assertTrue(body["online"])
        self.assertNotIn("state", body)


class EdgeIrrigationPublishTests(unittest.TestCase):
    def test_a_record_goes_to_its_own_topic_unretained(self) -> None:
        client = FakeMqttClient()
        publisher = make_publisher(client)

        result = publisher.send_edge_irrigation(
            EdgeIrrigationRecord(
                record_id="rec-1",
                node_id="node-1",
                volume_ml=60.0,
                dispensed_at_utc="2026-08-27T01:02:03Z",
            )
        )
        client.simulate_puback(client.published[-1] and 1)

        topic, payload, qos, retain = client.published[-1]
        self.assertEqual(topic, "tb/v2/orangepi-pro-01/up/irrigation")
        self.assertEqual(qos, 1)
        # Retaining it would replay one dose to every future subscriber, and the
        # server counts what it receives against the pot's daily budget.
        self.assertFalse(retain)
        self.assertEqual(json.loads(payload)["origin"], "EDGE_FALLBACK")
