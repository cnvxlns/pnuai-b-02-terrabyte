from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from terrabyte_edge.outbox import Outbox, OutboxFullError
from terrabyte_edge.protocol import Event


def event(event_id: str = "event-1", ppfd: float | None = 300.0) -> Event:
    return Event(
        event_id=event_id,
        context_id="ctx-1",
        captured_at_utc="2026-07-21T04:05:06Z",
        node_id="node-1",
        sequence=1,
        uptime_ms=100,
        air_temperature_c=20.0,
        relative_humidity_pct=50.0,
        ppfd_umol_m2_s=ppfd,
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

    def test_null_ppfd_survives_reopening(self) -> None:
        self.assertTrue(self.outbox.enqueue(event(ppfd=None)))

        reopened = Outbox(
            self.path,
            retry_base_seconds=2.0,
            retry_max_seconds=10.0,
            clock=lambda: self.now[0],
        )
        item = reopened.due(10)[0]
        self.assertIsNone(item.event.ppfd_umol_m2_s)
        self.assertIn("ppfd_umol_m2_s", item.event.to_record())

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


if __name__ == "__main__":
    unittest.main()
