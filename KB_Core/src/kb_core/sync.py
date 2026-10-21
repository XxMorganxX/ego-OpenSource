"""Source-agnostic sync orchestration: upsert present items, mirror-prune the rest."""

from datetime import datetime, timezone

from psycopg_pool import ConnectionPool

from kb_core.adapters.base import DEFAULT_INGESTOR, Adapter, SyncResult, write_counter
from kb_core.domain import NodeRecord
from kb_core.ingest.pipeline import IngestContext, Pipeline
from kb_core.ingest.transforms import default_pipeline
from kb_core.knowledge_base import KnowledgeBase
from kb_core.paths import join_path, sanitize_slug


def _intended_path(record: NodeRecord) -> "str | None":
    """Where the record WOULD have landed (what _persist_node computes), so a stuck
    record's log row can carry a readable item name. None when the slug is
    unsanitizable — the failure may be exactly that."""
    try:
        return join_path(record.parent_path, sanitize_slug(record.slug))
    except (ValueError, AttributeError, TypeError):
        return None


class SyncService:
    def __init__(self, kb: KnowledgeBase, pool: ConnectionPool) -> None:
        self._kb = kb
        self._pool = pool

    def _observe(self, result: SyncResult, label: str, fn, *args, **kwargs) -> None:
        """Observability must never abort ingestion: record a failure to log, don't raise.

        log_ingest_failed / log_ingest_resolved are the only I/O in the per-record loop
        beyond the upsert itself; a pool blip or an unstorable error text here must not
        cost the rest of the tick its upserts, prune, or sync_state.
        """
        try:
            fn(*args, **kwargs)
        except Exception as exc:
            result.errors.append(f"{label}: log failed: {exc}")

    def sync(self, adapter: Adapter, *, prune: bool = True, dry_run: bool = False,
             pipeline: Pipeline | None = None) -> SyncResult:
        # Each record flows through the (per-source) transform pipeline before persist;
        # post stages (FlagEnrichment, RecomputeImportance) run once after the batch.
        pipeline = pipeline or default_pipeline()
        result = SyncResult(source=adapter.source)
        # A source defines how its data is persisted via an optional `ingest.py`
        # (exposed as `adapter.ingestor`); core-only sources fall back to a plain upsert.
        ingestor = getattr(adapter, "ingestor", None) or DEFAULT_INGESTOR
        ctx = IngestContext(source=adapter.source, kb=self._kb,
                            now=datetime.now(timezone.utc))
        # Optional pre-fetch hook (e.g. the Obsidian stamper): a source may mint stable ids
        # into its store before it's read. It writes, so it never runs on a dry run; its
        # errors are captured now, before fetch() resets adapter.errors.
        if not dry_run and hasattr(adapter, "prepare"):
            adapter.prepare(self._kb)
            result.errors.extend(getattr(adapter, "errors", []))
        # Records this source is currently stuck on (latest ingest_log event is
        # 'ingest_failed'), keyed by external_id. Read once per sync: it is the dedup set
        # for log_ingest_failed (a repeat failure with the same text writes nothing) and,
        # after the loop, the list to resolve for ids that left the source. A dry run
        # writes nothing, so it never touches failure state either way.
        open_failures: dict[str, dict] = {}
        if not dry_run:
            open_failures = self._kb.open_ingest_failures(adapter.source)
        seen: set[str] = set()
        writes = write_counter(self._kb)
        for record in adapter.fetch():          # raises (aborting) if source unreachable
            seen.add(record.external_id)        # present in source -> never prune it
            if dry_run:
                result.upserted += 1
                continue
            transformed = None
            try:
                transformed = pipeline.run_record(record, ctx)
                if transformed is None:
                    continue                    # dropped by a transform; keep it in `seen`
                before = writes()
                ingestor.upsert(self._kb, transformed)
                result.upserted += 1
                if writes() != before:
                    result.changed += 1
                if record.external_id in open_failures:
                    # The upsert may have been a no-op (unchanged content writes no node
                    # event, see _persist_node's no-op gate), which would leave the record
                    # stuck forever; log_ingest_resolved declines (returns False) when a
                    # node event already ended the streak, so this writes only in that gap.
                    self._observe(result, f"{record.source}:{record.external_id}",
                                 self._kb.log_ingest_resolved, record.source,
                                 record.external_id, reason="landed")
                    del open_failures[record.external_id]
            except Exception as exc:
                result.errors.append(f"{record.source}:{record.external_id}: {exc}")
                # The daemon line above is unchanged; this is what makes the failure
                # visible on the dashboard. Deduped against open_failures, so a record
                # that fails every tick costs one row, not one per tick.
                self._observe(result, f"{record.source}:{record.external_id}",
                             self._kb.log_ingest_failed, record.source, record.external_id,
                             str(exc), path=_intended_path(transformed or record),
                             open=open_failures)
        # Everything the adapter failed to read during THIS fetch. fetch() resets the
        # list as it starts, so these are observation failures only; prepare()'s errors
        # were already captured above, before that reset.
        observation_errors = list(getattr(adapter, "errors", []))
        result.errors.extend(observation_errors)
        # A prune is a MIRROR operation: it deletes every node this fetch did not report.
        # That is only sound when the fetch actually observed the whole source. A note the
        # adapter could not read, or a directory it could not list, is missing from `seen`
        # for a reason that has nothing to do with the user deleting it -- pruning on such
        # a tick destroys live data. A permissions failure that leaves a vault only partially
        # readable would let the prune take every unreadable note with it, then the folders
        # they emptied and the entities they orphaned. Absence is trustworthy only
        # when the observation behind it was complete.
        #
        # Deliberately NOT gated on per-record pipeline/upsert failures: those records are
        # in `seen` (added before the try), so the mirror is still whole -- and gating on
        # them would let a single permanently-failing note freeze pruning for the source.
        if prune and observation_errors:
            result.prune_skipped = (f"source not fully observed "
                                    f"({len(observation_errors)} read error(s))")
            prune = False
        if not dry_run and prune:
            # A stuck record that is no longer in the source is closed here; one still in
            # the source either landed (its node event or the 'landed' write above ends
            # the streak) or failed again (deduped above), so there is nothing to do for
            # it. Gated on `prune`: --no-prune means "absence from this fetch is not
            # trusted", so it must not resolve records either.
            for external_id in open_failures.keys() - seen:
                self._observe(result, f"{adapter.source}:{external_id}",
                             self._kb.log_ingest_resolved, adapter.source, external_id)
        if prune:
            if dry_run:
                existing = self._kb.external_ids_for_source(adapter.source)
                result.deleted = len(existing - seen)
            else:
                result.deleted = self._kb.prune(adapter.source, seen)
                # mkdir -p creates folders to hold notes; reclaim the ones a prune just
                # emptied, so deleting a vault directory's notes also drops the directory.
                self._kb.prune_empty_folders()
                if result.deleted:
                    # A prune severs the deleted notes' entity links (FK cascade); collect any
                    # instance entity thereby left with zero support, so a mirror-prune doesn't
                    # leak orphaned entity nodes (the scheduled sweep would also catch these).
                    self._kb.reconcile_instances()
        if not dry_run:
            pipeline.run_post(ctx, seen)        # FlagEnrichment + RecomputeImportance
            self._record_sync(adapter.source)
        return result

    def _record_sync(self, source: str) -> None:
        with self._pool.connection() as conn:
            conn.execute(
                """INSERT INTO sync_state (source, last_synced_at) VALUES (%s, now())
                   ON CONFLICT (source) DO UPDATE SET last_synced_at = now()""",
                (source,),
            )
