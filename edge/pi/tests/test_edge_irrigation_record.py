"""The control queue: telling the server about water it did not authorise."""

from pathlib import Path
import json
import sqlite3
import tempfile
import unittest

from terrabyte_edge.outbox import KIND_CONTROL, KIND_TELEMETRY, Outbox
from terrabyte_edge.protocol import EdgeIrrigationRecord, Event


NOW = 1_800_000_000.0


def record(record_id: str = "rec-1", volume_ml: float = 60.0) -> EdgeIrrigationRecord:
    return EdgeIrrigationRecord(
        record_id=record_id,
        node_id="node-1",
        volume_ml=volume_ml,
        dispensed_at_utc="2026-08-27T01:02:03Z",
    )


def event(event_id: str) -> Event:
    return Event(
        event_id=event_id,
        context_id="ctx-1",
        captured_at_utc="2026-08-27T01:02:03Z",
        node_id="node-1",
        sequence=1,
        uptime_ms=100,
        air_temperature_c=20.0,
        relative_humidity_pct=50.0,
        ppfd_umol_m2_s=300.0,
    )


class EdgeIrrigationRecordTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = NOW

    def outbox(self, path: Path) -> Outbox:
        return Outbox(
            path,
            retry_base_seconds=2.0,
            retry_max_seconds=10.0,
            clock=lambda: self.now,
        )

    def test_event_id_is_derived_from_the_record_id(self) -> None:
        # The pi-side half of the duplicate defence, exactly as CommandAck does
        # it: a re-enqueue of the same delivery collapses onto this key.
        self.assertEqual(record("rec-9").event_id, "edge_irrigation:rec-9")

    def test_payload_names_the_gateway_and_the_origin(self) -> None:
        payload = record().payload(gateway_id="gw-1")

        self.assertEqual(payload["message_type"], "edge_irrigation")
        self.assertEqual(payload["gateway_id"], "gw-1")
        self.assertEqual(payload["node_id"], "node-1")
        self.assertEqual(payload["volume_ml"], 60.0)
        # The server writes this as origin=EDGE_FALLBACK, state=COMPLETED, and
        # its budget query then counts it with no extra code.
        self.assertEqual(payload["origin"], "EDGE_FALLBACK")

    def test_a_record_round_trips_through_the_outbox(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            outbox = self.outbox(Path(directory) / "edge.sqlite3")
            outbox.initialize()

            self.assertTrue(outbox.enqueue(record(), kind=KIND_CONTROL))
            due = outbox.due(10, kind=KIND_CONTROL)

        self.assertEqual(len(due), 1)
        self.assertEqual(due[0].event, record())

    def test_a_blocked_telemetry_row_does_not_hold_the_control_queue(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            outbox = self.outbox(Path(directory) / "edge.sqlite3")
            outbox.initialize()
            outbox.enqueue(event("evt-1"), kind=KIND_TELEMETRY)
            outbox.mark_retry("evt-1", 1, "broker refused", 3600.0)
            outbox.enqueue(record(), kind=KIND_CONTROL)

            due = outbox.due(10, kind=KIND_CONTROL)

        # The whole point of a separate kind: the server learning what water
        # already went in must not queue behind a poisoned telemetry sample.
        self.assertEqual(len(due), 1)

    def test_pending_control_rows_are_countable_for_the_resync_gate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            outbox = self.outbox(Path(directory) / "edge.sqlite3")
            outbox.initialize()
            outbox.enqueue(record("rec-1"), kind=KIND_CONTROL)
            outbox.enqueue(record("rec-2"), kind=KIND_CONTROL)
            outbox.enqueue(event("evt-1"), kind=KIND_TELEMETRY)

            pending, _dead = outbox.counts(kind=KIND_CONTROL)

        # CloudLink refuses CLOUD_ONLINE while this is above zero, so it has to
        # count control rows only — telemetry backlog is not a resync debt.
        self.assertEqual(pending, 2)


class StaleSchemaMigrationTests(unittest.TestCase):
    """A gateway in the field already has a telemetry_outbox with two kinds."""

    OLD_SCHEMA = """
        CREATE TABLE telemetry_outbox (
            event_id TEXT PRIMARY KEY,
            payload_json TEXT NOT NULL,
            created_at_epoch REAL NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
            next_attempt_epoch REAL NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending'
                CHECK (status IN ('pending', 'dead')),
            last_error TEXT,
            kind TEXT NOT NULL DEFAULT 'telemetry'
                CHECK (kind IN ('telemetry', 'ack'))
        )
    """

    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.path = Path(self._directory.name) / "edge.sqlite3"
        connection = sqlite3.connect(self.path)
        with connection:
            connection.execute(self.OLD_SCHEMA)
            connection.execute(
                "INSERT INTO telemetry_outbox(event_id, payload_json,"
                " created_at_epoch, next_attempt_epoch, kind)"
                " VALUES ('evt-old', ?, ?, ?, 'telemetry')",
                (json.dumps(event("evt-old").to_record()), NOW, NOW),
            )
        connection.close()

    def outbox(self) -> Outbox:
        return Outbox(
            self.path, retry_base_seconds=2.0, retry_max_seconds=10.0,
            clock=lambda: NOW,
        )

    def test_a_control_row_survives_the_old_check_constraint(self) -> None:
        outbox = self.outbox()
        outbox.initialize()

        # enqueue uses INSERT OR IGNORE, which swallows a CHECK violation as if
        # it were a duplicate. Without a migration this returns False forever
        # and the server is never told about autonomous irrigation — silently,
        # which is the worst way for this particular fact to go missing.
        self.assertTrue(outbox.enqueue(record(), kind=KIND_CONTROL))
        self.assertEqual(len(outbox.due(10, kind=KIND_CONTROL)), 1)

    def test_the_migration_keeps_rows_that_were_already_queued(self) -> None:
        outbox = self.outbox()
        outbox.initialize()

        self.assertEqual(len(outbox.due(10, kind=KIND_TELEMETRY)), 1)
