"""`kb-mcp-entity-resolved`: periodically re-embeds entities and surfaces near-duplicate merge
suggestions. Mirrors the reanalysis daemon: own process, own schedule, shares KnowledgeBase."""
import os
import time

from kb_core.db import migrate_pool, open_pool
from kb_core.entity_resolve import run_once
from kb_core.inference_routing import InferenceRouter
from kb_core.ingest.inference import ProviderUnavailable
from kb_core.knowledge_base import KnowledgeBase
from kb_core.registry import default_registry
from kb_core.tenancy import daemon_policy


def main() -> None:
    env = os.environ
    policy = daemon_policy(env)          # tenant mode: the registry's, never ./policy.toml
    pool = open_pool(env["KB_DATABASE_URL"])
    migrate_pool(pool)
    kb = KnowledgeBase(pool, default_registry(), policy=policy)
    router = InferenceRouter(env, pool=pool)
    print(f"[entity-resolved] inference routing: {router.describe()}", flush=True)

    max_d = float(env.get("KB_ENTITY_MERGE_DISTANCE", "0.20"))
    top_k = int(env.get("KB_ENTITY_MERGE_TOPK", "5"))
    batch = int(env.get("KB_ENTITY_EMBED_BATCH", "32"))
    describe_batch = int(env.get("KB_ENTITY_DESCRIBE_BATCH", "16"))
    period = float(env.get("KB_ENTITY_RESOLVE_INTERVAL", "600"))
    # Provider pauses are gated per role inside run_once (describe and judge each skip
    # only while their own provider is paused); re-embed and the scan always run.
    describe_client = router.client_for("describe_entity")
    judge_client = router.client_for("merge_judge")
    embed_client = router.embedder()
    print(f"[entity-resolved] up — max_distance={max_d} top_k={top_k} "
          f"batch={batch} describe_batch={describe_batch} period={period:g}s", flush=True)
    try:
        while True:
            try:
                r = run_once(kb, describe_client=describe_client,
                             judge_client=judge_client,
                             embed_client=embed_client,
                             max_distance=max_d, top_k=top_k, batch=batch,
                             describe_batch=describe_batch)
                print(f"[entity-resolved] described={r['described']} "
                      f"reembedded={r['reembedded']} candidates={r['candidates']} "
                      f"staged={r['staged']} blocked={r['blocked']}"
                      + ("" if not r.get("paused") else " | paused: " + " ".join(
                          f"{role}={prov}" for role, prov in r["paused"].items())),
                      flush=True)
            except ProviderUnavailable as exc:
                # run_once handles describe/judge outages per role; this only catches one
                # from elsewhere in the pass. Not a failure: skip it, the next period retries.
                print(f"[entity-resolved] {exc.provider} unavailable until {exc.until}: "
                      "skipping this pass", flush=True)
            except Exception as exc:                       # never let one bad pass kill the loop
                print(f"[entity-resolved] pass failed: {exc}", flush=True)
            time.sleep(period)
    except KeyboardInterrupt:
        print("[entity-resolved] shutting down", flush=True)


if __name__ == "__main__":
    main()
