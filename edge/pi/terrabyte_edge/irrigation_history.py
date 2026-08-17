"""Durable log of irrigation that actually happened, queryable by pot and time.

Two questions block the safety envelope in ``irrigation/decision.py`` and both
are answered from here:

* ``hours_since_last_irrigation`` — the minimum-interval gate, and the
  redistribution term in ``irrigation/volume.py``. Without it the service passed
  ``None`` and the formula assumed "watered three days ago" on every reading,
  which is the assumption that under-states how wet the pot already is.
* ``dispensed_today_ml`` — the daily-budget gate.

**An event log, not a field on the relay.** The command relay is one writer of
three: a cloud-commanded irrigation (its ack), an edge-autonomous emergency dose
(``autonomy.py``, t=3) and a manual bench test all move real water and all have
to be visible to the next decision. A counter living inside the relay would be
private to one of those writers and would have to be rewritten as soon as the
second one appeared.

**What is recorded is delivery, never intent.** A decision that says IRRIGATE is
not an entry here; only a dispense that a downstream actually reported is. The
temptation runs the other way — recording the decision is easier and errs
"safe", since an over-counted budget only ever withholds water — but it corrupts
``hours_since_last_irrigation``, and a model told that a bone-dry pot was watered
an hour ago will keep withholding while the plant dries out. Fabricated history
fails closed on the first cycle and open on every one after it.

Same SQLite file as ``outbox.py``, separate table. One file keeps the
store-and-forward queue and the irrigation record inside a single fsync domain,
so a power cut cannot leave an ack persisted with no matching volume. There is
no migration framework here: ``CREATE TABLE IF NOT EXISTS`` is the whole
migration for a table this new, and a *column* added later must follow
``Outbox._migrate`` (``PRAGMA table_info`` then ``ALTER TABLE``) rather than a
drop and recreate — the gateway in the field has a live database.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
import sqlite3
import time
import uuid
from typing import Callable, Iterator


# Who ordered the water. Kept as a column rather than inferred, because the three
# writers have different authorities: a cloud command was authorised by the
# server's Governor against its own budget, an autonomous dose was not authorised
# by anyone, and a manual test was authorised by whoever was standing at the
# bench. All three count against the edge budget; only their provenance differs.
SOURCE_CLOUD_COMMAND = "cloud_command"
SOURCE_EDGE_AUTONOMOUS = "edge_autonomous"
SOURCE_MANUAL = "manual"
SOURCES = (SOURCE_CLOUD_COMMAND, SOURCE_EDGE_AUTONOMOUS, SOURCE_MANUAL)

# The window the daily budget is measured over, matching
# ``IrrigationProperties.budgetWindow()`` on the server (``Duration.ofHours(24)``).
#
# A rolling 24 hours rather than "since local midnight", for two reasons.
#
# 1. The edge must never permit more than the cloud would (`D16`/`D17`). A
#    counter that resets at midnight allows a full budget at 23:55 and another
#    at 00:05 — two days' water in ten minutes — while the server, measuring the
#    same pot over a rolling window, would have refused the second one. Matching
#    the server's window is the only way the edge gate stays no wider than it.
# 2. A midnight boundary needs a correct local timezone, and the gateway boots
#    without one: NTP may not have synced (the autonomy state machine has a
#    SAFE_HOLD for exactly that) and the timezone is deployment configuration
#    nobody verifies. A 24-hour lookback needs only differences between
#    timestamps taken from the same clock, which is what ``outbox.py`` already
#    relies on.
BUDGET_WINDOW_SECONDS = 24.0 * 3600.0


@dataclass(frozen=True)
class IrrigationRecord:
    """One delivery of water to one pot."""

    record_id: str
    node_id: str
    dispensed_at_epoch: float
    volume_ml: float
    source: str
    command_id: str | None = None


class IrrigationHistory:
    """The irrigation event log, keyed by node and time."""

    def __init__(
        self,
        path: Path,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.path = path
        self.clock = clock

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=30.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 30000")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def initialize(self) -> None:
        """Create the table and indexes if they are not there yet.

        Safe against the populated database on the board: it touches nothing that
        ``telemetry_outbox`` owns. The PRAGMAs are repeated from ``Outbox`` rather
        than assumed, because whichever store initializes first sets them for the
        whole file, and irrigation records are the rows that must not be lost —
        losing one lets the next cycle water a pot that was just watered.
        """

        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = FULL")
            connection.execute(
                f"""
                CREATE TABLE IF NOT EXISTS irrigation_events (
                    record_id TEXT PRIMARY KEY,
                    node_id TEXT NOT NULL,
                    dispensed_at_epoch REAL NOT NULL,
                    volume_ml REAL NOT NULL CHECK (volume_ml > 0),
                    source TEXT NOT NULL CHECK (source IN (
                        {", ".join(f"'{source}'" for source in SOURCES)}
                    )),
                    command_id TEXT
                )
                """
            )
            # Both queries are "this node, back to a cutoff", so node_id has to
            # precede the time key or every lookup scans four pots' worth of
            # history.
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS irrigation_events_node_time
                    ON irrigation_events(node_id, dispensed_at_epoch)
                """
            )
            # A command may be acked more than once: QoS 1 duplicates exist on
            # both hops and the firmware's eight-entry ring buffer forgets ids.
            # Counting one delivery twice would subtract water the plant never
            # got from what it may still get, so the second insert is dropped
            # rather than summed. Partial index: autonomous and manual doses have
            # no command id and must not collide with each other on NULL.
            connection.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS irrigation_events_command
                    ON irrigation_events(command_id)
                    WHERE command_id IS NOT NULL
                """
            )

    def record(
        self,
        *,
        node_id: str,
        volume_ml: float,
        source: str,
        command_id: str | None = None,
        at_epoch: float | None = None,
        record_id: str | None = None,
    ) -> bool:
        """Record water that was delivered. ``False`` means it already was.

        ``volume_ml`` must be positive: this table is water that moved, and a
        command that delivered nothing (rejected by the firmware interlock, or
        stopped before the pump ran) is a command-state fact for the server, not
        an irrigation. Recording a zero would push out
        ``hours_since_last_irrigation`` on the strength of an event that changed
        no moisture.

        A partial delivery is a real entry: a watchdog that stopped the pump
        halfway still moved half the dose, and the ack reports what it managed.
        """

        if source not in SOURCES:
            raise ValueError(f"unknown irrigation source {source!r}; expected {SOURCES}")
        if not node_id:
            raise ValueError("node_id must not be empty")
        volume = float(volume_ml)
        if volume <= 0.0:
            raise ValueError("volume_ml must be positive; nothing delivered is not an event")
        at = self.clock() if at_epoch is None else float(at_epoch)
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO irrigation_events(
                    record_id, node_id, dispensed_at_epoch, volume_ml, source,
                    command_id
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    record_id or str(uuid.uuid4()),
                    node_id,
                    at,
                    volume,
                    source,
                    command_id,
                ),
            )
        return cursor.rowcount == 1

    def last_dispensed_epoch(self, node_id: str) -> float | None:
        """When this pot was last watered, or ``None`` if it never was."""

        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT MAX(dispensed_at_epoch) AS latest FROM irrigation_events
                WHERE node_id = ?
                """,
                (node_id,),
            ).fetchone()
        return None if row is None or row["latest"] is None else float(row["latest"])

    def hours_since_last_irrigation(self, node_id: str) -> float | None:
        """Hours since this pot was last watered, or ``None`` if it never was.

        ``None`` rather than a large number, so the caller decides what "never"
        means. The two callers want different things: the minimum-interval gate
        wants "not blocked", while the model's feature vector has a canonical
        range it cannot be handed an unbounded value for.

        Clamped at zero. A record timestamped slightly in the future — an NTP
        step backwards between the dispense and this query — would otherwise
        produce a negative age, and a negative age passes the interval gate.
        """

        last = self.last_dispensed_epoch(node_id)
        if last is None:
            return None
        return max(0.0, (self.clock() - last) / 3600.0)

    def dispensed_ml_since(self, node_id: str, since_epoch: float) -> float:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT COALESCE(SUM(volume_ml), 0.0) AS total FROM irrigation_events
                WHERE node_id = ? AND dispensed_at_epoch >= ?
                """,
                (node_id, float(since_epoch)),
            ).fetchone()
        return 0.0 if row is None else float(row["total"])

    def dispensed_today_ml(self, node_id: str) -> float:
        """Volume delivered to this pot in the trailing 24 hours, all sources.

        "Today" is the rolling window, not the calendar day — see
        :data:`BUDGET_WINDOW_SECONDS` for why the boundary is not local midnight.
        """

        return self.dispensed_ml_since(
            node_id, self.clock() - BUDGET_WINDOW_SECONDS
        )

    def recent(
        self, node_id: str, *, since_epoch: float, limit: int = 100
    ) -> list[IrrigationRecord]:
        """Newest first. For the autonomy state machine and for diagnostics."""

        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT record_id, node_id, dispensed_at_epoch, volume_ml, source,
                       command_id
                FROM irrigation_events
                WHERE node_id = ? AND dispensed_at_epoch >= ?
                ORDER BY dispensed_at_epoch DESC, record_id
                LIMIT ?
                """,
                (node_id, float(since_epoch), limit),
            ).fetchall()
        return [
            IrrigationRecord(
                record_id=row["record_id"],
                node_id=row["node_id"],
                dispensed_at_epoch=float(row["dispensed_at_epoch"]),
                volume_ml=float(row["volume_ml"]),
                source=row["source"],
                command_id=row["command_id"],
            )
            for row in rows
        ]
