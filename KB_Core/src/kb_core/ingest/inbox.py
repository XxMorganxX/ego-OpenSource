"""The push inbox: durable Postgres staging of webhook events (the local cache)."""

from dataclasses import dataclass
from datetime import datetime

from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool


@dataclass
class InboxEvent:
    id: int
    source: str
    external_id: str | None
    event_type: str
    payload: dict
    received_at: datetime


class Inbox:
    def __init__(self, pool: ConnectionPool) -> None:
        self._pool = pool

    def append(self, source: str, *, external_id: str | None, event_type: str,
               payload: dict) -> int:
        with self._pool.connection() as conn:
            return conn.execute(
                """INSERT INTO ingest_inbox (source, external_id, event_type, payload)
                   VALUES (%(s)s, %(e)s, %(t)s, %(p)s) RETURNING id""",
                {"s": source, "e": external_id, "t": event_type, "p": Jsonb(payload)},
            ).fetchone()["id"]

    def claim_pending(self, source: str, *, limit: int = 500) -> "list[InboxEvent]":
        with self._pool.connection() as conn:
            rows = conn.execute(
                """SELECT id, source, external_id, event_type, payload, received_at
                   FROM ingest_inbox
                   WHERE source = %(s)s AND processed_at IS NULL
                   ORDER BY received_at, id LIMIT %(l)s""",
                {"s": source, "l": limit},
            ).fetchall()
        return [InboxEvent(**r) for r in rows]

    def mark_processed(self, ids: "list[int]") -> None:
        if not ids:
            return
        with self._pool.connection() as conn:
            conn.execute(
                "UPDATE ingest_inbox SET status = 'processed', processed_at = now() "
                "WHERE id = ANY(%s)", (ids,),
            )

    def annotate(self, id: int, note: str) -> None:
        """Record why a PROCESSED event changed nothing (e.g. a stale manifest) without
        failing it: the row keeps its status and carries the reason in `error`."""
        with self._pool.connection() as conn:
            conn.execute("UPDATE ingest_inbox SET error = %(err)s WHERE id = %(id)s",
                         {"err": note, "id": id})

    def mark_failed(self, id: int, error: str) -> None:
        with self._pool.connection() as conn:
            conn.execute(
                "UPDATE ingest_inbox SET status = 'failed', processed_at = now(), "
                "error = %(err)s, attempts = attempts + 1 WHERE id = %(id)s",
                {"err": error, "id": id},
            )
