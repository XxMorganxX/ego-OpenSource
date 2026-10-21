"""`kb-mcp-consolidated`: rebuilds the derived concept layer (the KB's "sleep" — global
consolidation the per-memory enrichers can't do) when its inputs actually changed: polls a
cheap churn probe (KB_CONSOLIDATE_POLL), fires once writes are quiet (KB_CONSOLIDATE_QUIET)
and the enrichment backlog has drained, with KB_CONSOLIDATE_INTERVAL as a max-staleness
backstop; a failing pass backs off exponentially (2x poll, doubling, capped at 3600s)
instead of retrying every poll. Mirrors entity_resolve_daemon.py: own process, shares
KnowledgeBase. Concept coherence and labels use pinned Claude Sonnet 5; concept embeddings
stay on local Qwen.

It also serves MANUAL rebuilds: the dashboard queues a concept_run_requests row and NOTIFYs
`kb_consolidate`, which wakes this loop in about a second. A claimed request forces a pass
the churn gate would have held — that is the point of the button. Consolidation stays
single-writer (this process), so a dashboard rebuild can never interleave with a churn pass.
"""
import contextlib
import os
import time
from datetime import datetime, timezone

import psycopg

from kb_core.concept_judge import SYSTEM_FILE as CONCEPT_SYSTEM_FILE
from kb_core.consolidate import build_consolidation_clients, run_once, should_consolidate
from kb_core.db import migrate_pool, open_pool
from kb_core.ingest.inference import ProviderUnavailable
from kb_core.knowledge_base import ConsolidationBusy, KnowledgeBase
from kb_core.registry import default_registry
from kb_core.tenancy import daemon_policy

_WAKE_CHANNEL = "kb_consolidate"


def preflight_prompts(kb: KnowledgeBase) -> None:
    """Resolve the concept-labeler's system prompt once at daemon start, so a broken
    overlay (missing file, bad/cyclic include) raises PolicyError immediately instead of
    waiting for the churn gate to fire — which could be hours or days after a bad deploy,
    and by then the failure would only surface mid-pass (see kb_core.consolidate)."""
    kb.prompt(CONCEPT_SYSTEM_FILE)


def _open_listen(dsn: str):
    """A dedicated autocommit connection LISTENing for manual rebuild requests. Separate
    from the pool because it must stay parked on one session; reopened by the caller after
    any drop, so a click is never lost for longer than one poll."""
    conn = psycopg.connect(dsn, autocommit=True)
    conn.execute(f"LISTEN {_WAKE_CHANNEL}")
    return conn


def _wait(listen, delay: float) -> None:
    """Idle up to `delay`, waking early on the first NOTIFY. Payloads are ignored — the
    request table is the queue, the notification is only a doorbell. Notifications that
    arrive mid-pass are buffered by Postgres for this session and delivered here, so a
    click during a run is served by the NEXT pass rather than lost."""
    for _note in listen.notifies(timeout=delay):
        break


def _pause_delay(until, poll: float) -> float:
    """Seconds to idle for a provider pause ending at `until` (None = unknown): at least
    one poll, at most an hour, so a manual click is still re-tried within the hour."""
    left = (until - datetime.now(timezone.utc)).total_seconds() if until else poll
    return max(poll, min(left, 3600.0))


def main() -> None:
    env = os.environ
    policy = daemon_policy(env)          # tenant mode: the registry's, never ./policy.toml
    pool = open_pool(env["KB_DATABASE_URL"])
    migrate_pool(pool)
    kb = KnowledgeBase(pool, default_registry(), policy=policy)
    preflight_prompts(kb)
    label_client, embedding_client = build_consolidation_clients(env, pool=pool)
    # The labeler's remote provider, if any. Its provider_pause row is shared with every
    # daemon on this database: a pause the ingest lane recorded holds consolidation too.
    label_provider = getattr(label_client, "provider", None)
    if label_provider == "local":
        label_provider = None
    dsn = env["KB_DATABASE_URL"]

    knn_k = int(env.get("KB_CONSOLIDATE_KNN_K", "8"))
    min_sim = float(env.get("KB_CONSOLIDATE_MIN_SIM", "0.55"))
    resolution = float(env.get("KB_CONSOLIDATE_RESOLUTION", "1.0"))
    min_size = int(env.get("KB_CONSOLIDATE_MIN_SIZE", "3"))
    entity_share = float(env.get("KB_CONSOLIDATE_ENTITY_SHARE", "0.25"))
    hub_share_raw = float(env.get("KB_CONSOLIDATE_HUB_SHARE", "0.10"))
    hub_share = hub_share_raw if hub_share_raw > 0 else None
    lift_raw = float(env.get("KB_CONSOLIDATE_MIN_COHERENCE_LIFT", "0"))
    min_lift = lift_raw if lift_raw > 0 else None
    entity_alpha = float(env.get("KB_CONSOLIDATE_ENTITY_ALPHA", "1.0"))
    poll = float(env.get("KB_CONSOLIDATE_POLL", "60"))
    quiet = float(env.get("KB_CONSOLIDATE_QUIET", "180"))
    period = float(env.get("KB_CONSOLIDATE_INTERVAL", "86400"))  # max-staleness backstop

    # Single writer: any row still 'running' at startup belongs to a process that died.
    # Skipped when a CLI pass holds the advisory lock (see sweep_orphaned_concept_runs).
    swept = kb.sweep_orphaned_concept_runs()
    if swept:
        print(f"[consolidated] swept {swept} orphaned running run(s)", flush=True)

    print(f"[consolidated] up — knn_k={knn_k} min_sim={min_sim} "
          f"resolution={resolution} min_size={min_size} "
          f"entity_share={entity_share} hub_share={hub_share} "
          f"min_coherence_lift={min_lift} entity_alpha={entity_alpha} "
          f"poll={poll:g}s quiet={quiet:g}s "
          f"backstop={period:g}s wake={_WAKE_CHANNEL}",
          flush=True)
    last_reason = None
    fail_streak = 0
    listen = None
    try:
        while True:
            delay = poll
            if listen is None or listen.closed:
                listen = _open_listen(dsn)
            claimed: "list[int]" = []
            try:
                # A manual request outranks the churn gate: the human already decided.
                claimed = kb.claim_consolidation_requests()
                if claimed:
                    fire, reason = True, "manual"
                else:
                    signal = kb.consolidation_signal()
                    fire, reason = should_consolidate(signal, quiet_s=quiet,
                                                      backstop_s=period)
                paused = (kb.provider_paused(label_provider)
                          if fire and label_provider is not None else None)
                if paused is not None:
                    # Labelling would only hit a dead provider. Hold, keep any click.
                    kb.release_consolidation_requests(claimed)
                    claimed = []
                    fire, reason = False, f"{label_provider} paused"
                    delay = _pause_delay(paused, poll)
                if reason != last_reason:
                    print(f"[consolidated] state: {reason}"
                          f"{'' if fire else ' — holding'}", flush=True)
                    last_reason = reason
                if fire:
                    r = run_once(kb, label_client, embedding_client=embedding_client,
                                 knn_k=knn_k, knn_min_sim=min_sim, resolution=resolution,
                                 min_memories=min_size, entity_share=entity_share,
                                 hub_share=hub_share,
                                 min_coherence_lift=min_lift, entity_alpha=entity_alpha,
                                 trigger_reason=reason)
                    if claimed:
                        kb.finish_consolidation_requests(claimed, r.get("run_id"), None)
                        claimed = []
                    hub_bit = f"hub_dropped={r.get('hub_dropped', 0)}"
                    if min_lift is not None:
                        hub_bit += f" gate=coherence>={min_lift}"
                    elif hub_share is not None:
                        hub_bit += f" gate=hub_share:{hub_share}"
                    print(f"[consolidated] run {r.get('run_id')}: "
                          f"communities={r['communities']} kept={r['kept']} "
                          f"created={r['created']} updated={r['updated']} "
                          f"retired={r['retired']} discarded={r['discarded']} "
                          f"{hub_bit} "
                          f"label_failures={r.get('label_failures', 0)} "
                          f"embed_failures={r.get('embed_failures', 0)}", flush=True)
                    if r.get("label_failures"):
                        print(f"[consolidated] WARNING: {r['label_failures']} concept-label "
                              f"call(s) failed — those names fell back to placeholders. "
                              f"first error: {r.get('label_error')}", flush=True)
                    if r.get("embed_failures"):
                        print(f"[consolidated] WARNING: {r['embed_failures']} concept "
                              f"embedding(s) failed — those concepts are unreachable by the "
                              f"label-cosine retrieval arm until the next run. "
                              f"first error: {r.get('embed_error')}", flush=True)
                    last_reason = None       # next hold state logs afresh after a run
                fail_streak = 0
            except ConsolidationBusy as exc:
                # A `kb-consolidate --apply` pass holds the lock. Put the claim back so
                # the click survives to the next poll instead of dying on someone
                # else's run; this is not a failure, so no backoff.
                kb.release_consolidation_requests(claimed)
                print(f"[consolidated] deferring: {exc}", flush=True)
                last_reason = None
            except ProviderUnavailable as exc:
                # The labeler's provider is down (tenant agent offline, claude-cli out of
                # quota): not a failure of the pass. run_once already paused the provider
                # and wrote no concepts; put the click back (like ConsolidationBusy) and
                # wait for the provider's own pause window.
                kb.release_consolidation_requests(claimed)
                claimed = []
                delay = _pause_delay(exc.until, poll)
                print(f"[consolidated] {exc.provider} unavailable: {exc}; "
                      f"retrying in {delay:g}s", flush=True)
                last_reason = None
            except Exception as exc:               # never let one bad pass kill the loop
                if claimed:
                    kb.finish_consolidation_requests(claimed, None, str(exc))
                fail_streak = min(fail_streak + 1, 6)
                delay = min(poll * (2 ** fail_streak), 3600.0)
                print(f"[consolidated] pass failed: {exc} — retrying in {delay:g}s",
                      flush=True)
                last_reason = None       # next poll re-logs its state after recovery
            try:
                _wait(listen, delay)
            except psycopg.OperationalError as exc:
                print(f"[consolidated] wake channel dropped: {exc}; reconnecting",
                      flush=True)
                with contextlib.suppress(Exception):
                    listen.close()
                listen = None
                time.sleep(1.0)
    except KeyboardInterrupt:
        print("[consolidated] shutting down", flush=True)
    finally:
        if listen is not None:
            with contextlib.suppress(Exception):
                listen.close()


if __name__ == "__main__":
    main()
