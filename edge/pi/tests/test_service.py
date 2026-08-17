from types import SimpleNamespace
import json
from datetime import datetime, timezone
from pathlib import Path
import unittest

from terrabyte_edge.outbox import KIND_TELEMETRY, OutboxItem
from terrabyte_edge.protocol import Event
from terrabyte_edge.publisher import Delivery, DeliveryResult
from terrabyte_edge.service import BridgeService


# ``_ingest_line`` takes the port the line arrived on so a fault can be shown
# against the right cable. Which port is irrelevant to these tests, but it has
# to be a real string: GatewayState keys its per-port records by it.
PORT = "/dev/serial/by-id/usb-test"


def event(event_id: str) -> Event:
    return Event(
        event_id=event_id,
        context_id="ctx-1",
        captured_at_utc="2026-07-21T04:05:06Z",
        node_id="node-1",
        sequence=1,
        uptime_ms=100,
        air_temperature_c=20.0,
        relative_humidity_pct=50.0,
        ppfd_umol_m2_s=300.0,
    )


class FakeOutbox:
    def __init__(self) -> None:
        self.items = [
            OutboxItem(event("oldest"), 0),
            OutboxItem(event("newer"), 0),
        ]
        self.retried: list[str] = []
        self.delivered: list[str] = []
        self.due_kinds: list[str] = []

    def due(self, _limit: int, *, kind: str = KIND_TELEMETRY) -> list[OutboxItem]:
        self.due_kinds.append(kind)
        return self.items

    def mark_retry(
        self,
        event_id: str,
        _attempts: int,
        _error: str,
        _retry_after: float | None,
    ) -> float:
        self.retried.append(event_id)
        return 2.0

    def mark_delivered(self, event_id: str) -> None:
        self.delivered.append(event_id)

    def mark_dead(self, _event_id: str, _error: str) -> None:
        raise AssertionError("unexpected dead-letter")


class FakePublisher:
    def __init__(self) -> None:
        self.sent: list[str] = []
        self.closed = False

    def send(self, item: Event) -> DeliveryResult:
        self.sent.append(item.event_id)
        return DeliveryResult(Delivery.RETRY, "offline")

    def close(self) -> None:
        self.closed = True


class ServiceTests(unittest.TestCase):
    def test_retry_stops_newer_delivery_to_preserve_order(self) -> None:
        outbox = FakeOutbox()
        publisher = FakePublisher()
        settings = SimpleNamespace(
            upload_batch_size=20,
            http_timeout_seconds=1.0,
            device_id="orangepi-test",
            claim_code="483920",
            transport="mqtt",
        )
        service = BridgeService(
            settings,
            outbox=outbox,
            publisher=publisher,
            serial_readers=[],
        )

        self.assertEqual(service._upload_once(), 1)
        self.assertEqual(publisher.sent, ["oldest"])
        self.assertEqual(outbox.retried, ["oldest"])
        self.assertEqual(outbox.delivered, [])
        # The block above is scoped to telemetry. An ack must not be stuck
        # behind a backed-off observation, so this worker never asks for a
        # mixed batch.
        self.assertEqual(outbox.due_kinds, [KIND_TELEMETRY])

    def test_join_closes_the_publisher(self) -> None:
        outbox = FakeOutbox()
        publisher = FakePublisher()
        settings = SimpleNamespace(
            upload_batch_size=20,
            http_timeout_seconds=1.0,
            device_id="orangepi-test",
            claim_code="483920",
            transport="mqtt",
        )
        service = BridgeService(
            settings,
            outbox=outbox,
            publisher=publisher,
            serial_readers=[],
        )

        service.join()
        self.assertTrue(publisher.closed)


class IrrigationSuggestionWiringTests(unittest.TestCase):
    """The suggestion has to be attached before the event is queued.

    If it were computed at upload time instead, a reading that sat in the outbox
    through a network outage would be sized against configuration that may have
    changed since — the dose would no longer match the reading that justified it.
    """

    def build(self, *, volumes=None, crops=None):
        from terrabyte_edge.service import BridgeService

        settings = SimpleNamespace(
            upload_batch_size=20,
            http_timeout_seconds=1.0,
            crop_context_id="ctx-1",
            expected_node_ids=frozenset({"node-1"}),
            clock_minimum_utc=datetime(2025, 1, 1, tzinfo=timezone.utc),
            device_id="orangepi-test",
            claim_code="483920",
            transport="mqtt",
            pot_substrate_ml=volumes or {},
            pot_crop_codes=crops or {},
            substrate_volume_ml_for=lambda node: (volumes or {}).get(node),
            crop_code_for=lambda node: (crops or {}).get(node),
        )
        outbox = RecordingOutbox()
        service = BridgeService(
            settings,
            outbox=outbox,
            publisher=SimpleNamespace(close=lambda: None),
            serial_readers=[],
        )
        return service, outbox

    def line(self, moisture=20.0):
        return (
            json.dumps(
                {
                    "message_type": "telemetry",
                    "protocol_version": 1,
                    "node_id": "node-1",
                    "sequence": 1,
                    "uptime_ms": 1000,
                    "air_temperature_c": 24.0,
                    "relative_humidity_pct": 55.0,
                    "ppfd_umol_m2_s": 300.0,
                    "soil_moisture_pct": moisture,
                }
            )
            + "\n"
        ).encode()

    def test_configured_pot_gets_a_suggestion(self):
        service, outbox = self.build(
            volumes={"node-1": 3000}, crops={"node-1": "lettuce"}
        )
        service._ingest_line(PORT, self.line())

        suggestion = outbox.events[0].irrigation_suggestion
        self.assertIsNotNone(suggestion)
        self.assertGreater(suggestion.volume_ml, 0)
        self.assertEqual(suggestion.assumed_crop_code, "lettuce")
        self.assertEqual(suggestion.assumed_substrate_volume_ml, 3000)

    def test_no_pot_volume_means_no_suggestion(self):
        """Better an absent block than a number sized for a pot we guessed at —
        the backend's fallback table exists for exactly this case."""

        service, outbox = self.build(crops={"node-1": "lettuce"})
        service._ingest_line(PORT, self.line())

        self.assertIsNone(outbox.events[0].irrigation_suggestion)

    def test_the_suggestion_travels_in_the_envelope(self):
        service, outbox = self.build(
            volumes={"node-1": 3000}, crops={"node-1": "lettuce"}
        )
        service._ingest_line(PORT, self.line())

        node = outbox.events[0].envelope_v2(gateway_id="gw-1")["nodes"][0]
        self.assertIn("irrigation_suggestion", node)
        self.assertEqual(node["irrigation_suggestion"]["model_version"], "water-balance-v1")


class RecordingOutbox:
    def __init__(self):
        self.events = []

    def enqueue(self, event):
        self.events.append(event)
        return True


class CommandRelayWiringTests(unittest.TestCase):
    """Whether the relay exists at all, and what the short-key route does with it."""

    def build(self, *, publisher, enabled=True, transport="mqtt", **extra):
        settings = SimpleNamespace(
            upload_batch_size=20,
            upload_interval_seconds=1.0,
            http_timeout_seconds=1.0,
            device_id="orangepi-test",
            claim_code="483920",
            transport=transport,
            database_path=Path("/nonexistent/outbox.sqlite3"),
            command_relay_enabled=enabled,
            command_queue_max=8,
            command_deadman_interval_seconds=1.0,
            command_deadman_grace_seconds=5.0,
            command_max_serial_bytes=120,
            command_journal_retention_seconds=86_400.0,
            **extra,
        )
        return BridgeService(
            settings,
            outbox=FakeOutbox(),
            publisher=publisher,
            serial_readers=[],
        )

    def test_http_transport_gets_no_relay(self) -> None:
        """HTTP has no command downlink in the contract — no topic, no ack path.

        A relay that could never receive anything would suggest the gateway was
        waiting for commands when nothing could ever arrive.
        """

        service = self.build(publisher=FakePublisher(), transport="http")
        self.assertIsNone(service.command_relay)
        self.assertNotIn(
            "command-relay", [name for name, _ in service._critical_workers()]
        )

    def test_the_kill_switch_removes_the_relay_and_its_workers(self) -> None:
        service = self.build(publisher=CommandCapablePublisher(), enabled=False)
        self.assertIsNone(service.command_relay)

    def test_a_command_capable_transport_gets_the_relay_and_four_workers(self) -> None:
        service = self.build(publisher=CommandCapablePublisher())
        self.assertIsNotNone(service.command_relay)
        names = [name for name, _ in service._critical_workers()]
        self.assertEqual(
            names,
            [
                "backend-upload",
                "status-snapshot",
                "command-relay",
                "command-deadman",
                "ack-upload",
            ],
        )

    def test_the_short_key_route_reaches_the_relay(self) -> None:
        service = self.build(publisher=CommandCapablePublisher())
        seen = []
        service.command_relay.handle_serial_ack = lambda port, message: seen.append(
            (port, message)
        )

        service._ingest_line(PORT, b'{"t":"ack","id":"c1","ph":"accepted"}\n')
        self.assertEqual(seen, [(PORT, {"t": "ack", "id": "c1", "ph": "accepted"})])

    def test_an_unknown_short_key_frame_is_counted_against_the_port(self) -> None:
        service = self.build(publisher=CommandCapablePublisher())
        service._ingest_line(PORT, b'{"t":"surprise"}\n')
        self.assertEqual(service.state.snapshot().ports[0].errors, 1)


class CommandCapablePublisher(FakePublisher):
    def send_ack(self, ack):
        return DeliveryResult(Delivery.DELIVERED, "puback")

    def subscribe_commands(self, handler):
        self.handler = handler


if __name__ == "__main__":
    unittest.main()
