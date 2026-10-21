"""Prove HybridStrategy == KnowledgeBase.search() across your golden set.

The synthetic test runs in CI; this runs against your own corpus, which is where
collation, ties, and ANN behaviour actually bite. Method precedent: migration 0037.

    KB_DATABASE_URL=... uv run python scripts/prove_equivalence.py

Exits 0 only when every query's top-20 SET matches. Prints the first divergence in
full so it can be debugged without a second run.

Runs against a REAL corpus, so this script must leave it exactly as found.
Both kb.search() and strategy.search() call record_access on the rows they return,
which bumps access_count/last_accessed_at -- and that write measurably perturbs the
NEXT HNSW scan (10/10 unstable -> 5/5 stable once neutralized). The two calls per
item are therefore snapshotted and restored between them and again after, mirroring
tests/test_equivalence.py's _compare -- except the restore here is scoped to the ids
each call actually touched (at most `limit` per call), not every row in the table:
an UPDATE for the whole corpus on every restore is write amplification the review
flagged in the test helper. Precedent for the snapshot/restore shape (there scoped
to the whole run rather than per-item, since the harness makes one call per item,
not a legacy/strategy pair): evals/retrieval/run.py's run().
"""

import json
import os
import sys
from pathlib import Path

from kb_core.db import open_pool
from kb_core.embedding import instruct_query
from kb_core.ingest.inference import LOCAL_INFERENCE_URL, HTTPInferenceClient
from kb_core.knowledge_base import KnowledgeBase
from kb_core.registry import default_registry

from kb_retrieval.contract import Query
from kb_retrieval.registry import get_strategy


def _eval_data(rel: str, default: Path) -> Path:
    root = os.environ.get("KB_EVAL_DATA_DIR")
    return Path(root) / rel if root else default


GOLDEN = _eval_data("retrieval/golden.jsonl",
                     Path(__file__).resolve().parents[2] / "evals" / "retrieval" / "golden.jsonl")


def _snapshot_access(pool):
    """Full read of access-state, indexed by id, taken fresh per item (mirroring
    tests/test_equivalence.py's _compare, which snapshots once per comparison) so
    the window in which a concurrent write from the live daemons (ingest,
    reanalyze, entity-resolve, ...) could get clobbered by our own restore is one
    item wide, not the whole golden run. A SELECT only -- no write amplification --
    unlike _restore_access below, which only ever UPDATEs the ids record_access
    actually touched."""
    with pool.connection() as conn:
        rows = conn.execute(
            "SELECT id, access_count, last_accessed_at FROM nodes").fetchall()
    return {r["id"]: (r["access_count"], r["last_accessed_at"]) for r in rows}


def _restore_access(pool, snapshot, ids):
    """Restore ONLY `ids` to their snapshotted values. record_access touches at most
    `limit` rows per call, so restoring the whole table on every call (as
    tests/test_equivalence.py's _restore_access does) is write amplification -- an
    UPDATE for every row in the corpus when only ~20 changed."""
    rows = [(snapshot[i][0], snapshot[i][1], i) for i in ids if i in snapshot]
    if not rows:
        return
    with pool.connection() as conn:
        conn.cursor().executemany(
            "UPDATE nodes SET access_count = %s, last_accessed_at = %s WHERE id = %s",
            rows)


def main() -> int:
    pool = open_pool(os.environ["KB_DATABASE_URL"])
    kb = KnowledgeBase(pool, default_registry())
    strategy = get_strategy("hybrid", kb=kb)
    # Default: where scripts/kb-svc.sh runs KB_Inference (:8001; :8000 is the
    # dashboard). Pass KB_INFERENCE_URL to point anywhere else.
    client = HTTPInferenceClient(
        os.environ.get("KB_INFERENCE_URL", LOCAL_INFERENCE_URL), timeout=30)

    items = [json.loads(ln) for ln in GOLDEN.read_text().splitlines() if ln.strip()]
    bad = 0
    for item in items:
        text = item["request"]
        vec = client.embed([instruct_query(text)[:6000]])[0]
        snapshot = _snapshot_access(pool)
        legacy = [r.node.id for r in kb.search(text, embedding=vec, limit=20)]
        _restore_access(pool, snapshot, legacy)
        got = [r.id for r in strategy.search(Query(text=text, embedding=vec, limit=20)).rows]
        _restore_access(pool, snapshot, got)
        if not legacy and not got:
            # A degenerate environment (e.g. the embedder returning an all-zero
            # vector) would make set(legacy) == set(got) == set() and report a
            # trivial match. That is not equivalence -- it is both sides failing
            # to find anything. Fail loudly instead of counting it as a pass.
            print(f"EMPTY RESULT on both sides for {item['id']}: {text!r} -- "
                  f"treating as a failure, not a trivial match", file=sys.stderr)
            bad += 1
            continue
        if set(legacy) != set(got):
            bad += 1
            if bad == 1:
                print(f"DIVERGENCE on {item['id']}: {text!r}", file=sys.stderr)
                print(f"  only legacy: {set(legacy) - set(got)}", file=sys.stderr)
                print(f"  only new:    {set(got) - set(legacy)}", file=sys.stderr)
        elif legacy != got:
            print(f"order-only difference on {item['id']}", file=sys.stderr)
    print(f"{len(items) - bad}/{len(items)} queries match on top-20 set")
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
