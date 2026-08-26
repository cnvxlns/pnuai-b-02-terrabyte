from pathlib import Path
from types import SimpleNamespace
import json
import tempfile
from datetime import datetime, timezone
import unittest

from terrabyte_edge.outbox import KIND_CONTROL, OutboxItem
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


class TemporaryDatabase:
    """A real path for the durable stores a BridgeService always owns.

    ``Settings.database_path`` has no default: a gateway with nowhere to persist
    its outbox and irrigation history is not a configuration that exists. These
    fixtures predate the irrigation history and simply had not needed it yet.
    """

    def databasePath(self) -> Path:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        return Path(directory.name) / "edge.sqlite3"


class ServiceTests(TemporaryDatabase, unittest.TestCase):
    def test_retry_stops_newer_delivery_to_preserve_order(self) -> None:
        outbox = FakeOutbox()
        publisher = FakePublisher()
        settings = SimpleNamespace(
            database_path=self.databasePath(),
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
            database_path=self.databasePath(),
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


class IrrigationSuggestionWiringTests(TemporaryDatabase, unittest.TestCase):
    """The suggestion has to be attached before the event is queued.

    If it were computed at upload time instead, a reading that sat in the outbox
    through a network outage would be sized against configuration that may have
    changed since — the dose would no longer match the reading that justified it.
    """

    def build(self, *, volumes=None, crops=None):
        from terrabyte_edge.service import BridgeService

        settings = SimpleNamespace(
            database_path=self.databasePath(),
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
        self.pending_control = 0

    def enqueue(self, event):
        self.events.append(event)
        return True

    def counts(self, *, kind=None):
        return (self.pending_control if kind == KIND_CONTROL else 0, 0)


class AutonomyWiringTests(TemporaryDatabase, unittest.TestCase):
    """The service is what turns three tested modules into one behaviour."""

    def build(self, **overrides):
        settings = SimpleNamespace(
            database_path=self.databasePath(),
            upload_batch_size=20,
            upload_interval_seconds=0.01,
            http_timeout_seconds=1.0,
            crop_context_id="ctx-1",
            expected_node_id="node-1",
            device_id="orangepi-pro-01",
            clock_minimum_utc=overrides.get(
                "clock_minimum_utc", datetime(2025, 1, 1, tzinfo=timezone.utc)
            ),
            pot_substrate_ml={},
            pot_crop_codes={},
            substrate_volume_ml_for=lambda node: None,
            crop_code_for=lambda node: None,
            command_relay_enabled=False,
        )
        service = BridgeService(
            settings,
            outbox=RecordingOutbox(),
            publisher=SimpleNamespace(close=lambda: None),
            serial_reader=object(),
        )
        service.irrigation_history.initialize()
        return service

    def line(self, **overrides):
        body = {
            "message_type": "telemetry",
            "protocol_version": 1,
            "node_id": "node-1",
            "sequence": 1,
            "uptime_ms": 1000,
            "air_temperature_c": 24.0,
            "relative_humidity_pct": 45.0,
            "ppfd_umol_m2_s": 300.0,
            "soil_temperature_c": 21.0,
            "soil_moisture_pct": 12.0,
        }
        body.update(overrides)
        return json.dumps(body).encode()

    def test_a_soil_reading_reaches_autonomy(self) -> None:
        service = self.build()

        service._ingest_line(self.line())

        # Autonomy decides on the newest sample; if nothing feeds it, the
        # emergency rule is a module nobody ever calls.
        self.assertIn("node-1", service.autonomy._readings)

    def test_a_reading_with_no_soil_probe_is_not_offered_to_autonomy(self) -> None:
        service = self.build()

        service._ingest_line(self.line(soil_moisture_pct=None, soil_temperature_c=None))

        # The whole envelope turns on soil moisture. A node with no probe has
        # nothing autonomy can be right about.
        self.assertEqual(service.autonomy._readings, {})

    def test_autonomy_does_nothing_while_the_cloud_is_alive(self) -> None:
        service = self.build()
        service._ingest_line(self.line())
        service.cloud_link.record_heartbeat()

        self.assertIsNone(service._autonomy_tick())

    def test_autonomy_runs_once_the_cloud_has_been_quiet(self) -> None:
        service = self.build()
        service._ingest_line(self.line())
        service.cloud_link.record_heartbeat()
        service.cloud_link._started_at -= 3_600.0
        service.cloud_link._last_heartbeat_at -= 3_600.0

        outcome = service._autonomy_tick()

        self.assertIsNotNone(outcome)
        self.assertEqual(outcome.node_id, "node-1")

    def test_the_control_backlog_holds_the_link_out_of_cloud_online(self) -> None:
        from terrabyte_edge.cloud_link import CloudLinkState

        service = self.build()
        service.outbox.pending_control = 1
        for _ in range(3):
            service.cloud_link.record_heartbeat()
            service.cloud_link._streak_started_at -= 30.0

        service._refresh_link()

        self.assertEqual(service.cloud_link.state, CloudLinkState.RESYNC)

    def test_a_clock_behind_the_configured_minimum_holds_everything(self) -> None:
        from terrabyte_edge.cloud_link import CloudLinkState

        service = self.build(
            clock_minimum_utc=datetime(2099, 1, 1, tzinfo=timezone.utc)
        )

        service._refresh_link()

        # Every gate in the safety envelope is a comparison of timestamps, so a
        # gateway that booted before NTP settled cannot evaluate any of them.
        self.assertEqual(service.cloud_link.state, CloudLinkState.SAFE_HOLD)
        self.assertIsNone(service._autonomy_tick())
