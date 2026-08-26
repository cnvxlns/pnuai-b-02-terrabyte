"""Durable SQLite outbox for store-and-forward delivery."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import json
from pathlib import Path
import sqlite3
import time
from typing import Callable, Iterator

from .protocol import CommandAck, EdgeIrrigationRecord, Event, QueuedMessage


# Telemetry and command outcomes share a durability boundary, but not a retry
# queue. A backed-off telemetry row must not hold an ack until the backend has
# expired the command and charged water that may not have moved.
KIND_TELEMETRY = "telemetry"
KIND_ACK = "ack"

# Water delivered without a command behind it. Kept apart from acks as well as
# from telemetry, because this is the queue CloudLink gates CLOUD_ONLINE on:
# counting acks there would hold the gateway in RESYNC over an ordinary
# cloud-commanded dose that the server already knows it asked for.
KIND_CONTROL = "control"

KINDS = (KIND_TELEMETRY, KIND_ACK, KIND_CONTROL)

_RECORD_CODECS: dict[str, Callable[[dict], QueuedMessage]] = {
    KIND_TELEMETRY: Event.from_record,
    KIND_ACK: CommandAck.from_record,
    KIND_CONTROL: EdgeIrrigationRecord.from_record,
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

    # Defined once for both fresh and migrated databases. Existing rows predate
    # command acks and are telemetry by definition, so the default is also the
    # in-place backfill.
    _KIND_COLUMN = (
        "kind TEXT NOT NULL DEFAULT '"
        + KIND_TELEMETRY
        + "' CHECK (kind IN ("
        + ", ".join(f"'{kind}'" for kind in KINDS)
        + "))"
    )

    # The row shape, in one place, because the migration below has to rebuild
    # the table and a second copy of this list would drift from the first.
    _COLUMNS = (
        "event_id", "payload_json", "created_at_epoch", "attempts",
        "next_attempt_epoch", "status", "last_error", "kind",
    )

    def _table_ddl(self, name: str) -> str:
        return f"""
            CREATE TABLE {name} (
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

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = FULL")
            connection.execute(
                self._table_ddl("IF NOT EXISTS telemetry_outbox")
            )
            self._migrate(connection)
            connection.executescript(
                """
                CREATE INDEX IF NOT EXISTS telemetry_outbox_pending
                    ON telemetry_outbox(status, next_attempt_epoch, created_at_epoch);
                CREATE INDEX IF NOT EXISTS telemetry_outbox_order
                    ON telemetry_outbox(status, created_at_epoch, event_id);
                CREATE INDEX IF NOT EXISTS telemetry_outbox_kind_order
                    ON telemetry_outbox(status, kind, created_at_epoch, event_id);
                """
            )

    def _migrate(self, connection: sqlite3.Connection) -> None:
        """Bring a database written by an older build up to the current schema.

        Two separate problems, because ``kind`` arrived in two steps.
        """

        columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(telemetry_outbox)")
        }
        if "kind" not in columns:
            connection.execute(
                f"ALTER TABLE telemetry_outbox ADD COLUMN {self._KIND_COLUMN}"
            )
            return

        # The column exists, but its CHECK was written when there were only two
        # kinds, and SQLite cannot alter a constraint in place. This matters more
        # than it looks: `enqueue` inserts with OR IGNORE, which swallows a CHECK
        # violation exactly the way it swallows a duplicate id. On a gateway
        # already in the field every control row would vanish with no error and
        # no log line, and the failure would only surface as a server that
        # authorises water on top of water the edge already delivered.
        table_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
            ("telemetry_outbox",),
        ).fetchone()
        if table_sql is None:
            return
        definition = table_sql["sql"] or ""
        if all(f"'{kind}'" in definition for kind in KINDS):
            return

        # The standard SQLite table rebuild. Cheap here — the outbox holds
        # pending deliveries, not history — and it runs inside the transaction
        # `_connect` opens, so a power cut leaves the old table intact. Indexes
        # go with the dropped table and `initialize` recreates them immediately
        # after this returns.
        columns_csv = ", ".join(self._COLUMNS)
        connection.execute(self._table_ddl("telemetry_outbox_migrated"))
        connection.execute(
            f"INSERT INTO telemetry_outbox_migrated({columns_csv})"
            f" SELECT {columns_csv} FROM telemetry_outbox"
        )
        connection.execute("DROP TABLE telemetry_outbox")
        connection.execute(
            "ALTER TABLE telemetry_outbox_migrated RENAME TO telemetry_outbox"
        )

    def enqueue(
        self, event: QueuedMessage, *, kind: str = KIND_TELEMETRY
    ) -> bool:
        # INSERT OR IGNORE also swallows CHECK failures. Validate first so a bad
        # kind cannot masquerade as a harmless duplicate event id.
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

    def due(
        self, limit: int, *, kind: str = KIND_TELEMETRY
    ) -> list[OutboxItem]:
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
        # A delayed oldest item blocks newer items of the same kind. Keeping the
        # kinds separate preserves telemetry order without delaying outcomes.
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
        with self._connect() as connection:
            if kind is None:
                rows = connection.execute(
                    "SELECT status, COUNT(*) FROM telemetry_outbox GROUP BY status"
                ).fetchall()
            else:
                if kind not in KINDS:
                    raise ValueError(
                        f"unknown outbox kind {kind!r}; expected one of {KINDS}"
                    )
                rows = connection.execute(
                    """
                    SELECT status, COUNT(*) FROM telemetry_outbox
                    WHERE kind = ? GROUP BY status
                    """,
                    (kind,),
                ).fetchall()
        counted = dict(rows)
        return counted.get("pending", 0), counted.get("dead", 0)
