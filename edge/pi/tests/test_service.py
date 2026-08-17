from types import SimpleNamespace
import json
from datetime import datetime, timezone
import unittest

from terrabyte_edge.outbox import OutboxItem
from terrabyte_edge.protocol import Event
from terrabyte_edge.publisher import Delivery, DeliveryResult
from terrabyte_edge.service import BridgeService


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

    def due(self, _limit: int) -> list[OutboxItem]:
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
        )
        service = BridgeService(
            settings,
            outbox=outbox,
            publisher=publisher,
            serial_reader=object(),
        )

        self.assertEqual(service._upload_once(), 1)
        self.assertEqual(publisher.sent, ["oldest"])
        self.assertEqual(outbox.retried, ["oldest"])
        self.assertEqual(outbox.delivered, [])

    def test_join_closes_the_publisher(self) -> None:
        outbox = FakeOutbox()
        publisher = FakePublisher()
        settings = SimpleNamespace(
            upload_batch_size=20,
            http_timeout_seconds=1.0,
        )
        service = BridgeService(
            settings,
            outbox=outbox,
            publisher=publisher,
            serial_reader=object(),
        )

        service.join()
        self.assertTrue(publisher.closed)


if __name__ == "__main__":
    unittest.main()


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
            expected_node_id="node-1",
            clock_minimum_utc=datetime(2025, 1, 1, tzinfo=timezone.utc),
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
            serial_reader=object(),
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
        service._ingest_line(self.line())

        suggestion = outbox.events[0].irrigation_suggestion
        self.assertIsNotNone(suggestion)
        self.assertGreater(suggestion.volume_ml, 0)
        self.assertEqual(suggestion.assumed_crop_code, "lettuce")
        self.assertEqual(suggestion.assumed_substrate_volume_ml, 3000)

    def test_no_pot_volume_means_no_suggestion(self):
        """Better an absent block than a number sized for a pot we guessed at —
        the backend's fallback table exists for exactly this case."""

        service, outbox = self.build(crops={"node-1": "lettuce"})
        service._ingest_line(self.line())

        self.assertIsNone(outbox.events[0].irrigation_suggestion)

    def test_the_suggestion_travels_in_the_envelope(self):
        service, outbox = self.build(
            volumes={"node-1": 3000}, crops={"node-1": "lettuce"}
        )
        service._ingest_line(self.line())

        node = outbox.events[0].envelope_v2(gateway_id="gw-1")["nodes"][0]
        self.assertIn("irrigation_suggestion", node)
        self.assertEqual(node["irrigation_suggestion"]["model_version"], "water-balance-v1")


class RecordingOutbox:
    def __init__(self):
        self.events = []

    def enqueue(self, event):
        self.events.append(event)
        return True
