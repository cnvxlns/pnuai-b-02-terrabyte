import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from terrabyte_edge.outbox import KIND_ACK, KIND_TELEMETRY, Outbox, OutboxFullError
from terrabyte_edge.protocol import Event


def event(event_id: str = "event-1") -> Event:
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


class OutboxTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.now = [1000.0]
        self.path = Path(self.tempdir.name) / "state" / "outbox.sqlite3"
        self.outbox = Outbox(
            self.path,
            retry_base_seconds=2.0,
            retry_max_seconds=10.0,
            clock=lambda: self.now[0],
        )
        self.outbox.initialize()

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_event_survives_reopening_and_duplicate_id_is_ignored(self) -> None:
        self.assertTrue(self.outbox.enqueue(event()))
        self.assertFalse(self.outbox.enqueue(event()))

        reopened = Outbox(
            self.path,
            retry_base_seconds=2.0,
            retry_max_seconds=10.0,
            clock=lambda: self.now[0],
        )
        item = reopened.due(10)[0]
        self.assertEqual(item.event, event())
        self.assertEqual(reopened.counts(), (1, 0))

    def test_retry_is_delayed_with_capped_exponential_backoff(self) -> None:
        self.outbox.enqueue(event())
        delay = self.outbox.mark_retry("event-1", 0, "network", None)
        self.assertEqual(delay, 2.0)
        self.assertEqual(self.outbox.due(10), [])

        self.now[0] += 2.0
        item = self.outbox.due(10)[0]
        self.assertEqual(item.attempts, 1)
        self.assertEqual(item.event.event_id, "event-1")
        self.assertEqual(item.event.captured_at_utc, event().captured_at_utc)
        delay = self.outbox.mark_retry("event-1", 10, "still offline", 30.0)
        self.assertEqual(delay, 30.0)

    def test_delayed_oldest_event_blocks_newer_events(self) -> None:
        self.outbox.enqueue(event("oldest"))
        self.now[0] += 0.1
        self.outbox.enqueue(event("newer"))
        self.outbox.mark_retry("oldest", 0, "offline", None)

        self.assertEqual(self.outbox.due(10), [])
        self.now[0] += 2.0
        self.assertEqual(
            [item.event.event_id for item in self.outbox.due(10)],
            ["oldest", "newer"],
        )

    def test_delivered_is_deleted_and_terminal_error_is_quarantined(self) -> None:
        self.outbox.enqueue(event("delivered"))
        self.outbox.enqueue(event("bad"))
        self.outbox.mark_delivered("delivered")
        self.outbox.mark_dead("bad", "http_400")

        self.assertEqual(self.outbox.due(10), [])
        self.assertEqual(self.outbox.counts(), (0, 1))

    def test_row_limit_prevents_unbounded_disk_growth(self) -> None:
        limited = Outbox(
            self.path,
            retry_base_seconds=2.0,
            retry_max_seconds=10.0,
            max_rows=1,
            clock=lambda: self.now[0],
        )
        limited.enqueue(event("first"))
        with self.assertRaises(OutboxFullError):
            limited.enqueue(event("overflow"))


class KindPartitioningTests(unittest.TestCase):
    """Telemetry order is preserved; acks are not held hostage to it.

    The blocking in due() is deliberate for telemetry — a retry must not
    reorder observations. Applied to acks it is a correctness bug: the backend
    expires a command with no ack and charges its granted volume to the daily
    budget, so an ack delayed behind a backed-off observation turns into water
    the plant never received being subtracted from what it may still receive.
    """

    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.now = [1000.0]
        self.path = Path(self.tempdir.name) / "state" / "outbox.sqlite3"
        self.outbox = self.open()
        self.outbox.initialize()

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def open(self) -> Outbox:
        return Outbox(
            self.path,
            retry_base_seconds=2.0,
            retry_max_seconds=10.0,
            clock=lambda: self.now[0],
        )

    def test_enqueue_defaults_to_telemetry(self) -> None:
        self.outbox.enqueue(event("obs"))
        self.assertEqual(
            [item.event.event_id for item in self.outbox.due(10)], ["obs"]
        )
        self.assertEqual(self.outbox.due(10, kind=KIND_ACK), [])

    def test_a_backed_off_observation_does_not_hold_up_an_ack(self) -> None:
        self.outbox.enqueue(event("oldest-observation"))
        self.now[0] += 0.1
        self.outbox.enqueue(event("ack-1"), kind=KIND_ACK)
        self.outbox.mark_retry("oldest-observation", 0, "offline", None)

        self.assertEqual(self.outbox.due(10, kind=KIND_TELEMETRY), [])
        self.assertEqual(
            [item.event.event_id for item in self.outbox.due(10, kind=KIND_ACK)],
            ["ack-1"],
        )

    def test_ordering_is_still_preserved_within_a_kind(self) -> None:
        self.outbox.enqueue(event("ack-old"), kind=KIND_ACK)
        self.now[0] += 0.1
        self.outbox.enqueue(event("ack-new"), kind=KIND_ACK)
        self.outbox.mark_retry("ack-old", 0, "offline", None)

        self.assertEqual(self.outbox.due(10, kind=KIND_ACK), [])
        self.now[0] += 2.0
        self.assertEqual(
            [item.event.event_id for item in self.outbox.due(10, kind=KIND_ACK)],
            ["ack-old", "ack-new"],
        )

    def test_counts_are_repo_wide_by_default_and_filterable(self) -> None:
        self.outbox.enqueue(event("obs"))
        self.outbox.enqueue(event("ack-1"), kind=KIND_ACK)
        self.assertEqual(self.outbox.counts(), (2, 0))
        self.assertEqual(self.outbox.counts(kind=KIND_TELEMETRY), (1, 0))
        self.assertEqual(self.outbox.counts(kind=KIND_ACK), (1, 0))

    def test_an_unknown_kind_raises_rather_than_vanishing(self) -> None:
        """enqueue uses INSERT OR IGNORE, which swallows CHECK violations.

        Left to the database alone, a typo'd kind would be dropped and reported
        as ``False`` — the same answer a legitimately duplicated event_id gets.
        """

        with self.assertRaises(ValueError):
            self.outbox.enqueue(event("bogus"), kind="urgent")
        self.assertEqual(self.outbox.counts(), (0, 0))


class SchemaMigrationTests(unittest.TestCase):
    """The board in the field has a populated queue and no migration framework.

    CREATE TABLE IF NOT EXISTS silently does nothing on an existing database, so
    without an explicit ALTER the new column never reaches the one machine that
    matters. Recreating the table instead would throw away undelivered
    measurements.
    """

    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.now = [1000.0]
        self.path = Path(self.tempdir.name) / "state" / "outbox.sqlite3"

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def write_pre_kind_database(self) -> None:
        """The schema as it shipped, with one undelivered row already in it."""

        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path)
        with connection:
            connection.execute(
                """
                CREATE TABLE telemetry_outbox (
                    event_id TEXT PRIMARY KEY,
                    payload_json TEXT NOT NULL,
                    created_at_epoch REAL NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
                    next_attempt_epoch REAL NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending'
                        CHECK (status IN ('pending', 'dead')),
                    last_error TEXT
                )
                """
            )
            connection.execute(
                """
                INSERT INTO telemetry_outbox(
                    event_id, payload_json, created_at_epoch, next_attempt_epoch
                ) VALUES (?, ?, ?, ?)
                """,
                (
                    "legacy-row",
                    json.dumps(event("legacy-row").to_record()),
                    900.0,
                    900.0,
                ),
            )
        connection.close()

    def outbox(self) -> Outbox:
        return Outbox(
            self.path,
            retry_base_seconds=2.0,
            retry_max_seconds=10.0,
            clock=lambda: self.now[0],
        )

    def test_an_existing_queue_gains_the_column_without_losing_rows(self) -> None:
        self.write_pre_kind_database()
        outbox = self.outbox()
        outbox.initialize()

        self.assertEqual(
            [item.event.event_id for item in outbox.due(10)], ["legacy-row"]
        )

    def test_pre_existing_rows_are_backfilled_as_telemetry(self) -> None:
        """They are telemetry by definition: acks postdate the column."""

        self.write_pre_kind_database()
        self.outbox().initialize()

        connection = sqlite3.connect(self.path)
        kinds = connection.execute("SELECT kind FROM telemetry_outbox").fetchall()
        connection.close()
        self.assertEqual(kinds, [(KIND_TELEMETRY,)])

    def test_initialize_is_idempotent(self) -> None:
        """It runs on every service start, including right after a migration."""

        self.write_pre_kind_database()
        outbox = self.outbox()
        outbox.initialize()
        outbox.initialize()
        self.assertEqual(outbox.counts(), (1, 0))

    def test_the_migrated_schema_carries_the_same_check_as_a_fresh_one(self) -> None:
        """Otherwise a bad value passes on a dev box and lands on the board.

        The CHECK is written once and reused by both the CREATE and the ALTER for
        exactly this reason; this asserts the two paths really did agree.
        """

        self.write_pre_kind_database()
        self.outbox().initialize()

        fresh_path = self.path.parent / "fresh.sqlite3"
        Outbox(
            fresh_path,
            retry_base_seconds=2.0,
            retry_max_seconds=10.0,
            clock=lambda: self.now[0],
        ).initialize()

        for label, path in (("migrated", self.path), ("fresh", fresh_path)):
            with self.subTest(schema=label):
                connection = sqlite3.connect(path)
                try:
                    with self.assertRaises(sqlite3.IntegrityError):
                        with connection:
                            connection.execute(
                                """
                                INSERT INTO telemetry_outbox(
                                    event_id, payload_json, created_at_epoch,
                                    next_attempt_epoch, kind
                                ) VALUES ('x', '{}', 1.0, 1.0, 'urgent')
                                """
                            )
                finally:
                    connection.close()


if __name__ == "__main__":
    unittest.main()
