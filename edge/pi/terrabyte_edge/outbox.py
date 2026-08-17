"""Durable SQLite outbox for store-and-forward delivery."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import json
from pathlib import Path
import sqlite3
import time
from typing import Callable, Iterator

from .protocol import CommandAck, Event, QueuedMessage


# Message families that share the queue. They share it because they share the
# durability requirement — both must survive a power cut — but they must NOT
# share the head-of-line blocking below, which is why the column exists.
#
#   telemetry  observations. Order matters: see due().
#   ack        command outcomes. Time-critical: the backend expires a command
#              whose ack has not arrived and then charges its granted volume to
#              the daily budget anyway. An ack stuck behind a backed-off
#              telemetry row therefore becomes a phantom budget deduction —
#              water the plant never got, subtracted from what it may get.
KIND_TELEMETRY = "telemetry"
KIND_ACK = "ack"
KINDS = (KIND_TELEMETRY, KIND_ACK)

# The row's ``kind`` also names its decoder. The two families are different
# dataclasses with different required fields, so a single ``from_record`` would
# have to guess from the keys present — and a telemetry row queued by an older
# build legitimately lacks keys, which is exactly the case a guess gets wrong.
# Keeping the mapping beside KINDS means adding a family is one entry in each.
_RECORD_CODECS: dict[str, Callable[[dict], QueuedMessage]] = {
    KIND_TELEMETRY: Event.from_record,
    KIND_ACK: CommandAck.from_record,
}


@dataclass(frozen=True)
class OutboxItem:
    event: QueuedMessage
    attempts: int


class OutboxFullError(RuntimeError):
    """Raised before enqueueing when the configured row limit is reached."""


class Outbox:
    def __init__(
        self,
        path: Path,
        *,
        retry_base_seconds: float,
        retry_max_seconds: float,
        max_rows: int = 100_000,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.path = path
        self.retry_base_seconds = retry_base_seconds
        self.retry_max_seconds = retry_max_seconds
        self.max_rows = max_rows
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

    # Verbatim in both the CREATE and the ALTER below. There is no migration
    # framework here, so the two paths are the only definition of the schema and
    # they must not drift: a CHECK present on a freshly created dev database but
    # absent on the board that was migrated in place is exactly the divergence
    # that lets a bad value pass local tests and corrupt the live queue.
    _KIND_COLUMN = (
        "kind TEXT NOT NULL DEFAULT '"
        + KIND_TELEMETRY
        + "' CHECK (kind IN ("
        + ", ".join(f"'{kind}'" for kind in KINDS)
        + "))"
    )

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = FULL")
            connection.execute(
                f"""
                CREATE TABLE IF NOT EXISTS telemetry_outbox (
                    event_id TEXT PRIMARY KEY,
                    payload_json TEXT NOT NULL,
                    created_at_epoch REAL NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
                    next_attempt_epoch REAL NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending'
                        CHECK (status IN ('pending', 'dead')),
                    last_error TEXT,
                    {self._KIND_COLUMN}
                )
                """
            )
            self._migrate(connection)
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS telemetry_outbox_pending
                    ON telemetry_outbox(status, next_attempt_epoch, created_at_epoch)
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS telemetry_outbox_order
                    ON telemetry_outbox(status, created_at_epoch, event_id)
                """
            )
            # due() filters on kind before ordering, so kind has to precede the
            # sort keys or every ack scan walks the whole telemetry backlog.
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS telemetry_outbox_kind_order
                    ON telemetry_outbox(status, kind, created_at_epoch, event_id)
                """
            )

    def _migrate(self, connection: sqlite3.Connection) -> None:
        """Bring an existing database up to the current schema, in place.

        ``CREATE TABLE IF NOT EXISTS`` is a no-op on a database that already
        exists, so a column added later never appears on a board that has been
        running. The gateway in the field has a populated queue (megabytes of
        undelivered observations); dropping and recreating the table to get the
        new shape would discard real measurements, so the column is added and
        the existing rows keep flowing.

        The ``DEFAULT`` on the added column is what backfills: SQLite writes it
        into every existing row, and every existing row is telemetry by
        definition — acks did not exist before this column did.
        """

        columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(telemetry_outbox)")
        }
        if "kind" not in columns:
            connection.execute(
                f"ALTER TABLE telemetry_outbox ADD COLUMN {self._KIND_COLUMN}"
            )

    def enqueue(self, event: QueuedMessage, *, kind: str = KIND_TELEMETRY) -> bool:
        # Checked here as well as in the schema, and the Python check is the one
        # that actually fires. The INSERT below is OR IGNORE — it has to be, so a
        # replayed event_id is a no-op rather than an error — but OR IGNORE also
        # swallows a CHECK violation, turning a bad kind into a silently dropped
        # row whose return value is indistinguishable from a legitimate
        # duplicate. The constraint stays in the schema as a backstop against a
        # writer that is not this method.
        if kind not in KINDS:
            raise ValueError(f"unknown outbox kind {kind!r}; expected one of {KINDS}")
        payload = json.dumps(
            event.to_record(), separators=(",", ":"), ensure_ascii=True
        )
        now = self.clock()
        with self._connect() as connection:
            row_count = connection.execute(
                "SELECT COUNT(*) FROM telemetry_outbox"
            ).fetchone()[0]
            if row_count >= self.max_rows:
                raise OutboxFullError(
                    f"outbox row limit reached ({self.max_rows})"
                )
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO telemetry_outbox(
                    event_id, payload_json, created_at_epoch, next_attempt_epoch,
                    kind
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (event.event_id, payload, now, now, kind),
            )
        return cursor.rowcount == 1

    def due(self, limit: int, *, kind: str = KIND_TELEMETRY) -> list[OutboxItem]:
        """Rows of one kind that are ready to send, in capture order.

        Scoped to a single kind, and callers drain each kind separately, because
        the head-of-line blocking below is per kind by design. Draining the
        whole table at once would put an ack behind a telemetry row that is
        sleeping off a backoff, and an ack that arrives late is worse than one
        that arrives out of order — see the KINDS comment.
        """

        decode = _RECORD_CODECS.get(kind)
        if decode is None:
            raise ValueError(f"unknown outbox kind {kind!r}; expected one of {KINDS}")
        now = self.clock()
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT payload_json, attempts, next_attempt_epoch
                FROM telemetry_outbox
                WHERE status = 'pending' AND kind = ?
                ORDER BY created_at_epoch, event_id
                LIMIT ?
                """,
                (kind, limit),
            ).fetchall()
        # A delayed oldest item blocks newer items *of the same kind*. Otherwise
        # a network retry would silently reorder observations on the backend.
        due_rows = []
        for row in rows:
            if row["next_attempt_epoch"] > now:
                break
            due_rows.append(row)
        return [
            OutboxItem(decode(json.loads(row["payload_json"])), row["attempts"])
            for row in due_rows
        ]

    def mark_delivered(self, event_id: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM telemetry_outbox WHERE event_id = ?", (event_id,)
            )

    def mark_retry(
        self, event_id: str, attempts: int, error: str, retry_after: float | None
    ) -> float:
        exponential = self.retry_base_seconds * (2 ** min(attempts, 20))
        delay = min(self.retry_max_seconds, exponential)
        if retry_after is not None:
            # Retry-After is a server-mandated lower bound, even when it is
            # longer than the locally configured exponential-backoff cap.
            delay = max(delay, retry_after)
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE telemetry_outbox
                SET attempts = attempts + 1,
                    next_attempt_epoch = ?,
                    last_error = ?
                WHERE event_id = ? AND status = 'pending'
                """,
                (self.clock() + delay, error[:256], event_id),
            )
        return delay

    def mark_dead(self, event_id: str, error: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE telemetry_outbox
                SET status = 'dead', attempts = attempts + 1, last_error = ?
                WHERE event_id = ?
                """,
                (error[:256], event_id),
            )

    def counts(self, *, kind: str | None = None) -> tuple[int, int]:
        """``(pending, dead)``. Every kind by default, since the display shows
        one queue depth and a caller that meant "all" must not have to enumerate
        the kinds and risk missing one that was added later."""

        with self._connect() as connection:
            if kind is None:
                rows = connection.execute(
                    "SELECT status, COUNT(*) FROM telemetry_outbox GROUP BY status"
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT status, COUNT(*) FROM telemetry_outbox
                    WHERE kind = ? GROUP BY status
                    """,
                    (kind,),
                ).fetchall()
        counted = dict(rows)
        return counted.get("pending", 0), counted.get("dead", 0)
