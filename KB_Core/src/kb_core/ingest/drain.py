"""Push-side ingest: drain the inbox through the same pipeline + core.

Never prunes on its own; a `manifest` event is an explicit prune (pushed-vault source).
"""

import logging
from datetime import datetime, timezone

from psycopg_pool import ConnectionPool

from kb_core.adapters.base import (DEFAULT_INGESTOR, Ingestor, PushDecoder, SyncResult,
                                   write_counter)
from kb_core.ingest.inbox import Inbox
from kb_core.ingest.pipeline import IngestContext, Pipeline
from kb_core.ingest.transforms import default_pipeline
from kb_core.knowledge_base import KnowledgeBase

log = logging.getLogger(__name__)


class InboxDrainService:
    def __init__(self, kb: KnowledgeBase, pool: ConnectionPool) -> None:
        self._kb = kb
        self._pool = pool
        self._inbox = Inbox(pool)

    def _observe(self, result: SyncResult, label: str, fn, *args, **kwargs) -> None:
        """Observability must never abort ingestion: record a failure to log, don't raise.

        A raise here, left unguarded, would skip mark_processed for the whole batch (not
        just the observability write), redelivering it forever.
        """
        try:
            fn(*args, **kwargs)
        except Exception as exc:
            result.errors.append(f"{label}: log failed: {exc}")

    def drain(self, source: str, decoder: PushDecoder, *,
              pipeline: Pipeline | None = None,
              ingestor: Ingestor = DEFAULT_INGESTOR, limit: int = 500) -> SyncResult:
        pipeline = pipeline or default_pipeline()
        ctx = IngestContext(source=source, kb=self._kb, now=datetime.now(timezone.utc))
        result = SyncResult(source=source)
        events = self._inbox.claim_pending(source, limit=limit)
        writes = write_counter(self._kb)
        processed: list[int] = []
        for ev in events:
            try:
                if ev.event_type == "delete":
                    if not ev.external_id:
                        raise ValueError("delete event missing external_id")
                    result.deleted += self._kb.delete_by_external_id(source, ev.external_id)
                elif ev.event_type == "manifest":
                    note = self._apply_manifest(source, ev.payload, result)
                    if note is not None:
                        self._inbox.annotate(ev.id, note)
                else:
                    record = pipeline.run_record(decoder.decode(ev.payload), ctx)
                    if record is not None:
                        before = writes()
                        ingestor.upsert(self._kb, record)
                        result.upserted += 1
                        if writes() != before:
                            result.changed += 1
                        # The upsert may have been a no-op (unchanged content writes no node
                        # event, see _persist_node's no-op gate), which would leave a
                        # previously-stuck record stuck forever; log_ingest_resolved declines
                        # when a node event already ended the streak, so this is a no-op for
                        # a record that never failed. No `open` set to check first: push
                        # failures are rare, so the engine looks the record up (one indexed
                        # SELECT per successful push upsert -- acceptable at push volume).
                        self._observe(result, f"{source}:{record.external_id}",
                                     self._kb.log_ingest_resolved, source,
                                     record.external_id, reason="landed")
                processed.append(ev.id)
            except Exception as exc:
                self._inbox.mark_failed(ev.id, str(exc))
                result.errors.append(f"{source}:inbox#{ev.id}: {exc}")
                # Surface it on the dashboard (ingest_inbox.status='failed' is persisted but
                # nothing reads it). Keyed by the record's external id so a later successful
                # event for the same record ends the streak via its node event; an id-less
                # event keys to the inbox row and leaves the banner only by Dismiss. No
                # `open` set: push failures are rare, so the engine looks the record up.
                self._observe(result, f"{source}:inbox#{ev.id}", self._kb.log_ingest_failed,
                             source, ev.external_id or f"inbox#{ev.id}", str(exc))
        self._inbox.mark_processed(processed)
        if events:                              # only do post-work if we drained something
            pipeline.run_post(ctx, set())       # source-scoped post-stages (no `seen` needed)
            self._record_sync(source)
        return result

    def manifest_generation(self, source: str) -> int | None:
        with self._pool.connection() as conn:
            row = conn.execute("SELECT cursor FROM sync_state WHERE source = %s", (source,)).fetchone()
        if not row or row["cursor"] is None:
            return None
        try:
            return int(row["cursor"])
        except ValueError:
            return None

    def _set_manifest_generation(self, source: str, generation: int) -> None:
        with self._pool.connection() as conn:
            conn.execute(
                """INSERT INTO sync_state (source, last_synced_at, cursor) VALUES (%s, now(), %s)
                   ON CONFLICT (source) DO UPDATE SET cursor = EXCLUDED.cursor""",
                (source, str(generation)))

    def _apply_manifest(self, source: str, payload: dict, result: SyncResult) -> str | None:
        """Prune `source` to the listed ids. Returns a note when the manifest was skipped as
        stale (the caller records it on the inbox row), None when it was applied."""
        ids = {str(x) for x in (payload.get("ids") or [])}
        generation = payload.get("generation")
        # bool is an int subclass; a string/float/missing generation is refused too, so a
        # malformed manifest fails its event instead of slipping past the stale guard.
        if not isinstance(generation, int) or isinstance(generation, bool) or generation < 0:
            raise ValueError("manifest generation must be a non-negative integer")
        last = self.manifest_generation(source)
        if last is not None and generation <= last:
            # Stale or duplicate: never prune with it, but never silently either. A
            # reinstalled agent that restarted its counter below `last` would otherwise lose
            # pruning forever with no trace.
            note = (f"stale manifest generation {generation} <= last applied {last}: "
                    "skipped, nothing pruned")
            log.warning("%s: %s", source, note)
            return note
        if not ids and not payload.get("allow_empty", False):
            raise ValueError("empty manifest refused (set allow_empty to wipe this source)")
        result.deleted += self._kb.prune(source, ids)
        self._kb.prune_empty_folders()
        self._set_manifest_generation(source, generation)
        return None

    def _record_sync(self, source: str) -> None:
        with self._pool.connection() as conn:
            conn.execute(
                """INSERT INTO sync_state (source, last_synced_at) VALUES (%s, now())
                   ON CONFLICT (source) DO UPDATE SET last_synced_at = now()""",
                (source,),
            )
