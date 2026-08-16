from types import SimpleNamespace
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


if __name__ == "__main__":
    unittest.main()
