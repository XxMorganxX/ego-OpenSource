"""`kb-mcp-ingestd`: one always-on process — push receiver + per-source scheduler.

build_jobs is the single place every source becomes a standard scheduled task: a pull
source -> SyncService.sync, a push source -> InboxDrainService.drain, plus the enrichment
worker — each a ScheduledJob with its own period, carrying its own transform pipeline.
"""

import os
import threading
from collections.abc import Callable
from datetime import datetime, timedelta

import uvicorn
from fastapi import FastAPI
from psycopg_pool import ConnectionPool

from kb_core.db import migrate_pool, open_pool
from kb_core.ingest.config import SourceConfig, load_sources
from kb_core.ingest.drain import InboxDrainService
from kb_core.ingest.enrichment import Enricher, EnrichmentWorker
from kb_core.ingest.inbox import Inbox
from kb_core.ingest.embedding import build_embedding_enricher
from kb_core.inference_routing import InferenceRouter
from kb_core.ingest.inference import LOCAL_INFERENCE_URL, HTTPInferenceClient
from kb_core.ingest.llm import LLMEnricher, clients_for_passes, load_passes, passes_for_phase
from kb_core.ingest.promotion import EntityPromotionWorker
from kb_core.ingest.receiver import build_receiver, require_secret
from kb_core.ingest.scheduler import IngestScheduler, JobStatus, ScheduledJob, run_lanes
from kb_core.knowledge_base import DEFAULT_MAX_ACTIVE_CLASSES, KnowledgeBase
from kb_core.policy import Policy
from kb_core.registry import default_registry
from kb_core.tenancy import daemon_policy
from kb_core.sync import SyncService


# KB_INGEST_RECEIVER values (trimmed, case-insensitive) that turn the push receiver off.
# kb-svc.sh's ingest block accepts exactly this set (a test pins the parity).
RECEIVER_OFF_VALUES = ("0", "false", "no", "off")


def receiver_enabled(env) -> bool:
    """Whether this daemon serves the push receiver. Tenant daemons run with
    KB_INGEST_RECEIVER=0 (kb-svc.sh tenant mode): their notes arrive through the gateway."""
    return (env.get("KB_INGEST_RECEIVER", "1") or "").strip().lower() not in RECEIVER_OFF_VALUES


def enrich_gate_providers(env, policy) -> "tuple[str, ...]":
    """The remote providers an enrich-phase chain rides: a pause on ANY of them idles the
    lane (all-or-nothing per tick, as before — every node's chain touches every pass).
    Local is never gated (it has no quota). Falls back to claude-cli when no remote pass
    is configured, so an empty enrichment.toml keeps today's behaviour."""
    from kb_core.inference_routing import load_routing
    routes = load_routing(env)
    providers = {routes[p.name].provider
                 for p in passes_for_phase(load_passes(env, policy), "enrich")}
    providers.discard("local")
    return tuple(sorted(providers)) or ("claude-cli",)


def build_jobs(kb: KnowledgeBase, pool: ConnectionPool, sources: "list[SourceConfig]", *,
               enrichers: "list[Enricher] | None" = None,
               kick_enrich: "Callable[[], None] | None" = None,
               log: "Callable[[str], None]" = print,
               gate_providers: "tuple[str, ...]" = ("claude-cli",)) -> "list[ScheduledJob]":
    sync_svc = SyncService(kb, pool)
    drain_svc = InboxDrainService(kb, pool)
    jobs: list[ScheduledJob] = []
    # "local" joins the gate: it has no quota, but an UNREACHABLE KB_Inference raises
    # ProviderUnavailable(provider="local") and pauses it briefly. Without gating on that
    # pause the lane would re-claim a batch into the dead server on every tick.
    gate_providers = tuple(dict.fromkeys((*gate_providers, "local")))

    # Ingestion -> enrichment is event-driven: a sync of an enrichable source that left
    # nodes 'pending' pokes the enrich lane (kick_enrich), so new notes are claimed at
    # once instead of sitting out KB_ENRICH_PERIOD with nothing processing.
    def _kick_if_pending(cfg: SourceConfig) -> None:
        if kick_enrich is not None and cfg.is_enrichable and kb.has_pending_enrichment():
            kick_enrich()

    for cfg in sources:
        if not cfg.enabled:
            continue
        if cfg.mode == "pull":
            def run_pull(c=cfg):
                res = sync_svc.sync(c.adapter, prune=c.prune, pipeline=c.pipeline)
                # seen = records that reached the store (the whole corpus, every tick);
                # changed = node rows actually written. An idle tick is changed=0.
                log(f"[ingestd] pull {res.source}: seen={res.upserted} "
                    f"changed={res.changed} deleted={res.deleted} errors={len(res.errors)}"
                    + (f" | PRUNE SKIPPED: {res.prune_skipped}"
                       if res.prune_skipped else ""))
                for err in res.errors:
                    log(f"[ingestd]   {res.source} ERROR: {err}")
                _kick_if_pending(c)
                return res
            jobs.append(ScheduledJob(cfg.source, cfg.period, run_pull))
        else:
            def run_drain(c=cfg):
                res = drain_svc.drain(c.source, c.decoder, pipeline=c.pipeline)
                log(f"[ingestd] drain {res.source}: seen={res.upserted} "
                    f"changed={res.changed} deleted={res.deleted} errors={len(res.errors)}")
                _kick_if_pending(c)
                return res
            jobs.append(ScheduledJob(cfg.source, cfg.period, run_drain))
    enrich_period = timedelta(seconds=int(os.environ.get("KB_ENRICH_PERIOD", "120")))
    enrich_batch = int(os.environ.get("KB_ENRICH_BATCH", "10"))
    # Opt-in across-node parallelism. Default 1 = the serial loop this has always run.
    # Worth raising only when the heavy passes route to a provider that can serve calls
    # concurrently (claude-cli spawns one subprocess per call); a pass routed `local` hits
    # the single-flight inference server and re-serialises the batch there regardless.
    enrich_concurrency = max(1, int(os.environ.get("KB_ENRICH_CONCURRENCY", "1")))
    def run_enrich():
        # Outage gate: while a provider the enrichment chains ride is paused
        # (ProviderUnavailable → defer_enrichment → provider_pause), claiming a batch
        # would only burn it through a dead provider. All-or-nothing per tick is
        # deliberate — every node's chain touches every routed provider. The owner's
        # chains ride claude-cli, a tenant's ride `agent` (enrich_gate_providers).
        for prov in gate_providers:
            paused = kb.provider_paused(prov)
            if paused is not None:
                log(f"[ingestd] enrichment paused: {prov} until {paused:%Y-%m-%d %H:%M%z}")
                return 0
        n = EnrichmentWorker(kb, enrichers or [], concurrency=enrich_concurrency,
                             gate_providers=gate_providers).run(limit=enrich_batch)
        log(f"[ingestd] enrichment: processed {n}")
        return n
    # Own lane + `more`: with a backlog, batches run back-to-back the moment the previous
    # one finishes (KB_ENRICH_PERIOD is only the idle poll), and a promotion sweep on the
    # slow lane can never hold up the next claim. `more` asks the table, not the claim
    # size: a full claim was the old proxy, and it stranded notes that arrived during a
    # partial batch's minutes-long run for the whole period — the remaining window of
    # "plenty pending, 0 processing". The pause check in `more` matters as much as the
    # one in run_enrich: deferred nodes sit at 'pending', so has_pending_enrichment
    # alone would re-tick the lane back-to-back for the whole outage.
    jobs.append(ScheduledJob(
        "enrichment", enrich_period, run_enrich, lane="enrich",
        more=lambda n: all(kb.provider_paused(p) is None for p in gate_providers)
                       and kb.has_pending_enrichment()))

    # Entity promotion: a cross-memory sweep that materializes recurring candidates into
    # sub-entities (threshold-create, then link new mentions). Cheaper than enrichment, so
    # it runs on a longer period by default.
    promote_threshold = int(os.environ.get("KB_PROMOTE_THRESHOLD", "2"))
    class_floor = int(os.environ.get("KB_CLASS_THRESHOLD", "3"))     # min votes to be considered
    max_classes = int(os.environ.get("KB_MAX_ACTIVE_CLASSES", str(DEFAULT_MAX_ACTIVE_CLASSES)))
    dominance_hi = float(os.environ.get("KB_CLASS_DOMINANCE_HI", "0.6"))
    dominance_lo = float(os.environ.get("KB_CLASS_DOMINANCE_LO", "0.4"))
    dominance_span = int(os.environ.get("KB_CLASS_DOMINANCE_SPAN", "40"))
    dominance_margin = float(os.environ.get("KB_CLASS_DOMINANCE_MARGIN", "0.1"))
    promote_period = timedelta(seconds=int(os.environ.get("KB_PROMOTE_PERIOD", "300")))
    gate_on = (os.environ.get("KB_INSTANCE_MEANING_GATE", "on").strip().lower() != "off")
    promo_client = InferenceRouter(pool=pool).client_for("meaning_gate") if gate_on else None
    if promo_client is not None:
        # Resolved ONCE here, at daemon start: EntityPromotionWorker re-resolves this
        # same prompt every tick inside run_promotion, but a tick's PolicyError is
        # swallowed by the scheduler (one bad job must never kill the loop) — so a
        # broken overlay would otherwise fail silently, tick after tick, instead of
        # failing loud at startup.
        from kb_core.meaning_judge import SYSTEM_FILE as MEANING_SYSTEM_FILE
        kb.prompt(MEANING_SYSTEM_FILE)
    def run_promotion():
        n = EntityPromotionWorker(
            kb, threshold=promote_threshold, inference_client=promo_client,
            class_floor=class_floor, max_classes=max_classes, dominance_hi=dominance_hi,
            dominance_lo=dominance_lo, dominance_span=dominance_span,
            dominance_margin=dominance_margin).run()
        log(f"[ingestd] promotion: settled {n}")
        return n
    jobs.append(ScheduledJob("promotion", promote_period, run_promotion, lane="slow"))
    return jobs


def build_app(inbox: Inbox, *, secret: str) -> FastAPI:
    """The push receiver. It serves nothing unauthenticated but /healthz: the ingestion
    log it used to expose at an open GET /log is read through the dashboard API
    (/api/ingest/log), behind the dashboard's auth."""
    return build_receiver(inbox, secret=secret)


def _heartbeat(lane: str, now: datetime, ran: "list[str]",
               statuses: "list[JobStatus]") -> None:
    """Print one tick line: which lane ticked, which of its jobs ran, and when each of
    that lane's jobs is next due. Lanes tick on their own threads, so the label is what
    keeps interleaved lines readable."""
    scan = ", ".join(f"{s.name}: due in {max(0, round(s.due_in_seconds))}s "
                     f"(every {round(s.period_seconds)}s)" for s in statuses)
    ran_note = f"RAN {', '.join(ran)} | " if ran else ""
    print(f"[ingestd] tick[{lane}] {now:%H:%M:%S}Z | {ran_note}{scan}", flush=True)


def _build_enrichers(env: dict, *, phase: str = "enrich",
                     policy: "Policy | None" = None,
                     pool: "ConnectionPool | None" = None) -> "list[Enricher]":
    """The enrichment pipeline for one phase: LLM passes (if configured) then the embedding
    enricher (unless KB_EMBED_ENABLED is off). Returns [] only when there are no LLM passes
    AND embedding is disabled.

    Only passes tagged for `phase` ('enrich' = first-time, 'recrawl' = re-analysis) run.
    Every pass resolves through inference.toml (kb_core.inference_routing); embedding
    remains local via KB_Inference /embed (the embed task is pinned local). `pool` reaches
    the router for `agent`-routed passes (tenant daemons), which enqueue llm_jobs rows.
    """
    enrichers: "list[Enricher]" = []
    passes = passes_for_phase(load_passes(env, policy), phase)
    if passes:
        router = InferenceRouter(env, pool=pool)
        clients_by_pass = clients_for_passes(router, passes)
        print(f"[ingestd] inference routing ({phase}): {router.describe()}", flush=True)
        # Preflight each distinct local client once (remote tiers have no /health).
        url = env.get("KB_INFERENCE_URL", LOCAL_INFERENCE_URL)
        seen: "set[int]" = set()
        for c in clients_by_pass.values():
            if isinstance(c, HTTPInferenceClient) and id(c) not in seen:
                seen.add(id(c))
                _preflight_inference(c, url)
        max_classes = int(env["KB_MAX_ACTIVE_CLASSES"]) if "KB_MAX_ACTIVE_CLASSES" in env else None
        enrichers.append(LLMEnricher(None, passes, max_classes=max_classes,
                                     clients_by_pass=clients_by_pass))
    embedder = build_embedding_enricher(env)
    if embedder is not None:
        print(f"[ingestd] embedding enrichment ({phase}): enabled", flush=True)
        enrichers.append(embedder)
    return enrichers


def _preflight_inference(client: HTTPInferenceClient, url: str) -> None:
    """Probe KB_Inference at boot so a down/misconfigured engine is visible immediately.

    Warns but does not crash — the rest of the daemon (pull/push ingestion) runs regardless;
    only enrichment depends on the LLM.
    """
    try:
        h = client.health()
    except Exception as exc:
        print(f"[ingestd] WARNING: KB_Inference unreachable at {url}: {exc} — "
              "enrichment will fail until it is up", flush=True)
        return
    if h.get("ollama") == "up" and h.get("model_available"):
        print(f"[ingestd] KB_Inference OK: model {h.get('model')} available", flush=True)
    else:
        print(f"[ingestd] WARNING: KB_Inference not ready ({h}) — "
              "enrichment will fail until the model is available", flush=True)


def _last_run(pool: ConnectionPool):
    def lookup(name: str) -> "datetime | None":
        with pool.connection() as conn:
            row = conn.execute(
                "SELECT last_synced_at FROM sync_state WHERE source = %s", (name,)
            ).fetchone()
        return row["last_synced_at"] if row else None
    return lookup


def main() -> None:
    # The pool is the real ceiling on enrichment concurrency, not KB_ENRICH_CONCURRENCY:
    # open_pool defaults to max_size=4 and `pool.connection()` BLOCKS ~30s before raising
    # PoolTimeout, so N enrichment threads against a 4-connection pool would stall rather
    # than fail loudly. Size it to the workers plus headroom for everything else that
    # shares this pool concurrently: the enrich-lane runner (claims), the slow-lane
    # runner (promotion sweeps), the fast-lane runner (pulls/drains), and the push
    # receiver. Default 1 keeps the historical minimum of 4 plus the extra lane.
    _conc = max(1, int(os.environ.get("KB_ENRICH_CONCURRENCY", "1")))
    # Refuse a missing, short or placeholder HMAC key before anything starts, not after
    # the scheduler lanes are already running.
    secret = (require_secret(os.environ.get("KB_INGEST_SECRET"))
              if receiver_enabled(os.environ) else None)
    policy = daemon_policy(os.environ)   # tenant mode: the registry's, never ./policy.toml
    pool = open_pool(os.environ["KB_DATABASE_URL"], max_size=max(4, _conc + 5))
    migrate_pool(pool)
    kb = KnowledgeBase(pool, default_registry(), policy=policy)
    # Recover claims stranded by a mid-batch shutdown. Safe exactly here: this daemon is
    # the only claimer and its scheduler hasn't started, so no batch can be in flight.
    stuck = kb.requeue_stuck_processing()
    if stuck:
        print(f"[ingestd] requeued {stuck} node(s) stranded at 'processing'", flush=True)
    # Late-bound: `lanes` fills in right after run_lanes below. A pull finishing in the
    # gap misses one kick, which the enrichment job's boot seed (due immediately) covers.
    lanes: "dict[str, IngestScheduler]" = {}
    def kick_enrich() -> None:
        sched = lanes.get("enrich")
        if sched is not None:
            sched.poke("enrichment")
    jobs = build_jobs(kb, pool, load_sources(),
                      enrichers=_build_enrichers(os.environ, phase="enrich", policy=kb.policy,
                                                 pool=pool),
                      kick_enrich=kick_enrich,
                      gate_providers=enrich_gate_providers(os.environ, kb.policy))
    tick_seconds = float(os.environ.get("KB_TICK_SECONDS", "20"))
    # Three lanes, three threads: ingestion (pull/drain) ticks on the fast lane while an
    # enrichment batch occupies the enrich lane and a promotion sweep occupies the slow
    # lane, each for minutes — none can delay another's next run. See run_lanes.
    lanes.update(run_lanes(
        jobs, heartbeat=tick_seconds, last_run=_last_run(pool), on_tick=_heartbeat,
        on_error=lambda n, e: print(f"[ingestd] job {n} failed: {e}", flush=True)))
    print(f"[ingestd] scheduler up — {len(jobs)} jobs across lanes "
          f"{sorted(lanes)}, heartbeat every {tick_seconds:g}s", flush=True)
    if not receiver_enabled(os.environ):
        print("[ingestd] push receiver disabled (KB_INGEST_RECEIVER=0); scheduler lanes only",
              flush=True)
        threading.Event().wait()         # lanes run on their own threads; park the main thread
        return
    app = build_app(Inbox(pool), secret=secret)
    uvicorn.run(app, host=os.environ.get("KB_INGEST_HOST", "127.0.0.1"),
                port=int(os.environ.get("KB_INGEST_PORT", "8030")),
                proxy_headers=False)     # never take the client address from X-Forwarded-For


if __name__ == "__main__":
    main()
