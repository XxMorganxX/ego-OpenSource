"""`kb-mcp-reanalyzed`: a standalone daemon that slowly revisits existing nodes and redraws
their connections via the local LLM. Decoupled from kb-mcp-ingestd — a separate process on
its own schedule, sharing only the KnowledgeBase and the LLM enricher logic.
"""

import contextlib
import os
import sys
import time
import traceback
from uuid import UUID

import psycopg

from kb_core.analysis.reanalysis import RECRAWL_PASS_FOR_REASON, ReanalysisWorker
from kb_core.db import migrate_pool, open_pool
from kb_core.ingest.config import load_sources
from kb_core.ingest.embedding import build_embedding_enricher
from kb_core.inference_routing import InferenceRouter
from kb_core.ingest.inference import LOCAL_INFERENCE_URL, HTTPInferenceClient
from kb_core.ingest.llm import LLMEnricher, clients_for_passes, load_passes, passes_for_phase
from kb_core.knowledge_base import KnowledgeBase
from kb_core.registry import default_registry
from kb_core.tenancy import daemon_policy

# The manual re-crawl wake channel: the dashboard emits NOTIFY here (payload = a node id)
# so a "Re-crawl" button press is serviced now instead of waiting for the next timer tick.
_WAKE_CHANNEL = "kb_reanalyze"


def _validate_recrawl_passes(passes: list) -> None:
    """Fail loud before a missing pass can have its dependency cursor stamped."""
    configured = {p.name for p in passes}
    missing = set(RECRAWL_PASS_FOR_REASON.values()) - configured
    if missing:
        raise RuntimeError(f"missing required recrawl pass(es): {', '.join(sorted(missing))}")


def reanalysis_sources(env: dict) -> "list[str]":
    """Which sources the crawl covers: the enrichable configured sources (sources.toml)
    plus agent-written memories. Non-enrichable sources are left alone (is_enrichable)."""
    enrichable = {c.source for c in load_sources(env) if c.is_enrichable}
    return sorted(enrichable | {"agent"})


def _recrawl_target(worker: ReanalysisWorker, payload: str) -> None:
    """Service one manual re-crawl wake: re-analyze the single node named in the NOTIFY
    payload. Tolerant by design — a malformed or non-crawlable target is logged and skipped,
    never allowed to crash the daemon loop."""
    nid = (payload or "").strip()
    if not nid:
        return
    try:
        done = worker.run_node(UUID(nid))
    except ValueError:
        print(f"[reanalyzed] ignoring malformed re-crawl request: {payload!r}", flush=True)
        return
    print(f"[reanalyzed] manual re-crawl {nid}: {done} node(s)", flush=True)


def main() -> None:
    env = os.environ
    policy = daemon_policy(env)          # tenant mode: the registry's, never ./policy.toml
    pool = open_pool(env["KB_DATABASE_URL"])
    migrate_pool(pool)
    kb = KnowledgeBase(pool, default_registry(), policy=policy)

    # Coordinated rollout entrypoint: with every long-running writer stopped, apply the
    # migrations above and adopt the legacy population before any service can move a
    # vocabulary. A failure exits non-zero and leaves the fleet stopped.
    if "--adopt-only" in sys.argv:
        adopted = kb.adopt_vocabulary_cursors(
            class_hash=kb.active_class_hash(), kind_hash=kb.doc_kind_vocab_hash())
        print(f"[reanalyzed] cursor adoption complete: {adopted} node(s)", flush=True)
        return

    passes = passes_for_phase(load_passes(env, kb.policy), "recrawl")
    if not passes:
        print("[reanalyzed] no 'recrawl' passes configured (enrichment.toml); nothing to do",
              flush=True)
        return
    _validate_recrawl_passes(passes)

    router = InferenceRouter(env, pool=pool)
    clients_by_pass = clients_for_passes(router, passes)
    print(f"[reanalyzed] inference routing: {router.describe()}", flush=True)
    url = env.get("KB_INFERENCE_URL", LOCAL_INFERENCE_URL)
    probed: "set[int]" = set()
    for c in clients_by_pass.values():
        if isinstance(c, HTTPInferenceClient) and id(c) not in probed:
            probed.add(id(c))
            try:
                h = c.health()
                print(f"[reanalyzed] KB_Inference {url}: ollama={h.get('ollama')} "
                      f"model_available={h.get('model_available')}", flush=True)
            except Exception as exc:
                print(f"[reanalyzed] WARNING: KB_Inference unreachable at {url}: {exc}",
                      flush=True)

    sources = reanalysis_sources(env)
    batch = int(env.get("KB_REANALYZE_BATCH", "10"))
    max_classes = int(env["KB_MAX_ACTIVE_CLASSES"]) if "KB_MAX_ACTIVE_CLASSES" in env else None
    enrichers = [LLMEnricher(None, passes, max_classes=max_classes,
                             clients_by_pass=clients_by_pass)]
    embedder = build_embedding_enricher(env)
    if embedder is not None:
        print("[reanalyzed] embedding enrichment (recrawl): enabled", flush=True)
        enrichers.append(embedder)
    # The recrawl lane's pause gate, same shape as the ingest enrich lane: the remote
    # providers its passes ride (local has no quota and is never gated).
    gate = tuple(sorted({getattr(c, "provider", None) for c in clients_by_pass.values()}
                        - {None, "local"}))
    worker = ReanalysisWorker(kb, enrichers, sources, batch=batch, gate_providers=gate)

    # One-time backfill: crawl every node that currently exists, once, then exit.
    if "--backfill" in sys.argv:
        with pool.connection() as conn:
            before = conn.execute("SELECT now() AS now").fetchone()["now"]
        print(f"[reanalyzed] backfill: analyzing all existing nodes in {sources} …", flush=True)
        total = worker.backfill(
            before, on_progress=lambda t: print(f"[reanalyzed] backfill: {t} analyzed…",
                                                flush=True))
        print(f"[reanalyzed] backfill complete: {total} node(s)", flush=True)
        return

    period = float(env.get("KB_REANALYZE_PERIOD", "600"))
    dsn = env["KB_DATABASE_URL"]
    print(f"[reanalyzed] up — sources={sources} batch={batch} "
          f"period={period:g}s passes={[p.name for p in passes]}", flush=True)

    listen = None
    cursors_adopted = False
    try:
        while True:
            if not cursors_adopted:
                try:
                    adopted = kb.adopt_vocabulary_cursors(
                        class_hash=kb.active_class_hash(),
                        kind_hash=kb.doc_kind_vocab_hash())
                except Exception as exc:
                    # Never enter the crawl with legacy NULL cursors: that would turn a
                    # transient hash/DB failure into the full corpus sweep adoption prevents.
                    print(f"[reanalyzed] cursor adoption failed; tick skipped: {exc}",
                          flush=True)
                    traceback.print_exc()
                    time.sleep(max(1.0, period))
                    continue
                cursors_adopted = True
                if adopted:
                    print(f"[reanalyzed] adopted current vocabulary for {adopted} node(s)",
                          flush=True)

            # Open a dedicated LISTEN connection on the first pass and after any drop, so a
            # manual wake is never silently lost for longer than one period.
            if listen is None or listen.closed:
                listen = psycopg.connect(dsn, autocommit=True)
                listen.execute(f"LISTEN {_WAKE_CHANNEL}")

            n = worker.run()
            print(f"[reanalyzed] re-analyzed {n} node(s), "
                  f"{getattr(worker, 'last_changed', 0)} changed", flush=True)

            # Idle up to `period`, but wake early to service any manual re-crawl requests
            # (NOTIFY kb_reanalyze, '<node id>') that arrive in the meantime.
            try:
                for note in listen.notifies(timeout=period):
                    _recrawl_target(worker, note.payload)
            except psycopg.OperationalError as exc:
                print(f"[reanalyzed] wake channel dropped: {exc}; reconnecting", flush=True)
                with contextlib.suppress(Exception):
                    listen.close()
                listen = None
                time.sleep(1.0)
    except KeyboardInterrupt:
        print("[reanalyzed] shutting down", flush=True)
    finally:
        if listen is not None:
            with contextlib.suppress(Exception):
                listen.close()


if __name__ == "__main__":
    main()
