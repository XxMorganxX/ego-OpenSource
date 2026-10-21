"""Shadow replica of production `search_memories` -- a WRITE-FREE counterfactual harness.

Why this exists: `run.py` drives the live MCP server, and every search it makes bumps
`nodes.access_count` / `last_accessed_at` (the engine's `_record_access`), which feeds the
FTS arm's importance multiplier -- so an eval perturbs the thing it measures. This module
runs the SAME ranking against a connection that cannot write, in two proven-equal paths:

  PROD    -- the production code itself: `KnowledgeBase.search` constructed on a pool whose
             every transaction is `default_transaction_read_only=on`, with the one write on
             the search path (`_record_access` / `record_access`) replaced by a no-op. Module
             constants (HYBRID_ARM_POOL, ANN_CHUNK_FANOUT, RRF_K, OR_FALLBACK_MAX_WORDS) can
             be monkeypatched for one call.
  REPLICA -- a parameterised restatement of search()'s hybrid SQL that IMPORTS the engine's
             SEARCH_FILTERS / _IMP_FACTOR / SEARCH_SCAFFOLDING_TYPES (never copies them),
             runs each arm separately in one REPEATABLE READ transaction, fuses in Python
             with the server-side `ORDER BY path` tiebreak, and exposes knobs the production
             SQL has no switch for (KNOB_DEFAULTS). `shadow_fidelity.py` proves replica ==
             prod id-for-id before any counterfactual is trusted.

Entry point:
    shadow_search(query, overrides={}, **args) -> dict
      args       the MCP tool's arguments (under/source/type/tags/kind/since/until/
                 include_dead/limit), exactly as `search_memories` takes them
      overrides  {} -> PROD; a subset of PROD_CONSTANT_KNOBS -> PROD with those constants
                 patched; anything else -> REPLICA with KNOB_DEFAULTS keys. `query_prefix`
                 is honoured on both paths (it only changes the query vector); default is
                 the shipped instruction (QUERY_INSTRUCTION) -- pass BARE ("") to reproduce
                 production before the instruction shipped (a raw, uninstructed query vector).
      returns    {"ids": [...], "rows": [{id, rrf, fts_r, knn_r, ent_r, similarity, note_sim,
                 chunk_sim, score}], "fts": [...], "knn": [...], "ent": [...], "fts_q",
                 "or_fired", "latency_ms", "path", ...}

Configuration (environment, like run.py / validate.py):
    KB_DATABASE_URL   DSN; `options=-c default_transaction_read_only=on` is ALWAYS appended
                      and assert_readonly() must be called at startup by every CLI.
    KB_EVAL_DATA_DIR  where the real gold lives (`retrieval/golden.jsonl`,
                      `retrieval/traffic_gold.jsonl`). Required by the gold loaders: the
                      synthetic sets shipped in this directory do not match a live DB.
    KB_POLICY_CONFIG  defaults to KB_Core/policy.toml (relative to the repo) when that file
                      exists -- the same file the MCP process is started with.
    KB_INFERENCE_URL  the embedding service, same default as the production client. Its
                      /health names the live embedding model, which keys the query-vector
                      cache (an unreachable service is an error unless
                      KB_SHADOW_TRUST_CACHE_MODEL=1 says to trust the cache's model name).

Run with the engine's venv python (never `uv run`, which may rebuild a venv under a live
server): `../../KB_Core/.venv/bin/python shadow_bench.py ...`. CLI exit codes (run_cli):
0 = ran (and, for shadow_fidelity, replica proven == prod), 1 = shadow_fidelity mismatch,
2 = could not run (configuration, database, embedding service or embedding budget).

Everything in this file is import-safe without a database: connections are opened lazily by
`kb()` / `make_kb()`, so the pure helpers (BM25 math, RRF fusion, dedup, metrics, query
normalisation, as-of admissibility) are unit-testable (tests/test_shadow.py).
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import math
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover -- non-POSIX: cache writes stay atomic, just unlocked
    fcntl = None

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[1]
CACHE_DIR = HERE / ".cache" / "shadow"
RESULTS_DIR = HERE / "results" / "shadow"
QVEC_CACHE = CACHE_DIR / "qvec_cache.json"
DF_CACHE = CACHE_DIR / "df_cache.json"

sys.path.insert(0, str(REPO_ROOT / "KB_Core" / "src"))
_POLICY = REPO_ROOT / "KB_Core" / "policy.toml"
if _POLICY.exists():
    os.environ.setdefault("KB_POLICY_CONFIG", str(_POLICY))

# No DB/network at import time -- pure constants, safe beside the lazy kb_core
# imports elsewhere in this module.
from kb_core.embedding import QUERY_INSTRUCTION, QUERY_INSTRUCTION_VERSION  # noqa: E402

MAX_LIMIT = 200          # mirrors KB_MCP server.py MAX_LIMIT
DEFAULT_LIMIT = 20       # search_memories' default
EMBED_TEXT_CAP = 6000    # applies AFTER instruction formatting: server.py's
                         # make_query_embedders embeds prefix.format(q=text)[:6000]
                         # (instruct_query(text)[:6000]), same cap as the bare
                         # embedder's text[:6000]
# Corpus-flag thresholds used for the hub / stub shares reported by the bench. "Hub" =
# notes the access loop has already amplified; "short" = notes below the chunker's
# minimum, i.e. a whole-note vector with no chunk corroboration.
HUB_ACCESS_COUNT = 8
SHORT_NOTE_CHARS = 700

# Query-side instruction prefixes (the embedder is asymmetric; documents stay raw).
# `{q}` is the query. P1 is the shipped instruction (the model card's default wording,
# now production); P2/P3 are the wording-sensitivity alternates
# measured alongside it.
PREFIX_P1 = QUERY_INSTRUCTION   # the shipped instruction, imported -- never a copy
PREFIX_P2 = ("Instruct: Given a question about the user's personal notes, retrieve notes "
             "that answer it\nQuery: {q}")
PREFIX_P3 = ("Instruct: Retrieve documents from a personal knowledge base that are relevant "
             "to the query\nQuery: {q}")
BARE = ""   # query_prefix for a raw query vector (production before the instruction shipped)


# ============================================================================ pure helpers
def bm25_idf(df: int, n_docs: int) -> float:
    """BM25 inverse document frequency (Robertson/Sparck Jones, +1 smoothed)."""
    return math.log((n_docs - df + 0.5) / (df + 0.5) + 1.0)


def bm25_tf_weight(tf: int, doc_len: float, avg_len: float, k1: float = 1.2, b: float = 0.75) -> float:
    """Saturated, length-normalised term-frequency weight. `doc_len` / `avg_len` are in
    characters here (the SQL twin uses length(content)), not tokens."""
    return tf * (k1 + 1.0) / (tf + k1 * (1.0 - b + b * doc_len / avg_len))


def bm25_score(tf_by_lexeme: dict, idf_by_lexeme: dict, doc_len: float, avg_len: float,
               k1: float = 1.2, b: float = 0.75) -> float:
    """Reference implementation of the per-document score the replica's SQL computes
    (sum over the query lexemes present in the document)."""
    return sum(idf_by_lexeme[lex] * bm25_tf_weight(tf, doc_len, avg_len, k1, b)
               for lex, tf in tf_by_lexeme.items() if lex in idf_by_lexeme)


def rrf_fuse(ranked_lists: list, k: float) -> dict:
    """Reciprocal-rank fusion over `[(ids_in_rank_order, weight), ...]`. Absence from a
    list contributes 0 (never a penalty); a duplicate id inside one list keeps its best
    (first) position -- the semantics of the engine's FULL OUTER JOIN fusion and of
    kb_retrieval.fusion.rrf, plus an explicit per-list weight (1.0 == production)."""
    scores: dict = {}
    for ids, weight in ranked_lists:
        seen = set()
        for i, ident in enumerate(ids, start=1):
            if ident in seen:
                continue
            seen.add(ident)
            scores[ident] = scores.get(ident, 0.0) + weight / (k + i)
    return scores


def order_fused(scores: dict, path_rank: dict) -> list:
    """Production's final ordering: fused score DESC, then the server-side `ORDER BY path`
    position (ids missing from `path_rank` sort last)."""
    return sorted(scores, key=lambda i: (-scores[i], path_rank.get(i, 1 << 30)))


def collapse_near_duplicates(ordered: list, pairs: list, depth: int) -> tuple:
    """Near-duplicate collapse inside the top `depth` of a fused order. `pairs` is
    `[(id_a, id_b, cosine), ...]` for pairs at or above the collapse threshold; the
    better-ranked member of each pair stays, the other moves to the tail (after every
    non-collapsed id), so the page is refilled from the pool. A member already collapsed
    cannot itself absorb another id. Returns (new_order, [(kept, dropped, cosine), ...])."""
    head = ordered[:depth]
    if len(head) < 2 or not pairs:
        return list(ordered), []
    pos = {i: n for n, i in enumerate(head)}
    dropped, collapsed = set(), []
    for a, b, sim in sorted(pairs, key=lambda p: -p[2]):
        if a not in pos or b not in pos:
            continue
        first, later = sorted((a, b), key=lambda i: pos[i])
        if first in dropped or later in dropped:
            continue
        dropped.add(later)
        collapsed.append((first, later, round(float(sim), 4)))
    kept = [i for i in head if i not in dropped]
    return kept + list(ordered[depth:]) + [i for i in head if i in dropped], collapsed


_ACCENT_FROM = "èéêëàáâäìíîïòóôöùúûüçñÈÉÊËÀÁÂÄÌÍÎÏÒÓÔÖÙÚÛÜÇÑ"
_ACCENT_TO = "eeeeaaaaiiiioooouuuucnEEEEAAAAIIIIOOOOUUUUCN"
_ACCENT_TABLE = str.maketrans(_ACCENT_FROM, _ACCENT_TO)
_STOPWORDS = set("a an the of to in on for and or with what is are my me i how why which who "
                 "about at by from as into".split())


def split_terms(query: str) -> list:
    """Whitespace split that keeps a double-quoted phrase as ONE term (the OR-join must not
    break a phrase that `normalize_query` produced)."""
    return re.findall(r'"[^"]*"|\S+', query)


def or_join(terms: list) -> str:
    return " OR ".join(terms)


def fallback_terms(query: str, k: dict) -> list:
    """The terms the OR fallback counts (against the length cap) and OR-joins. With the
    production gate (`fts_mode="prod"`) and no query normalisation this is the engine's own
    `query.split()` -- the engine does NOT keep a quoted phrase whole, so neither may the
    replica, or a quoted query would fire the fallback on one side only. Every other mode
    (and `lexnorm` query/both, whose phrases this module introduced) keeps a quoted phrase
    as one term (`split_terms`)."""
    if k.get("fts_mode", "prod") == "prod" and k.get("lexnorm") not in ("query", "both"):
        return query.split()
    return split_terms(query)


def normalize_query(query: str, mode) -> str:
    """Lexical normalisation of the QUERY side (`lexnorm` in {"query", "both"}): accents are
    stripped and a token joined with `-` or `_` becomes a quoted phrase of its parts, so
    `kb-mcp` matches documents that spell it `KB_MCP` (both tokenise to adjacent lexemes).
    A leading `-` (websearch negation) is left alone."""
    if mode not in ("query", "both"):
        return query
    out = []
    for tok in query.split():
        t = tok.translate(_ACCENT_TABLE)
        core = t.strip('",.:;()!?')
        if ("-" in core or "_" in core) and not core.startswith("-"):
            parts = [p for p in core.replace("_", " ").replace("-", " ").split() if p]
            t = '"' + " ".join(parts) + '"' if len(parts) > 1 else (parts[0] if parts else t)
        out.append(t)
    return " ".join(out)


def query_ngrams(query: str, n_max: int = 3, caps_only: bool = False) -> list:
    """Lower-cased 1..n-grams of the query's word tokens, used to anchor the entity arm.
    Unigrams that are stopwords or shorter than 3 characters are dropped. With `caps_only`
    an n-gram must contain a token the user wrote Capitalised or with an underscore
    (proper-noun-ish), which keeps generic words from anchoring on generic entities."""
    raw = re.findall(r"[A-Za-z0-9][A-Za-z0-9_'\-]*", query)
    toks = [t.lower().strip("'\"") for t in raw]
    toks = [t[:-2] if t.endswith("'s") else t for t in toks]
    proper = [(t[:1].isupper() or "_" in t) for t in raw]
    out = set()
    for n in range(1, n_max + 1):
        for i in range(len(toks) - n + 1):
            g = toks[i:i + n]
            if n == 1 and (g[0] in _STOPWORDS or len(g[0]) < 3):
                continue
            if caps_only and not any(proper[i:i + n]):
                continue
            out.add(" ".join(g))
    return sorted(out)


def parse_when(value):
    """KB_MCP server.py `_parse_when`: ISO date/datetime -> aware datetime (naive = UTC)."""
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    got = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    return got if got.tzinfo else got.replace(tzinfo=timezone.utc)


def admissible_at(ingested_at, asof, events=()) -> bool:
    """As-of rule for replaying a traced call against today's corpus: a node (or a gold
    member) counts only if it existed when the call was made. `asof=None` admits all.

    `ingested_at` is the node's LAST (re)creation: a delete + recreate under the same id
    resets it, so on its own it would read a months-old note as born at the recreate.
    A node still present with `ingested_at <= asof` has existed ever since, so it is
    admitted outright. Otherwise its ingest_log history decides: `events` is
    `[(at, event), ...]` of its `node_created` / `node_deleted` rows in log order. The last
    one at/before `asof` wins (created -> existed, deleted -> did not); with none before
    `asof`, a first later event that is a delete means it already existed (it predates the
    log), and a create means it did not."""
    if asof is None:
        return True
    t = parse_when(asof)
    if ingested_at is not None and parse_when(ingested_at) <= t:
        return True
    evs = [(parse_when(at), ev) for at, ev in events]
    prior = [ev for at, ev in evs if at <= t]
    if prior:
        return prior[-1] == "node_created"
    return bool(evs) and evs[0][1] == "node_deleted"


def norm_args(args: dict) -> tuple:
    """MCP tool args -> (KnowledgeBase.search kwargs, limit), with the tool's clamp/defaults."""
    a = dict(args or {})
    limit = a.pop("limit", None)
    limit = DEFAULT_LIMIT if limit is None else max(1, min(MAX_LIMIT, int(limit)))
    inc = a.pop("include_dead", None)
    kw = {"under": a.get("under"), "source": a.get("source"), "type": a.get("type"),
          "tags": a.get("tags"), "kind": a.get("kind"),
          "since": parse_when(a.get("since")), "until": parse_when(a.get("until")),
          "include_dead": True if inc is None else bool(inc)}
    return kw, limit


# ------------------------------------------------------------------------------ metrics
def must_recall(ids: list, must: list, k: int) -> float:
    """run.py's must_recall@k: |must ∩ top-k| / |must|."""
    top = set(ids[:k])
    return sum(m in top for m in must) / len(must)


def first_hit_rank(ids: list, must: list, k: int):
    ranks = [ids.index(m) + 1 for m in must if m in ids[:k]]
    return min(ranks) if ranks else None


def group_recall(ids: list, groups: dict, k: int) -> tuple:
    """Group recall: `groups` maps a group key to the set of member ids the gold treats as
    interchangeable answers (mirror copies of one document, or a family the annotators
    grouped -- see load_traffic_gold); a hit on any member counts the group once.
    Returns (hit_groups, total_groups)."""
    top = set(ids[:k])
    hits = sum(1 for members in groups.values() if any(m in top for m in members))
    return hits, len(groups)


def capped_recall(ids: list, groups: dict, k: int) -> float:
    """Group recall with the denominator capped at k (a call asking for k=6 cannot return
    7 central documents); 1.0 when the page is full of gold."""
    hits, total = group_recall(ids, groups, k)
    return hits / min(total, k) if total else 0.0


def graded_ndcg(ids: list, gains: dict, member_group: dict, k: int) -> float:
    """nDCG@k with graded gains per GROUP (gains keyed by group; `member_group` maps a
    member id to its group). A group is credited once, at its best-ranked member; the
    ideal ordering is the groups' gains sorted descending."""
    seen, dcg = set(), 0.0
    for n, ident in enumerate(ids[:k]):
        g = member_group.get(ident)
        if g is None or g in seen:
            continue
        seen.add(g)
        dcg += gains[g] / math.log2(n + 2)
    ideal = sorted(gains.values(), reverse=True)[:k]
    idcg = sum(g / math.log2(n + 2) for n, g in enumerate(ideal))
    return dcg / idcg if idcg else 0.0


def mean(xs) -> float:
    xs = list(xs)
    return sum(xs) / len(xs) if xs else float("nan")


# ============================================================================ configuration
DEFAULT_DSN = "postgresql://kb@localhost:5433/kb_mcp"


class CannotRun(RuntimeError):
    """The harness cannot run (as opposed to: it ran and found a mismatch)."""


class EmbedBudgetExceeded(CannotRun):
    """A run would need more fresh query embeddings than --max-embeds allows."""


def eval_data_dir() -> Path:
    root = os.environ.get("KB_EVAL_DATA_DIR")
    if not root:
        raise SystemExit("KB_EVAL_DATA_DIR is unset. The shadow harness scores the REAL gold "
                         "sets ($KB_EVAL_DATA_DIR/retrieval/golden.jsonl and "
                         "traffic_gold.jsonl); the synthetic sets in this directory do not "
                         "match a live database.")
    return Path(root)


def readonly_conninfo(dsn: str | None = None) -> str:
    """The DSN with `default_transaction_read_only=on` FORCED into `options` (appended to
    any options already present) and a recognisable application_name."""
    from psycopg.conninfo import conninfo_to_dict, make_conninfo
    dsn = dsn or os.environ.get("KB_DATABASE_URL", DEFAULT_DSN)
    d = conninfo_to_dict(dsn)
    opts = (d.get("options") or "").strip()
    d["options"] = (opts + " -c default_transaction_read_only=on").strip()
    d["application_name"] = "kb-shadow-ro"
    return make_conninfo(**d)


def make_kb(dsn: str | None = None):
    """Production KnowledgeBase on a read-only pool with the search path's only write
    (`_record_access`, also exposed as `record_access`) replaced by a no-op."""
    from kb_core.db import open_pool
    from kb_core.knowledge_base import KnowledgeBase
    from kb_core.registry import default_registry
    pool = open_pool(readonly_conninfo(dsn), max_size=4)
    kb = KnowledgeBase(pool, default_registry())
    kb._record_access = lambda ids: None
    kb.record_access = lambda ids: None
    return kb


READ_ONLY_SQLSTATE = "25006"   # read_only_sql_transaction


def assert_readonly(kb) -> str:
    """Prove the pool refuses writes: the session must report `transaction_read_only = on`
    AND an UPDATE that would touch no row must be rejected with SQLSTATE 25006
    (read_only_sql_transaction). Any OTHER error -- wrong database, missing table, missing
    privilege, timeout -- proves nothing and is raised. Returns the server's refusal text.

    This is a session DEFAULT: a transaction that ran `SET TRANSACTION READ WRITE` could
    lift it. The harness never does; for a privilege-level guarantee connect as a role
    that only has SELECT."""
    import psycopg
    with kb._pool.connection() as conn:
        row = conn.execute("SHOW transaction_read_only").fetchone()
        mode = row["transaction_read_only"] if isinstance(row, dict) else row[0]
        if mode != "on":
            conn.rollback()
            raise CannotRun(f"the shadow connection reports transaction_read_only={mode!r}; "
                            "refusing to run")
        try:
            conn.execute("UPDATE nodes SET access_count = access_count WHERE false")
        except psycopg.Error as exc:
            conn.rollback()
            if getattr(exc, "sqlstate", None) == READ_ONLY_SQLSTATE:
                return str(exc).strip()
            raise CannotRun(f"the read-only probe failed for another reason (SQLSTATE "
                            f"{getattr(exc, 'sqlstate', None)}: {str(exc).strip()}), so it "
                            "proves nothing; refusing to run") from exc
    raise CannotRun("the shadow connection accepted an UPDATE -- it is NOT read-only; "
                    "refusing to run")


def file_sha256(path) -> str | None:
    p = Path(path)
    return hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else None


def file_digest(path) -> dict | None:
    """{path, sha256, lines} of a file (None when it does not exist) -- for jsonl gold,
    `lines` is the item count."""
    p = Path(path)
    if not p.is_file():
        return None
    b = p.read_bytes()
    return {"path": str(p), "sha256": hashlib.sha256(b).hexdigest(),
            "lines": sum(1 for line in b.splitlines() if line.strip())}


def git_state() -> dict:
    """What 'production' meant when a result was produced: HEAD, the uncommitted engine diff
    (the working tree is shared with other sessions) and the harness's own files, which may
    be untracked or locally edited. `kb_core_diff_sha256` hashes the full diff plus every
    untracked file under KB_Core/src (name and content), so two different edits never
    compare equal the way two equal-sized `--stat` summaries can."""
    def run(*argv):
        try:
            return subprocess.run(["git", *argv], cwd=REPO_ROOT, capture_output=True,
                                  text=True, check=True).stdout.strip()
        except Exception as exc:  # noqa: BLE001 -- git may be absent on a mirror
            return f"unavailable: {exc}"
    untracked = run("ls-files", "--others", "--exclude-standard", "--", "KB_Core/src")
    untracked = [] if untracked.startswith("unavailable") else untracked.splitlines()
    h = hashlib.sha256(run("diff", "--", "KB_Core/src").encode())
    for rel in sorted(untracked):
        h.update(b"\0" + rel.encode() + b"\0")
        if (REPO_ROOT / rel).is_file():
            h.update((REPO_ROOT / rel).read_bytes())
    return {"head": run("rev-parse", "HEAD"),
            "kb_core_diff_stat": run("diff", "--stat", "--", "KB_Core/src"),
            "kb_core_diff_sha256": h.hexdigest(), "kb_core_untracked": untracked,
            "harness": {p.name: file_sha256(p) for p in sorted(HERE.glob("shadow*.py"))}}


def run_provenance(argv=None) -> dict:
    """Everything besides git that decides a result: the command line, which database
    (never the password), and the policy file the FTS importance factor reads."""
    from psycopg.conninfo import conninfo_to_dict
    d = conninfo_to_dict(os.environ.get("KB_DATABASE_URL", DEFAULT_DSN))
    pol = os.environ.get("KB_POLICY_CONFIG")
    return {"argv": list(sys.argv if argv is None else argv),
            "db": {k: d.get(k) for k in ("host", "port", "dbname")},
            "policy": ({"path": pol, "sha256": file_sha256(pol)} if pol else None)}


def run_cli(name: str, body, argv=None) -> int:
    """The CLIs' exit-code contract: `body(argv)`'s own code (0, or 1 for a fidelity
    mismatch), or 2 when the harness COULD NOT RUN -- missing configuration or gold, the
    database or embedding service unreachable, the embedding budget too small -- so an
    automated gate never mistakes "could not run" for "replica diverged". The pool is
    always closed (its worker threads otherwise stall interpreter exit by ~20 s)."""
    try:
        return body(argv)
    except SystemExit as exc:
        if exc.code is None or isinstance(exc.code, int):     # argparse / --help / explicit
            raise
        print(f"{name}: could not run: {exc.code}", file=sys.stderr)
        return 2
    except CannotRun as exc:
        print(f"{name}: could not run: {exc}", file=sys.stderr)
        return 2
    except Exception:  # noqa: BLE001 -- DB / network failures: keep the traceback, exit 2
        import traceback
        traceback.print_exc()
        print(f"{name}: could not run (unexpected error above)", file=sys.stderr)
        return 2
    finally:
        close()


# ============================================================================ cache files
@contextlib.contextmanager
def _file_lock(path: Path):
    """Exclusive advisory lock on a sidecar `<name>.lock` (concurrent sessions share the
    cache directory); a no-op where fcntl is unavailable."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if fcntl is None:
        yield
        return
    with open(path.with_name(path.name + ".lock"), "a") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def _atomic_write_json(path: Path, data) -> None:
    """Write to a per-process temp file, then rename over `path`: a reader never sees a
    half-written file and two writers never share a temp name."""
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with open(tmp, "w") as fh:
        json.dump(data, fh)
    os.replace(tmp, path)


# ============================================================================ embeddings
class Embedder:
    """The production embedding client (`HTTPInferenceClient.embed`, text capped at 6000
    chars) behind an on-disk cache keyed by (model, exact prefix template, query text).
    `max_calls` bounds fresh calls so a bench cannot silently spend an embedding budget.

    The model component of the key is the LIVE service's embedding model (its /health,
    no inference), never the name stored in the cache file: after a model swap the old
    model's vectors simply miss instead of being served against re-embedded documents.
    An unreachable service raises CannotRun unless KB_SHADOW_TRUST_CACHE_MODEL=1, which
    falls back to the cache file's model name (recorded as unverified)."""

    KEY_SEP = "\x1f"

    def __init__(self, url: str | None = None, timeout: float = 60.0,
                 cache_path: Path = QVEC_CACHE, max_calls: int | None = 50):
        from kb_core.ingest.inference import LOCAL_INFERENCE_URL, HTTPInferenceClient
        self.url = url or os.environ.get("KB_INFERENCE_URL", LOCAL_INFERENCE_URL)
        self.client = HTTPInferenceClient(self.url, timeout=timeout)
        self.cache_path = Path(cache_path)
        self.calls = 0
        self.max_calls = max_calls
        self.cache_model, self.cache = None, {}
        if self.cache_path.exists():
            data = json.load(open(self.cache_path))
            self.cache_model = data.get("model")
            self.cache = data.get("entries", {})
        self.live_model = self._probe_live_model(HTTPInferenceClient)
        if self.live_model:
            self.model = self.live_model
            if self.cache_model and self.cache_model != self.live_model:
                print(f"warning: the query-vector cache was written for embedding model "
                      f"{self.cache_model!r} but the service now serves {self.live_model!r}; "
                      "the old model's vectors will miss and be re-embedded", file=sys.stderr)
        elif os.environ.get("KB_SHADOW_TRUST_CACHE_MODEL") == "1":
            self.model = self.cache_model or "unknown"
            print(f"warning: embedding service at {self.url} unreachable; trusting the cache's "
                  f"model name {self.model!r} UNVERIFIED (KB_SHADOW_TRUST_CACHE_MODEL=1)",
                  file=sys.stderr)
        else:
            raise CannotRun(f"embedding service at {self.url} is unreachable, so the live "
                            "embedding model cannot be checked against the query-vector "
                            "cache; start it, or set KB_SHADOW_TRUST_CACHE_MODEL=1 to trust "
                            f"the cache's model name {self.cache_model!r}")

    def _probe_live_model(self, client_cls) -> str | None:
        try:
            health = client_cls(self.url, timeout=5.0).health()
        except Exception:  # noqa: BLE001 -- unreachable: the caller decides
            return None
        return (health.get("embedding") or {}).get("model") or None

    def key(self, text: str, prefix: str = "") -> str:
        return self.KEY_SEP.join([self.model, prefix or "", text])

    def missing(self, pairs) -> list:
        """The distinct `(text, prefix)` pairs that are not cached, i.e. the fresh embedding
        calls a run over `pairs` will make."""
        return sorted({(t, p or "") for t, p in pairs if self.key(t, p or "") not in self.cache})

    def _save(self) -> None:
        """Merge-on-write under a lock, so concurrent runs never drop each other's vectors."""
        with _file_lock(self.cache_path):
            merged = {}
            if self.cache_path.exists():
                try:
                    merged = json.load(open(self.cache_path)).get("entries", {})
                except (OSError, ValueError):
                    merged = {}
            merged.update(self.cache)
            self.cache = merged
            _atomic_write_json(self.cache_path, {"model": self.model,
                                                 "key": "model\\x1fprefix_template\\x1fquery",
                                                 "entries": merged})

    def __call__(self, text: str, prefix: str | None = None) -> list:
        prefix = prefix or ""
        k = self.key(text, prefix)
        if k in self.cache:
            return self.cache[k]
        if self.max_calls is not None and self.calls >= self.max_calls:
            raise EmbedBudgetExceeded(f"embedding budget exhausted ({self.max_calls} fresh "
                                      "calls); raise --max-embeds deliberately")
        embed_text = (prefix.format(q=text) if prefix else text)[:EMBED_TEXT_CAP]
        vec = self.client.embed([embed_text])[0]
        self.calls += 1
        self.model = self.client.embed_model or self.model
        self.cache[self.key(text, prefix)] = vec
        self._save()
        return vec


def check_embed_budget(pairs, budget: int | None) -> int:
    """Count the fresh embeddings a run over `pairs` needs BEFORE any search, and refuse
    (CannotRun -> exit 2) when it exceeds the budget instead of dying mid-run. A cold
    cache (fresh clone or machine: the cache is gitignored) needs one call per distinct
    query and prefix. Returns the count."""
    emb = embedder()
    need = len(emb.missing(pairs))
    if budget is not None and need > budget:
        where = (emb.cache_path.relative_to(HERE) if emb.cache_path.is_relative_to(HERE)
                 else emb.cache_path)
        raise EmbedBudgetExceeded(f"this run needs {need} fresh query embeddings (cold or "
                                  f"partial cache at {where}) but --max-embeds is {budget}; "
                                  f"rerun with --max-embeds {need}")
    return need


# ============================================================================ DF cache
DF_SCOPE = "type = 'note' AND dead_at IS NULL"


def build_df_cache(kb) -> dict:
    """Lexeme document frequencies over the live notes' content_tsv (ts_stat), plus the
    corpus size and average content length the BM25 twin needs. Read-only on the DB."""
    with kb._pool.connection() as c:
        rows = c.execute(f"SELECT word, ndoc FROM ts_stat($$SELECT content_tsv FROM nodes "
                         f"WHERE {DF_SCOPE}$$)").fetchall()
        n = c.execute(f"SELECT count(*) AS n, avg(length(content)) AS avglen FROM nodes "
                      f"WHERE {DF_SCOPE}").fetchone()
    data = {"N": int(n["n"]), "avglen": float(n["avglen"] or 1.0),
            "df": {r["word"]: int(r["ndoc"]) for r in rows},
            "config": "english", "scope": "type='note' AND dead_at IS NULL",
            "built_at": datetime.now(timezone.utc).isoformat()}
    with _file_lock(DF_CACHE):
        _atomic_write_json(DF_CACHE, data)
    return data


def df_cache_info(kb=None) -> dict | None:
    """The DF snapshot a BM25 result depends on: {N, avglen, lexemes, built_at, sha256},
    plus `live_N` (the same scope counted now) when `kb` is given. None without a cache."""
    if not DF_CACHE.exists() and _DF is None:
        return None
    d = df_cache()
    info = {"N": d["N"], "avglen": round(d["avglen"], 1), "lexemes": len(d["df"]),
            "built_at": d.get("built_at"), "sha256": file_sha256(DF_CACHE)}
    if kb is not None:
        with kb._pool.connection() as c:
            info["live_N"] = int(c.execute(f"SELECT count(*) AS n FROM nodes WHERE {DF_SCOPE}")
                                 .fetchone()["n"])
    return info


_DF = None


def df_cache(kb=None) -> dict:
    global _DF
    if _DF is None:
        if DF_CACHE.exists():
            _DF = json.load(open(DF_CACHE))
        elif kb is not None:
            _DF = build_df_cache(kb)
        else:
            raise SystemExit(f"no DF cache at {DF_CACHE.relative_to(REPO_ROOT)}; run "
                             "shadow_bench.py --refresh-df")
    return _DF


# ============================================================================ PROD path
PROD_CONSTANT_KNOBS = {"HYBRID_ARM_POOL", "ANN_CHUNK_FANOUT", "RRF_K", "OR_FALLBACK_MAX_WORDS"}


@contextlib.contextmanager
def patched_constants(**over):
    import kb_core.knowledge_base as kbmod
    saved = {}
    for k, v in over.items():
        if v is None:
            continue
        saved[k] = getattr(kbmod, k)
        setattr(kbmod, k, v)
    try:
        yield
    finally:
        for k, v in saved.items():
            setattr(kbmod, k, v)


def prod_search(kb, query: str, args: dict, vec, **constants) -> dict:
    kw, limit = norm_args(args)
    t0 = time.perf_counter()
    with patched_constants(**constants):
        hits = kb.search(query, embedding=vec, limit=limit, **kw)
    ms = (time.perf_counter() - t0) * 1000
    rows = [{"id": str(h.node.id), "rrf": float(h.rank), "similarity": h.similarity,
             "score": h.score, "path": h.node.path, "dead": h.node.dead_at is not None}
            for h in hits]
    return {"ids": [r["id"] for r in rows], "rows": rows, "latency_ms": ms, "path": "prod",
            "limit": limit}


# ============================================================================ REPLICA path
KNOB_DEFAULTS = dict(
    # --- candidate pools / ANN
    pool=None,            # per-arm pool; None -> max(limit, HYBRID_ARM_POOL)
    fanout=None,          # chunk over-fetch multiple; None -> ANN_CHUNK_FANOUT
    annnotes=None,        # whole-note leg limit; None -> pool
    ef_search=None,       # SET LOCAL hnsw.ef_search; None -> server default
    iterative=True,       # SET LOCAL hnsw.iterative_scan = relaxed_order (production does)
    exact=False,          # force exhaustive scans on the vector legs (ANN ceiling)
    # --- fusion
    rrfk=None,            # None -> RRF_K
    w_fts=1.0, w_knn=1.0,
    w_fts_or=None,        # FTS weight used INSTEAD of w_fts when the OR fallback fired
    w_fts_or_long_only=False,  # ... only for queries longer than OR_FALLBACK_MAX_WORDS
    or_requires_knn=False,     # OR fallback: drop FTS candidates not also in the kNN pool
    # --- lexical arm
    fts_mode="prod",      # prod | and | or_all | or_if_empty_scoped | or_if_few_scoped | plainto | simple | none
    or_max_words=None,    # length cap of the production fallback; None -> OR_FALLBACK_MAX_WORDS
    or_few_k=None,        # or_if_few_scoped: fire the OR form when AND matches (in scope) < this; None -> limit
    or_min_cov=None,      # OR form: keep notes containing >= this fraction of the query's lexemes
    or_rank="rank",       # rank | cov_then_rank (OR form only)
    fts_score="prod",     # prod (ts_rank) | idf_sum | bm25
    fts_score_scope="or_only",  # or_only | all
    bm25_k1=1.2, bm25_b=0.75,
    lexnorm=None,         # None | query | docs | both (see normalize_query / _tsv_expr)
    importance="prod",    # prod | off | no_decay | no_access
    # --- vector arm shape
    chunk_agg="max",      # max | sum_top3 | mean_top2 | max_bonus
    leg_merge="max",      # max | mean | note_only
    stub_guard=None,      # None | fts_empty | always: whole-note similarity x stub_penalty below stub_chars
    stub_chars=SHORT_NOTE_CHARS, stub_penalty=0.9,
    # --- extra arms / filters
    w_ent=None,           # entity-anchored third RRF list weight (None = off)
    ent_trgm=0.6, ent_min_len=5, ent_link_type="mentions", ent_caps_only=False,
    exclude_path_regex=None,  # drop notes whose path matches (every arm)
    asof=None,            # ISO timestamptz: only nodes that existed at this instant (admissible_at)
    knn_only=False, fts_only=False,
    dedup=None,           # cosine threshold for near-duplicate collapse in the fused pool
    dedup_depth=60,
    query_prefix=QUERY_INSTRUCTION,   # instruction template with {q}; BARE = raw query vector
)

_TSQ = {
    "prod": "websearch_to_tsquery('english', %(q)s)",
    "and": "websearch_to_tsquery('english', %(q)s)",
    "or_all": "websearch_to_tsquery('english', %(q)s)",
    "or_if_empty_scoped": "websearch_to_tsquery('english', %(q)s)",
    "or_if_few_scoped": "websearch_to_tsquery('english', %(q)s)",
    "plainto": "plainto_tsquery('english', %(q)s)",
    "simple": "websearch_to_tsquery('simple', %(q)s)",
}
# Allowed VALUES of the enumerated knobs: a typo must fail, not fall through to some other
# behaviour (e.g. importance="no-access" silently meaning "off").
KNOB_CHOICES = {
    "fts_mode": set(_TSQ) | {"none"},
    "or_rank": {"rank", "cov_then_rank"},
    "fts_score": {"prod", "idf_sum", "bm25"},
    "fts_score_scope": {"or_only", "all"},
    "lexnorm": {None, "query", "docs", "both"},
    "importance": {"prod", "off", "no_decay", "no_access"},
    "chunk_agg": {"max", "sum_top3", "mean_top2", "max_bonus"},
    "leg_merge": {"max", "mean", "note_only"},
    "stub_guard": {None, "fts_empty", "always"},
}
# The replica's twins of the production constants (which only the PROD path patches).
_REPLICA_TWIN = {"HYBRID_ARM_POOL": "pool", "ANN_CHUNK_FANOUT": "fanout", "RRF_K": "rrfk",
                 "OR_FALLBACK_MAX_WORDS": "or_max_words"}


def validate_knobs(knobs: dict) -> None:
    """Replica knobs: keys must be KNOB_DEFAULTS keys, enumerated values must be allowed."""
    unknown = set(knobs) - set(KNOB_DEFAULTS)
    if unknown:
        twins = {u: _REPLICA_TWIN[u] for u in unknown if u in _REPLICA_TWIN}
        hint = (f"; production constants are PROD-path only (alone in overrides) -- on the "
                f"replica use {twins}" if twins else "")
        raise ValueError(f"unknown knobs {sorted(unknown)}{hint}")
    bad = {key: knobs[key] for key, ok in KNOB_CHOICES.items()
           if key in knobs and knobs[key] not in ok}
    if bad:
        raise ValueError(f"invalid knob values {bad}; allowed: "
                         f"{ {key: sorted(KNOB_CHOICES[key], key=str) for key in bad} }")


_ACC_IDS = None


def accented_note_ids(kb) -> list:
    """Notes whose tsvector holds a non-ASCII lexeme -- the only ones the on-the-fly
    document-side normalisation (`lexnorm` docs/both) has to recompute."""
    global _ACC_IDS
    if _ACC_IDS is None:
        with kb._pool.connection() as c:
            _ACC_IDS = [r["id"] for r in c.execute(
                "SELECT id::text AS id FROM nodes n WHERE type = 'note' AND EXISTS "
                "(SELECT 1 FROM unnest(tsvector_to_array(n.content_tsv)) l WHERE l ~ '[^ -~]')"
            ).fetchall()]
    return _ACC_IDS


def _engine():
    import kb_core.knowledge_base as kbmod
    return kbmod


def _imp_params(kb) -> dict:
    return {"floor": kb._policy.importance.recency_floor, "ln2": math.log(2),
            "hl": kb._policy.importance.recency_half_life_seconds}


_HIDDEN: dict = {}


def asof_hidden_ids(kb, asof) -> list:
    """Ids of the nodes that did NOT exist at `asof` under admissible_at's rule (cached per
    instant). Only a node whose ingested_at is after `asof` can be hidden -- one still
    present with ingested_at <= asof has existed ever since -- so one read of those nodes
    and their ingest_log create/delete history decides. This is what makes a replay before
    a mass delete + recreate (which resets ingested_at) see the corpus that existed."""
    if not asof:
        return []
    key = parse_when(asof).isoformat()
    if key not in _HIDDEN:
        with kb._pool.connection() as c:
            rows = c.execute("""
                SELECT n.id::text AS id, n.ingested_at,
                       coalesce(array_agg(l.at ORDER BY l.at, l.id) FILTER (WHERE l.id IS NOT NULL),
                                '{}') AS ats,
                       coalesce(array_agg(l.event ORDER BY l.at, l.id) FILTER (WHERE l.id IS NOT NULL),
                                '{}') AS evs
                FROM nodes n
                LEFT JOIN ingest_log l ON l.node_id = n.id
                                      AND l.event IN ('node_created', 'node_deleted')
                WHERE n.ingested_at IS NULL OR n.ingested_at > %(a)s::timestamptz
                GROUP BY n.id, n.ingested_at""", {"a": key}).fetchall()
        _HIDDEN[key] = sorted(r["id"] for r in rows if not admissible_at(
            r["ingested_at"], key, list(zip(r["ats"], r["evs"]))))
    return _HIDDEN[key]


# Every caller's FROM has `nodes` in scope unaliased; qualified because some also join a
# relation with its own `id` column.
_ASOF_SQL = "\n                  AND NOT (nodes.id = ANY(%(asof_hidden)s::uuid[]))"


def _base_params(kb, kw, limit, pool, k) -> dict:
    p = {**kw, "limit": limit, "pool": pool,
         "nostub": list(_engine().SEARCH_SCAFFOLDING_TYPES), "inc_dead": kw["include_dead"]}
    p.pop("include_dead")
    p.update(_imp_params(kb))
    p["asof"] = k.get("asof")
    p["asof_hidden"] = asof_hidden_ids(kb, k.get("asof"))
    p["xre"] = k.get("exclude_path_regex")
    p.update(acc_ids=accented_note_ids(kb) if k.get("lexnorm") in ("docs", "both") else [],
             acc_from=_ACCENT_FROM, acc_to=_ACCENT_TO)
    return p


def filters_sql(k) -> str:
    """The engine's SEARCH_FILTERS plus the replica-only as-of / path-exclusion clauses."""
    f = _engine().SEARCH_FILTERS
    if k.get("asof"):
        f += _ASOF_SQL
    if k.get("exclude_path_regex"):
        f += "\n                  AND path::text !~ %(xre)s"
    return f


_ACCESS_SHARE_MARKER = "(importance - 1.0)"


def _imp_expr(importance: str) -> str:
    imp = _engine()._IMP_FACTOR
    if importance == "prod":
        return imp
    if importance == "no_decay":
        return "importance"
    if importance == "off":
        return "1.0"
    if importance == "no_access":
        # the access modifier is additive (0.5 * a/(a+saturation)) on top of the base, so
        # freezing it at 0 while keeping every other modifier = subtract its stored share
        if _ACCESS_SHARE_MARKER not in imp:
            raise RuntimeError("the engine's _IMP_FACTOR no longer contains "
                               f"{_ACCESS_SHARE_MARKER!r}; importance='no_access' would "
                               "silently equal production -- update shadow._imp_expr")
        return imp.replace(_ACCESS_SHARE_MARKER,
                           "((importance - 0.5 * coalesce((importance_modifiers->>'access')"
                           "::float, 0)) - 1.0)")
    raise ValueError(f"importance={importance!r}; allowed: {sorted(KNOB_CHOICES['importance'])}")


def _tsv_expr(k) -> str:
    if k.get("fts_mode") == "simple":
        return "to_tsvector('simple', coalesce(title,'') || ' ' || content)"
    if k.get("lexnorm") in ("docs", "both"):
        return ("(CASE WHEN id = ANY(%(acc_ids)s::uuid[]) THEN to_tsvector('english', "
                "translate(coalesce(title,'') || ' ' || content, %(acc_from)s, %(acc_to)s)) "
                "ELSE content_tsv END)")
    return "content_tsv"


def fts_query_text(kb, conn, query, kw, k) -> tuple:
    """Which text the lexical arm searches, and whether the OR fallback fired.
    prod: the engine's gate -- AND form probed corpus-wide WITHOUT filters, OR-join when
    nothing matched and 1 < words <= OR_FALLBACK_MAX_WORDS, words being the engine's own
    `query.split()` (fallback_terms). The scoped variants probe inside the filter block
    (and the as-of clause)."""
    mode, words = k["fts_mode"], fallback_terms(query, k)
    tsv = _tsv_expr(k)
    p = _base_params(kb, kw, 1, 1, k); p["q"] = query
    cap = k["or_max_words"] if k["or_max_words"] is not None else _engine().OR_FALLBACK_MAX_WORDS
    if mode == "prod":
        if 1 < len(words) <= cap:
            asof = _ASOF_SQL if k.get("asof") else ""
            hit = conn.execute(f"SELECT EXISTS (SELECT 1 FROM nodes WHERE {tsv} @@ "
                               f"websearch_to_tsquery('english', %(q)s){asof}) AS hit",
                               p).fetchone()["hit"]
            if not hit:
                return or_join(words), True
        return query, False
    if mode == "or_all":
        return (or_join(words), True) if len(words) > 1 else (query, False)
    if mode in ("or_if_empty_scoped", "or_if_few_scoped"):
        if len(words) > 1:
            n = conn.execute(f"SELECT count(*) AS c FROM nodes WHERE {tsv} @@ "
                             f"websearch_to_tsquery('english', %(q)s) {filters_sql(k)}",
                             p).fetchone()["c"]
            need = 1 if mode == "or_if_empty_scoped" else (k["or_few_k"] or k["_limit"])
            if n < need:
                return or_join(words), True
        return query, False
    return query, False


def query_lexemes(conn, query: str) -> list:
    return conn.execute("SELECT tsvector_to_array(to_tsvector('english', %(q)s)) AS a",
                        {"q": query}).fetchone()["a"] or []


def fts_arm(kb, conn, q_fts, kw, pool, k, *, or_fired=False, query=None) -> list:
    mode = k["fts_mode"]
    if mode == "none" or not q_fts:
        return []
    tsq, tsv, filt = _TSQ[mode], _tsv_expr(k), filters_sql(k)
    rank = f"ts_rank({tsv}, {tsq})"
    imp = _imp_expr(k["importance"])
    p = _base_params(kb, kw, pool, pool, k); p["q"] = q_fts
    new_score = k["fts_score"] != "prod" and (k["fts_score_scope"] == "all" or or_fired)
    need_cov = or_fired and (k["or_min_cov"] is not None or k["or_rank"] != "rank")
    if new_score or need_cov:
        lex = query_lexemes(conn, query or q_fts)
        df = df_cache(kb)
        idf = [bm25_idf(df["df"].get(lexeme, 0), df["N"]) for lexeme in lex]
        p.update(lex=lex, idf=idf, need=max(1, math.ceil((k["or_min_cov"] or 0.0) * len(lex))),
                 k1=k["bm25_k1"], b=k["bm25_b"], avglen=df["avglen"])
        if k["fts_score"] == "bm25":
            tfw = ("(array_length(u.positions, 1) * (%(k1)s + 1.0) / (array_length(u.positions, 1) "
                   "+ %(k1)s * (1 - %(b)s + %(b)s * length(m.content) / %(avglen)s)))")
            agg = f"sum(q.idf * {tfw})"
        elif k["fts_score"] == "idf_sum":
            agg = "sum(q.idf)"
        else:
            agg = "NULL::float"
        score_src = "s.rscore" if new_score else f"ts_rank(m.tsv_, {tsq})"
        order = ("cov DESC, " if k["or_rank"] == "cov_then_rank" else "") + "score DESC, id"
        # Lexemes are unique within a tsvector, so count(*) == count(DISTINCT); filtering
        # INSIDE the LATERAL keeps the unnest from materialising ~1M rows on OR-flood queries.
        sql = f"""
            WITH m AS (
              SELECT id, importance, importance_modifiers, last_accessed_at, ingested_at,
                     content, {tsv} AS tsv_
              FROM nodes WHERE {tsv} @@ {tsq} {filt}),
            s AS (
              SELECT m.id, count(*) AS cov, {agg} AS rscore
              FROM m
              JOIN LATERAL (SELECT lexeme, positions FROM unnest(m.tsv_) t(lexeme, positions, weights)
                            WHERE lexeme = ANY(%(lex)s::text[])) u ON TRUE
              JOIN unnest(%(lex)s::text[], %(idf)s::float[]) q(l, idf) ON q.l = u.lexeme
              GROUP BY m.id),
            sc AS (
              SELECT m.id, ts_rank(m.tsv_, {tsq}) AS relevance, s.cov, {score_src} * {imp} AS score
              FROM m JOIN s ON s.id = m.id WHERE s.cov >= %(need)s)
            SELECT id, relevance, score, cov, row_number() OVER (ORDER BY {order}) AS r
            FROM sc ORDER BY r LIMIT %(pool)s"""
        return [dict(x) for x in conn.execute(sql, p).fetchall()]
    decayed = f"{rank} * {imp}"
    sql = f"""
        SELECT id, {rank} AS relevance, {decayed} AS score,
               row_number() OVER (ORDER BY {decayed} DESC, id) AS r
        FROM nodes WHERE {tsv} @@ {tsq} {filt}
        ORDER BY r LIMIT %(pool)s"""
    return [dict(x) for x in conn.execute(sql, p).fetchall()]


def _chunk_leg(agg: str) -> str:
    if agg == "max":
        return """(SELECT nodes.id, nodes.importance, 1 - ch.dist AS similarity, 'chunk' AS leg
                   FROM (SELECT node_id, (embedding <=> %(vec)s::vector) AS dist
                         FROM node_chunks
                         ORDER BY embedding <=> %(vec)s::vector
                         LIMIT %(annchunks)s) ch
                   JOIN nodes ON nodes.id = ch.node_id
                   WHERE TRUE {F})"""
    expr = {"sum_top3": "sum(1 - ch.dist) FILTER (WHERE ch.rn <= 3)",
            "mean_top2": "avg(1 - ch.dist) FILTER (WHERE ch.rn <= 2)",
            "max_bonus": "max(1 - ch.dist) + 0.01 * ln(count(*))"}[agg]
    return f"""(SELECT nodes.id, nodes.importance, {expr} AS similarity, 'chunk' AS leg
                FROM (SELECT node_id, dist, row_number() OVER (PARTITION BY node_id ORDER BY dist) AS rn
                      FROM (SELECT node_id, (embedding <=> %(vec)s::vector) AS dist
                            FROM node_chunks
                            ORDER BY embedding <=> %(vec)s::vector
                            LIMIT %(annchunks)s) x) ch
                JOIN nodes ON nodes.id = ch.node_id
                WHERE TRUE {{F}}
                GROUP BY nodes.id, nodes.importance)"""


def knn_arm(kb, conn, vec, kw, pool, k, *, annnotes, annchunks, stub_active=False) -> list:
    """The vector arm: whole-note leg UNION ALL chunk leg (each ANN-ordered, the shape the
    HNSW index can serve), reduced per note, ordered by similarity DESC, importance DESC, id."""
    filt = filters_sql(k)
    p = _base_params(kb, kw, pool, pool, k)
    if k["leg_merge"] == "note_only":
        annchunks = 0
    p.update(vec="[" + ",".join(repr(float(x)) for x in vec) + "]",
             annnotes=annnotes, annchunks=annchunks,
             stub_chars=k["stub_chars"], stub_penalty=k["stub_penalty"])
    chunk = _chunk_leg(k["chunk_agg"]).replace("{F}", filt)
    stub = ("* (CASE WHEN length(nodes.content) < %(stub_chars)s THEN %(stub_penalty)s ELSE 1.0 END)"
            if stub_active else "")
    ns = "max(similarity) FILTER (WHERE leg = 'note')"
    cs = "max(similarity) FILTER (WHERE leg = 'chunk')"
    merged = ("max(similarity)" if k["leg_merge"] in ("max", "note_only")
              else f"(coalesce({ns}, {cs}) + coalesce({cs}, {ns})) / 2")
    sql = f"""
        WITH cand AS (
            SELECT id, {merged} AS similarity, max(importance) AS importance,
                   {ns} AS note_sim, {cs} AS chunk_sim
            FROM (
                (SELECT nodes.id, nodes.importance,
                        (1 - (nodes.embedding <=> %(vec)s::vector)) {stub} AS similarity, 'note' AS leg
                 FROM nodes
                 WHERE nodes.embedding IS NOT NULL {filt}
                 ORDER BY nodes.embedding <=> %(vec)s::vector
                 LIMIT %(annnotes)s)
                UNION ALL
                {chunk}
            ) legs
            GROUP BY id
        )
        SELECT id, similarity, importance, note_sim, chunk_sim,
               row_number() OVER (ORDER BY similarity DESC, importance DESC, id) AS r
        FROM cand ORDER BY r LIMIT %(pool)s"""
    with conn.transaction():
        if k["iterative"]:
            conn.execute("SET LOCAL hnsw.iterative_scan = relaxed_order")
        if k["ef_search"] is not None:
            conn.execute(f"SET LOCAL hnsw.ef_search = {int(k['ef_search'])}")
        if k["exact"]:
            conn.execute("SET LOCAL enable_indexscan = off")
            conn.execute("SET LOCAL enable_bitmapscan = off")
        return [dict(x) for x in conn.execute(sql, p).fetchall()]


def entity_arm(kb, conn, query, kw, pool, k) -> tuple:
    """Third RRF list: live memories linked (entity_links) to active entities whose canonical
    name or alias equals a query n-gram, or is trigram-similar (>= ent_trgm) for n-grams of
    >= ent_min_len chars. Ordered by #matched entities, link weight, importance, recency."""
    p = _base_params(kb, kw, pool, pool, k)
    p.update(ngrams=query_ngrams(query, caps_only=k["ent_caps_only"]), trgm=k["ent_trgm"],
             minlen=k["ent_min_len"], lt=k["ent_link_type"])
    ents = conn.execute("""
        SELECT DISTINCT e.node_id::text AS id, e.canonical_name, g.g AS matched
        FROM entities e, unnest(%(ngrams)s::text[]) g(g)
        WHERE e.status = 'active'
          AND (lower(e.canonical_name) = g.g
               OR EXISTS (SELECT 1 FROM unnest(e.aliases) a WHERE lower(a) = g.g)
               OR (length(g.g) >= %(minlen)s AND similarity(lower(e.canonical_name), g.g) >= %(trgm)s))""",
                        p).fetchall()
    if not ents:
        return [], []
    p["ent_ids"] = [e["id"] for e in ents]
    rows = conn.execute(f"""
        WITH mem AS (
          SELECT el.memory_id AS id, count(DISTINCT el.entity_id) AS n_ent, sum(coalesce(el.weight, 0.5)) AS w
          FROM entity_links el WHERE el.entity_id = ANY(%(ent_ids)s::uuid[]) AND el.link_type = %(lt)s
          GROUP BY el.memory_id)
        SELECT nodes.id, mem.n_ent, mem.w,
               row_number() OVER (ORDER BY mem.n_ent DESC, mem.w DESC, nodes.importance DESC,
                                  COALESCE(nodes.event_at, nodes.source_created_at) DESC NULLS LAST, nodes.id) AS r
        FROM mem JOIN nodes ON nodes.id = mem.id WHERE TRUE {filters_sql(k)}
        ORDER BY r LIMIT %(pool)s""", p).fetchall()
    return [dict(x) for x in rows], [dict(e) for e in ents]


def path_order(conn, ids) -> dict:
    """Server-side `ORDER BY path` positions (ltree's native comparator, as production)."""
    if not ids:
        return {}
    rows = conn.execute("SELECT id FROM nodes WHERE id = ANY(%(ids)s) ORDER BY path",
                        {"ids": [str(i) for i in ids]}).fetchall()
    return {str(r["id"]): i for i, r in enumerate(rows)}


def near_duplicate_pairs(conn, ids: list, thr: float) -> list:
    rows = conn.execute("""
        SELECT a.id::text AS a, b.id::text AS b, 1 - (a.embedding <=> b.embedding) AS sim
        FROM nodes a JOIN nodes b ON a.id < b.id
        WHERE a.id = ANY(%(ids)s::uuid[]) AND b.id = ANY(%(ids)s::uuid[])
          AND a.embedding IS NOT NULL AND b.embedding IS NOT NULL
          AND 1 - (a.embedding <=> b.embedding) >= %(thr)s""",
                        {"ids": list(ids), "thr": thr}).fetchall()
    return [(r["a"], r["b"], float(r["sim"])) for r in rows]


def replica_search(kb, query: str, args: dict, vec, **knobs) -> dict:
    validate_knobs(knobs)
    k = {**KNOB_DEFAULTS, **knobs}
    eng = _engine()
    kw, limit = norm_args(args)
    k["_limit"] = limit
    pool = k["pool"] or max(limit, eng.HYBRID_ARM_POOL)
    annnotes = k["annnotes"] or pool
    annchunks = pool * (k["fanout"] if k["fanout"] is not None else eng.ANN_CHUNK_FANOUT)
    rrfk = k["rrfk"] if k["rrfk"] is not None else eng.RRF_K
    q_search = normalize_query(query, k["lexnorm"])
    t0 = time.perf_counter()
    with kb._pool.connection() as conn:
        with conn.transaction():
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
            if k["knn_only"]:
                q_fts, fired, fts = None, False, []
            else:
                q_fts, fired = fts_query_text(kb, conn, q_search, kw, k)
                fts = fts_arm(kb, conn, q_fts, kw, pool, k, or_fired=fired, query=q_search)
            stub_active = k["stub_guard"] == "always" or (k["stub_guard"] == "fts_empty" and not fts)
            knn = [] if (vec is None or k["fts_only"]) else knn_arm(
                kb, conn, vec, kw, pool, k, annnotes=annnotes, annchunks=annchunks,
                stub_active=stub_active)
            ent, ents = ([], [])
            if k["w_ent"] is not None:
                ent, ents = entity_arm(kb, conn, query, kw, pool, k)
            if fired and k["or_requires_knn"]:
                knn_ids = {str(r["id"]) for r in knn}
                fts = [r for r in fts if str(r["id"]) in knn_ids]
                for i, r in enumerate(fts, 1):
                    r["r"] = i
            cap = k["or_max_words"] if k["or_max_words"] is not None else eng.OR_FALLBACK_MAX_WORDS
            is_long = len(fallback_terms(q_search, k)) > cap      # the gate's own word count
            use_or_w = fired and k["w_fts_or"] is not None and (is_long or not k["w_fts_or_long_only"])
            w_f = k["w_fts_or"] if use_or_w else k["w_fts"]
            lists = [([str(r["id"]) for r in fts], w_f), ([str(r["id"]) for r in knn], k["w_knn"])]
            if ent:
                lists.append(([str(r["id"]) for r in ent], k["w_ent"]))
            fused = rrf_fuse(lists, rrfk)
            ordered = order_fused(fused, path_order(conn, list(fused)))
            pairs = []
            if k["dedup"] is not None:
                head = ordered[:k["dedup_depth"]]
                ordered, pairs = collapse_near_duplicates(
                    ordered, near_duplicate_pairs(conn, head, k["dedup"]), k["dedup_depth"])
    ms = (time.perf_counter() - t0) * 1000
    fts_by = {str(r["id"]): r for r in fts}
    knn_by = {str(r["id"]): r for r in knn}
    ent_by = {str(r["id"]): r for r in ent}
    rows = []
    for i in ordered:
        f, n, e = fts_by.get(i), knn_by.get(i), ent_by.get(i)
        rows.append({"id": i, "rrf": fused[i], "fts_r": f["r"] if f else None,
                     "knn_r": n["r"] if n else None, "ent_r": e["r"] if e else None,
                     "similarity": n["similarity"] if n else None,
                     "note_sim": n["note_sim"] if n else None,
                     "chunk_sim": n["chunk_sim"] if n else None,
                     "score": f["score"] if f else 0.0})
    return {"ids": ordered[:limit], "full": ordered, "rows": rows[:limit], "rows_full": rows,
            "fts": [{**r, "id": str(r["id"])} for r in fts],
            "knn": [{**r, "id": str(r["id"])} for r in knn],
            "ent": [{**r, "id": str(r["id"])} for r in ent], "ents_matched": ents,
            "fts_q": q_fts, "or_fired": fired, "pool": pool, "latency_ms": ms,
            "path": "replica", "limit": limit, "dedup_pairs": pairs,
            "knobs": {kk: v for kk, v in k.items() if not kk.startswith("_")}}


# ============================================================================ facade
_KB = None
_EMB = None


def kb():
    """The shared read-only KnowledgeBase; assert_readonly() runs before it is handed out,
    so library callers of shadow_search() get the proof too, not only the CLIs."""
    global _KB
    if _KB is None:
        k = make_kb()
        try:
            assert_readonly(k)
        except BaseException:
            with contextlib.suppress(Exception):
                k._pool.close()
            raise
        _KB = k
    return _KB


def close() -> None:
    """Close the shared pool (idempotent)."""
    global _KB
    if _KB is not None:
        with contextlib.suppress(Exception):
            _KB._pool.close()
        _KB = None


def embedder() -> Embedder:
    global _EMB
    if _EMB is None:
        _EMB = Embedder()
    return _EMB


def shadow_search(query: str, overrides: dict | None = None, *, path: str | None = None,
                  embedding=None, **args) -> dict:
    """See the module docstring. `path` forces 'prod' or 'replica'."""
    overrides = dict(overrides or {})
    prefix = overrides.pop("query_prefix", KNOB_DEFAULTS["query_prefix"])
    vec = embedding if embedding is not None else embedder()(query, prefix)
    use = path or ("prod" if set(overrides) <= PROD_CONSTANT_KNOBS else "replica")
    if use == "prod":
        return prod_search(kb(), query, args, vec, **overrides)
    return replica_search(kb(), query, args, vec, **overrides)


# ============================================================================ gold / data
def load_jsonl(path: Path) -> list:
    if not path.exists():
        raise SystemExit(f"gold set not found: {path}")
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def load_authored_gold() -> list:
    return load_jsonl(eval_data_dir() / "retrieval" / "golden.jsonl")


def authored_search_items(items: list) -> list:
    """`(item, search_args, must_ids)` for every item whose plan is a single search_memories
    step with must gold -- run.py's semantics: the step's query, or the item's `request`
    when the step has none; limit = max(k, 20) (the bench retrieves 50 and slices, which
    gives the same fused order for every k <= 50)."""
    out = []
    for it in items:
        steps = [s for s in it.get("tool_plan") or [] if s.get("tool") == "search_memories"]
        must = [m["id"] for m in it["expected"].get("memories", []) if m.get("must", True)]
        if len(steps) != 1 or not must:
            continue
        a = dict(steps[0].get("args") or {})
        a.setdefault("query", it["request"])
        out.append((it, a, must))
    return out


def load_traffic_gold(tier: str = "adjudicated", skipped: list | None = None) -> list:
    """`traffic_gold.jsonl` (schema: id, trace_id, at, tool, query, args, tier,
    expected.memories[{id, path, title, grade, group, note}], excluded[], provenance{}).
    `tier` filters on the item's tier; "all" keeps every item.

    Only `tool == "search_memories"` items are returned: the replica models that tool
    alone, so items traced from other tools (e.g. search_graph) are skipped in EVERY tier.
    Pass a list as `skipped` to receive the ids of skipped items of the requested tier, so
    callers can report them instead of silently scoring fewer items than the file holds."""
    items = load_jsonl(eval_data_dir() / "retrieval" / "traffic_gold.jsonl")
    out = []
    for it in items:
        for key in ("id", "trace_id", "at", "query", "args", "tier", "expected"):
            if key not in it:
                raise SystemExit(f"traffic gold item {it.get('id')!r} lacks {key!r}")
        if tier != "all" and it["tier"] != tier:
            continue
        if it.get("tool", "search_memories") != "search_memories":
            if skipped is not None:
                skipped.append(it["id"])
            continue
        out.append(it)
    return out


def traffic_groups(item: dict, admissible: dict | None = None) -> tuple:
    """Grade-2 groups, all-grade gains and member->group map for one traffic item.
    `admissible` maps member id -> bool (as-of / existence); members failing it are dropped
    from every denominator. Returns (g2_groups, gains_by_group, member_group, g1_groups).

    A `group` in the traffic gold is a set the annotators judged INTERCHANGEABLE -- mirror
    copies of one document, but in some items also a family (a design spec with its
    implementation plans, templated sibling emails). A hit on any member credits the group
    once, so family groups score higher recall than per-document gold would."""
    members, gains, member_group = {}, {}, {}
    for m in item["expected"].get("memories", []):
        if admissible is not None and not admissible.get(m["id"], False):
            continue
        g = m.get("group") or m["id"]
        member_group[m["id"]] = g
        gains[g] = max(gains.get(g, 0), int(m.get("grade", 2)))
        members.setdefault(g, set()).add(m["id"])
    # a group is graded by its best member, and EVERY member counts as a hit for it
    g2 = {g: mem for g, mem in members.items() if gains[g] >= 2}
    g1 = {g: mem for g, mem in members.items() if gains[g] < 2}
    return g2, gains, member_group, g1


def node_meta(kb, ids: list) -> dict:
    """Existence / ingestion time / flags for a set of ids (one read)."""
    if not ids:
        return {}
    with kb._pool.connection() as c:
        return {r["id"]: dict(r) for r in c.execute(
            "SELECT id::text AS id, ingested_at, dead_at IS NOT NULL AS dead, length(content) AS clen, "
            "access_count FROM nodes WHERE id = ANY(%(ids)s::uuid[])", {"ids": list(ids)}).fetchall()}


def corpus_flags(kb) -> tuple:
    """(hub ids, short ids) over notes, for the shares the bench reports."""
    with kb._pool.connection() as c:
        hubs = {r["id"] for r in c.execute(
            "SELECT id::text AS id FROM nodes WHERE type = 'note' AND access_count >= %(h)s",
            {"h": HUB_ACCESS_COUNT}).fetchall()}
        short = {r["id"] for r in c.execute(
            "SELECT id::text AS id FROM nodes WHERE type = 'note' AND length(content) < %(s)s",
            {"s": SHORT_NOTE_CHARS}).fetchall()}
    return hubs, short


def recent_traces(kb, *, since_days: int = 30, limit: int = 60, tool: str = "search_memories") -> list:
    """Real-traffic calls (caller IS NULL) with a usable result list, newest first."""
    with kb._pool.connection() as c:
        rows = c.execute(
            "SELECT id, at, args, results, trace, embedder_ok, error FROM mcp_trace "
            "WHERE tool = %(tool)s AND caller IS NULL AND results IS NOT NULL AND error IS NULL "
            "AND embedder_ok AND at >= now() - make_interval(days => %(d)s) "
            "ORDER BY at DESC LIMIT %(n)s", {"tool": tool, "d": since_days, "n": limit}).fetchall()
    return [dict(r) for r in rows]


def served_prefix(t: dict) -> str:
    """The instruction a recorded call's query vector was embedded with. KB_MCP writes
    trace.query_vector since the instruction shipped; a trace without it was embedded raw. A marker
    this harness does not know is refused rather than replayed with the wrong vector."""
    marker = (t.get("trace") or {}).get("query_vector")
    if marker is None:
        return BARE
    if marker == QUERY_INSTRUCTION_VERSION:
        return QUERY_INSTRUCTION
    raise CannotRun(f"trace {t.get('id')} was served with query vector {marker!r}, which "
                    "this harness cannot reproduce")
