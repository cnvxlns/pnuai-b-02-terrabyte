"""The irrigation event log: what the safety envelope reads before it decides."""

from pathlib import Path
import sqlite3
import tempfile
import unittest

from terrabyte_edge.irrigation_history import (
    BUDGET_WINDOW_SECONDS,
    SOURCE_CLOUD_COMMAND,
    SOURCE_EDGE_AUTONOMOUS,
    SOURCE_MANUAL,
    IrrigationHistory,
)
from terrabyte_edge.outbox import KIND_TELEMETRY, Outbox
from terrabyte_edge.protocol import Event


NOW = 1_800_000_000.0
HOUR = 3600.0


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


class HistoryTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "outbox.sqlite3"
        self.now = NOW
        self.history = IrrigationHistory(self.path, clock=lambda: self.now)
        self.history.initialize()

    def record(self, *, node_id="node-1", volume_ml=60.0, ago=0.0, **kwargs) -> bool:
        return self.history.record(
            node_id=node_id,
            volume_ml=volume_ml,
            source=kwargs.pop("source", SOURCE_CLOUD_COMMAND),
            at_epoch=self.now - ago,
            **kwargs,
        )


class QueryTests(HistoryTestCase):
    def test_a_pot_that_was_never_watered_reports_none(self) -> None:
        """``None``, not a large number: the caller decides what "never" means.

        The interval gate wants "not blocked" and the feature vector wants a value
        inside the range the model was trained on, and those are different answers.
        """

        self.assertIsNone(self.history.hours_since_last_irrigation("node-1"))
        self.assertEqual(self.history.dispensed_today_ml("node-1"), 0.0)

    def test_hours_since_the_most_recent_dose(self) -> None:
        self.record(ago=9.0 * HOUR, command_id="cmd-old")
        self.record(ago=2.5 * HOUR, command_id="cmd-new")

        self.assertAlmostEqual(
            self.history.hours_since_last_irrigation("node-1"), 2.5, places=6
        )

    def test_a_clock_that_stepped_backwards_cannot_report_a_negative_age(
        self,
    ) -> None:
        """A negative age passes the minimum-interval gate.

        An NTP correction between the dispense and this query is enough to produce
        one, and the gateway does correct its clock after boot.
        """

        self.record(ago=-30.0 * 60.0, command_id="cmd-future")
        self.assertEqual(self.history.hours_since_last_irrigation("node-1"), 0.0)

    def test_the_daily_total_is_a_rolling_window(self) -> None:
        """Matched to the server's ``budgetWindow()``, which is 24 rolling hours.

        A calendar-day counter would allow a full budget at 23:55 and another at
        00:05 — two days' water in ten minutes — where the server, measuring the
        same pot over a rolling window, would have refused the second.
        """

        self.record(volume_ml=100.0, ago=23.0 * HOUR, command_id="cmd-inside")
        self.record(volume_ml=100.0, ago=25.0 * HOUR, command_id="cmd-outside")

        self.assertEqual(self.history.dispensed_today_ml("node-1"), 100.0)
        # And it moves: two hours later the one inside has aged out too.
        self.now += 2.0 * HOUR
        self.assertEqual(self.history.dispensed_today_ml("node-1"), 0.0)

    def test_every_source_counts_against_the_same_budget(self) -> None:
        """A cloud command and an autonomous dose put the same water in the pot."""

        self.record(volume_ml=40.0, source=SOURCE_CLOUD_COMMAND, command_id="cmd-1")
        self.record(volume_ml=60.0, source=SOURCE_EDGE_AUTONOMOUS)
        self.record(volume_ml=10.0, source=SOURCE_MANUAL)

        self.assertEqual(self.history.dispensed_today_ml("node-1"), 110.0)

    def test_pots_do_not_share_a_history(self) -> None:
        self.record(node_id="node-1", volume_ml=50.0, command_id="cmd-1")

        self.assertEqual(self.history.dispensed_today_ml("node-2"), 0.0)
        self.assertIsNone(self.history.hours_since_last_irrigation("node-2"))

    def test_recent_returns_newest_first(self) -> None:
        self.record(volume_ml=10.0, ago=5.0 * HOUR, command_id="cmd-old")
        self.record(volume_ml=20.0, ago=1.0 * HOUR, command_id="cmd-new")

        records = self.history.recent(
            "node-1", since_epoch=self.now - BUDGET_WINDOW_SECONDS
        )
        self.assertEqual([record.volume_ml for record in records], [20.0, 10.0])
        self.assertEqual(records[0].command_id, "cmd-new")
        self.assertEqual(records[0].source, SOURCE_CLOUD_COMMAND)


class WriteTests(HistoryTestCase):
    def test_the_same_command_is_never_counted_twice(self) -> None:
        """QoS 1 duplicates exist on both hops, and the firmware's eight-entry
        ring buffer forgets ids, so the same ack can arrive more than once.

        Double-counting subtracts water the plant never got from what it may still
        get, so the second insert is dropped rather than summed.
        """

        self.assertTrue(self.record(volume_ml=60.0, command_id="cmd-1"))
        self.assertFalse(self.record(volume_ml=60.0, command_id="cmd-1"))
        self.assertEqual(self.history.dispensed_today_ml("node-1"), 60.0)

    def test_doses_with_no_command_id_do_not_collide(self) -> None:
        """Autonomous and manual doses have no command to be idempotent about.

        A plain unique index would treat their NULLs as equal in some engines and
        silently drop the second dose, which is why the index is partial.
        """

        self.assertTrue(self.record(volume_ml=60.0, source=SOURCE_EDGE_AUTONOMOUS))
        self.assertTrue(self.record(volume_ml=60.0, source=SOURCE_EDGE_AUTONOMOUS))
        self.assertEqual(self.history.dispensed_today_ml("node-1"), 120.0)

    def test_a_delivery_of_nothing_is_not_an_event(self) -> None:
        """Recording a zero would push ``hours_since_last_irrigation`` forward on
        the strength of something that changed no moisture. A command that
        delivered nothing is a command-state fact for the server."""

        with self.assertRaises(ValueError):
            self.record(volume_ml=0.0, command_id="cmd-1")
        with self.assertRaises(ValueError):
            self.record(volume_ml=-10.0, command_id="cmd-2")

    def test_a_partial_delivery_is_a_real_entry(self) -> None:
        """A watchdog that stopped the pump halfway still moved half the dose."""

        self.assertTrue(self.record(volume_ml=27.5, command_id="cmd-1"))
        self.assertEqual(self.history.dispensed_today_ml("node-1"), 27.5)

    def test_an_unknown_source_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            self.history.record(
                node_id="node-1", volume_ml=10.0, source="wishful_thinking"
            )

    def test_the_schema_refuses_a_bad_row_from_another_writer(self) -> None:
        """The Python checks above are the ones that fire; this is the backstop.

        ``record`` inserts OR IGNORE — it has to, so a replayed command is a no-op
        rather than an error — and OR IGNORE would swallow a CHECK violation, so
        the constraint exists for a writer that is not this class.
        """

        connection = sqlite3.connect(self.path)
        self.addCleanup(connection.close)
        with self.assertRaises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO irrigation_events(
                    record_id, node_id, dispensed_at_epoch, volume_ml, source
                ) VALUES ('r-1', 'node-1', 0.0, 10.0, 'nonsense')
                """
            )


class SharedDatabaseTests(unittest.TestCase):
    """The board in the field has a live outbox in this same file.

    There is no migration framework, so adding the log has to be additive: the
    queue keeps its rows and keeps flowing.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "outbox.sqlite3"

    def outbox(self) -> Outbox:
        return Outbox(
            self.path,
            retry_base_seconds=1.0,
            retry_max_seconds=10.0,
            clock=lambda: NOW,
        )

    def test_the_log_can_be_added_to_a_populated_queue(self) -> None:
        outbox = self.outbox()
        outbox.initialize()
        self.assertTrue(outbox.enqueue(event("queued-before")))

        history = IrrigationHistory(self.path, clock=lambda: NOW)
        history.initialize()
        history.record(
            node_id="node-1", volume_ml=60.0, source=SOURCE_CLOUD_COMMAND,
            command_id="cmd-1",
        )

        # The observation is still there, still deliverable.
        self.assertEqual(outbox.counts(), (1, 0))
        due = outbox.due(10, kind=KIND_TELEMETRY)
        self.assertEqual([item.event.event_id for item in due], ["queued-before"])
        self.assertEqual(history.dispensed_today_ml("node-1"), 60.0)

    def test_either_store_may_initialize_first(self) -> None:
        """Whichever runs first sets the file's journal mode for both."""

        history = IrrigationHistory(self.path, clock=lambda: NOW)
        history.initialize()
        outbox = self.outbox()
        outbox.initialize()

        self.assertTrue(outbox.enqueue(event("queued-after")))
        self.assertEqual(outbox.counts(), (1, 0))
        connection = sqlite3.connect(self.path)
        mode = connection.execute("PRAGMA journal_mode").fetchone()[0]
        connection.close()
        self.assertEqual(mode, "wal")

    def test_initialize_is_idempotent(self) -> None:
        history = IrrigationHistory(self.path, clock=lambda: NOW)
        history.initialize()
        history.record(
            node_id="node-1", volume_ml=60.0, source=SOURCE_MANUAL
        )
        history.initialize()

        self.assertEqual(history.dispensed_today_ml("node-1"), 60.0)


if __name__ == "__main__":
    unittest.main()
