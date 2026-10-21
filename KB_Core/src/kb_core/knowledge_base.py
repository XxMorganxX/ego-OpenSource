"""The reusable core service — all read/write logic lives here."""

import contextlib
import dataclasses
import hashlib
import math
import re
import threading
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from kb_core import db_browse
from kb_core import library_export
from kb_core import skills
from kb_core.doc_kind import DEFAULT_DOC_KIND_SUPPORT
from kb_core.domain import Edge, Entity, Node, NodePatch, NodeRecord, SearchResult
from kb_core.entities import (
    BELONGS_TO, ENTITY_CLASS, ENTITY_INSTANCE, ENTITY_ROOT, ENTITY_SOURCE, ENTITY_TYPE,
    KINDS, MENTIONS, ROLES, STATUS_ACTIVE, STATUS_BLOCKED, STATUS_INACTIVE, UNTYPED_CATEGORY,
    canonical_key, clean_class_description, entity_external_id,
    instance_external_id, instance_name_issue, instance_parent_path, is_valid_instance_name,
    normalize_entity_name,
)
from kb_core.aging import age_params, age_sql
from kb_core.identity import content_hash, node_id
from kb_core.importance import (
    NodeStats, ModifierRegistry, default_modifier_registry, compute_importance,
    effective_importance,
)
from kb_core.policy import Policy, Projects, load_policy
from kb_core.paths import ancestor_paths, humanize_path, join_path, leaf, parent_of, sanitize_slug
from kb_core.prompts import PromptResolver, prompts_base_dir
from kb_core.registry import TypeRegistry
from kb_core.retrieval_heat import retrieval_heatmap as _retrieval_heatmap
from kb_core.validation import sanitize_text, validate, ValidationError


class PathConflict(ValueError):
    """A write would put a node where a *different* node already lives.

    The store keeps one node per location (UNIQUE(parent_id, slug); UNIQUE(path) for roots, whose
    parent_id is NULL and therefore distinct). A create or move whose target slot is already held
    by another id raises this — instead of leaking psycopg's UniqueViolation — so callers can show
    an actionable message (update the existing node, or pick another slug) rather than a raw error.
    """


class ConsolidationBusy(RuntimeError):
    """Raised when a consolidation pass is already running (advisory lock held)."""


# Both location constraints mean "a node already lives here"; which one fires depends on whether
# the colliding node is nested (parent_id+slug) or at the root (path). Anything else is a real bug.
_LOCATION_CONSTRAINTS = {"nodes_parent_id_slug_key", "nodes_path_key"}


@contextmanager
def _as_path_conflict(path: str):
    """Translate a location-uniqueness violation into PathConflict; re-raise every other error."""
    try:
        yield
    except psycopg.errors.UniqueViolation as exc:
        if exc.diag.constraint_name in _LOCATION_CONSTRAINTS:
            raise PathConflict(f"a node already exists at {path!r}") from exc
        raise


# How many `active` concepts are offered to the LLM (and shown by default) at once. The
# active set is the top-N concepts ranked by how many memories belong to them, so concepts
# compete for a bounded number of slots: a real concept with many members keeps its slot,
# while a rarely-used one (a recurring hallucination that still cleared the threshold) ranks
# below the cap and falls out of the offered set. Raised to 60 (well above realistic active
# concept counts) so activation is pure absolute vote-floor: dominance only engages when
# far-exceeding the cap. Tunable via KB_MAX_ACTIVE_CLASSES.
DEFAULT_MAX_ACTIVE_CLASSES = 60
# How many concepts to *show the model* during the discover/classify passes. Larger than
# the active cap on purpose: it also surfaces the strongest not-yet-active candidates, so the
# model reuses an emerging name (e.g. "projects") instead of minting a near-duplicate
# ("project") that would only diverge until one side happens to activate.
DEFAULT_PROMPT_CLASSES = 80

# Vote-driven class activation (see `reconcile_classes`). A class is activated when it has at
# least DEFAULT_CLASS_VOTE_FLOOR distinct memory votes AND polls at least `p`× the leading
# class's votes AND ranks within the cap. `p` is the *dominance ratio*: it decays from HI (few
# classes — be strict, only near-leaders qualify) to LO (a diverse taxonomy — be lenient) as
# the total class count approaches DEFAULT_DOMINANCE_SPAN. Deactivation uses p − MARGIN, so a
# class near the boundary doesn't flap active/inactive across sweeps (hysteresis).
DEFAULT_CLASS_VOTE_FLOOR = 3
DEFAULT_DOMINANCE_HI = 0.6
DEFAULT_DOMINANCE_LO = 0.4
DEFAULT_DOMINANCE_SPAN = 40
DEFAULT_DOMINANCE_MARGIN = 0.1

# A candidate group whose best extraction confidence sits in the rubric's bottom band (0.3 —
# "not a durable anchor", see prompts/entities.system.md) may never CREATE an entity, however
# often it recurs: the model has said the name's meaning is exhausted by its sentence or its
# implementation. Mentions of an ALREADY-established entity still link regardless (that leg of
# `promotable_entities` is untouched) — this gate only withholds first-time creation.
#
# The cutoff sits BETWEEN the 0.3 and 0.6 bands rather than on 0.3 because `confidence` is
# stored as `real`: 0.3 round-trips as float4 0.30000001, so a literal `> 0.3` against a float8
# parameter is TRUE for stored 0.3 rows and the gate would silently never fire. (The same
# rounding is why `WHERE confidence = 0.6` matches nothing.) Comparing against a midpoint is
# exact for every value the schema allows.
TRUSTED_CONFIDENCE_CUTOFF = 0.45


# One filter block shared by every search arm and every read primitive, so a
# facet/time scope can never let a vector match leak past it. Static template,
# named placeholders only -- this is why the hot path has no SQL-injection surface.
SEARCH_FILTERS = """
                  AND (%(under)s::ltree IS NULL OR path <@ %(under)s::ltree)
                  AND (%(source)s::text IS NULL OR source = %(source)s::text)
                  AND (%(type)s::text IS NULL OR type = %(type)s::text)
                  AND (%(type)s::text IS NOT NULL OR NOT (type = ANY(%(nostub)s::text[])))
                  AND (%(tags)s::text[] IS NULL OR tags @> %(tags)s::text[])
                  AND (%(kind)s::text IS NULL OR doc_kind = %(kind)s::text)
                  AND (%(inc_dead)s::bool OR dead_at IS NULL)
                  AND (%(since)s::timestamptz IS NULL
                       OR COALESCE(event_at, source_created_at) >= %(since)s)
                  AND (%(until)s::timestamptz IS NULL
                       OR COALESCE(event_at, source_created_at) < %(until)s)"""

# Project key of a path: the subtree opened by
# the policy's `folder_label` at depth 0..max_depth, up to and including the next label —
# `Obsidian.Projects.my_repo.docs.x` -> `Obsidian.Projects.my_repo`. NULL for everything
# else (the label's own folder, a deeper label, entities) and for every path when the
# policy has no [projects] section. {path} is an ltree column expression; the label and
# depth ride as %(proj_label)s / %(proj_max_depth)s (see project_params).
_PROJECT_KEY_CASE = """(CASE
    WHEN index({path}, %(proj_label)s::ltree) BETWEEN 0 AND %(proj_max_depth)s
     AND nlevel({path}) >= index({path}, %(proj_label)s::ltree) + 2
    THEN subpath({path}, 0, index({path}, %(proj_label)s::ltree) + 2)
    ELSE NULL END)"""


def project_key_sql(path: str, projects: "Projects | None") -> str:
    return "NULL::ltree" if projects is None else _PROJECT_KEY_CASE.format(path=path)


def project_params(projects: "Projects | None") -> dict:
    """Query parameters project_key_sql needs — {} when there is no [projects] section."""
    if projects is None:
        return {}
    return {"proj_label": projects.folder_label, "proj_max_depth": projects.max_depth}


# Whole days since a project last changed, per project key. Activity is the newest
# event_at or hash-gated node_created/node_updated ingest event across the project's
# NOTES (concept_priority's "activity event" — never bare node timestamps). Whole days
# so the recompute's stored value is bit-identical across ticks within a UTC day.
_PROJECT_ACTIVITY_TEMPLATE = """
    WITH proj_notes AS (
        SELECT n.id, n.event_at, {key} AS project
        FROM nodes n
        WHERE n.type = 'note'
    )
    SELECT p.project::text AS project,
           CASE WHEN GREATEST(max(p.event_at), max(il.at)) IS NULL THEN NULL
                ELSE floor(GREATEST(0, EXTRACT(EPOCH FROM (now() - GREATEST(
                         max(p.event_at), max(il.at))))) / 86400)::int
           END AS dormant_days
    FROM proj_notes p
    LEFT JOIN ingest_log il
           ON il.node_id = p.id AND il.event IN ('node_created', 'node_updated')
    WHERE p.project IS NOT NULL
      AND (%(keys)s::text[] IS NULL OR p.project::text = ANY(%(keys)s::text[]))
    GROUP BY p.project"""


def project_activity_sql(projects: "Projects | None") -> str:
    return _PROJECT_ACTIVITY_TEMPLATE.format(key=project_key_sql("n.path", projects))

# Stuck ingest records: node_ids that ever failed to land, narrowed to those whose LATEST
# ingest_log event is still 'ingest_failed'. One query serves both the per-source dedup set
# (open_ingest_failures) and the dashboard banner (stuck_ingest_records). Only node_ids that
# ever failed are examined; measured at a few ms on an ingest log of tens of thousands of
# rows via a seq scan on the first CTE, so no index is warranted. `%(source)s` is NULL for "all
# sources"; `%(limit)s` is NULL for "no limit" (Postgres reads LIMIT NULL as LIMIT ALL).
_STUCK_INGEST_SQL = """
    WITH failed_nodes AS (
        SELECT DISTINCT node_id FROM ingest_log
        WHERE event = 'ingest_failed'
          AND (%(source)s::text IS NULL OR source = %(source)s)
    ), latest AS (
        SELECT DISTINCT ON (l.node_id)
               l.id, l.at, l.event, l.source, l.external_id, l.node_id, l.path, l.detail
        FROM ingest_log l JOIN failed_nodes f ON f.node_id = l.node_id
        WHERE (%(source)s::text IS NULL OR l.source = %(source)s)
        ORDER BY l.node_id, l.at DESC, l.id DESC
    )
    SELECT source, external_id, node_id, path, detail->>'error' AS error, at AS since
    FROM latest WHERE event = 'ingest_failed'
    ORDER BY at DESC, id DESC LIMIT %(limit)s
"""

# importance x retrieval-recency multiplier -- the "search priority" factor. Both
# arms order by it (FTS on ts_rank, kNN on cosine), so an important, recently
# retrieved memory outranks an equally similar forgotten one.
_IMP_FACTOR = """(
                         1.0 + (importance - 1.0) *
                           (%(floor)s + (1 - %(floor)s) *
                            exp(-%(ln2)s * EXTRACT(EPOCH FROM (now() - coalesce(last_accessed_at, ingested_at))) / %(hl)s))
                       )"""


def search_confidence(results: "list[SearchResult]", *, scoped: bool = False) -> dict:
    """Absolute quality of a result set — the thing RRF rank cannot express.

    Rank is a position artifact (1/(K+r)): a junk query's top hit and a perfect
    match's top hit score alike, so callers had no way to tell "here is the answer"
    from "here is the closest thing in the corpus, which is nothing". The best cosine
    in the set is comparable across queries, so it becomes the headline.

    Advisory, never enforced — retrieval still returns what it found; see the
    CONFIDENCE_* calibration note for why this is a label and not a filter.

    Levels:
      none    — nothing matched. Say the KB does not know; do NOT summarize the rows.
      weak    — related at best; hedge rather than assert.
      strong  — a genuine semantic match.
      keyword — literal term hits, no vector arm. Self-evidently relevant, so this is
                NOT a low-confidence verdict.
      scoped  — a hard facet (kind/under/tags/time) selected these rows, so semantic
                similarity is the wrong yardstick: "all my troubleshooting logs" is
                answered correctly by the filter even when nothing is semantically
                near the phrasing.
    """
    sims = [r.similarity for r in results if r.similarity is not None]
    if not results:
        return {"level": "none", "best_similarity": None, "n": 0}
    best = max(sims) if sims else None
    if scoped:
        level = "scoped"
    elif not sims:
        level = "keyword"
    else:
        level = ("strong" if best >= CONFIDENCE_STRONG
                 else "weak" if best >= CONFIDENCE_WEAK else "none")
    return {"level": level,
            "best_similarity": round(best, 4) if best is not None else None,
            "n": len(results)}


def dominance_threshold(n_classes: int, *, hi: float, lo: float, span: int) -> float:
    """The activation dominance ratio for a taxonomy of `n_classes` total (non-blocked) classes:
    linearly decays from `hi` (few classes → strict) to `lo` (≥ `span` classes → lenient), so a
    class must out-poll the leader by a smaller margin as the taxonomy diversifies. Clamped to
    [lo, hi]."""
    if span <= 0:
        return lo
    frac = min(1.0, max(0.0, n_classes / span))
    return hi - (hi - lo) * frac

_COLUMNS = (
    "id, source, external_id, type, parent_id, slug, path::text AS path, "
    "title, content, summary, uri, tags, source_created_at, source_updated_at, "
    "event_at, ingested_at, updated_at, content_hash, "
    "importance, importance_modifiers, access_count, last_accessed_at, "
    "enrichment_status, enriched_at, enriched_hash, last_analyzed_at, "
    "entity_eligible, entity_skip_reason, dead_at, dead_reason, "
    "analysis_excluded, analysis_excluded_at, embedded_hash, doc_kind"
)

# Same row shape minus the note body: content comes back '' and summary NULL. For metadata
# listings (the dashboard tree) -- selecting `content` drags every body out of TOAST (15 MB
# to build a 1 MB tree; 63 ms vs 10 ms measured) only for the caller to drop it.
_COLUMNS_NO_BODY = _COLUMNS.replace("content, summary, ", "'' AS content, NULL::text AS summary, ")
assert _COLUMNS_NO_BODY != _COLUMNS


def _row_to_node(row: dict) -> Node:
    return Node(
        id=row["id"], source=row["source"], external_id=row["external_id"],
        type=row["type"], parent_id=row["parent_id"], slug=row["slug"],
        path=row["path"], content=row["content"], content_hash=row["content_hash"],
        title=row["title"], summary=row["summary"], uri=row["uri"], tags=row["tags"],
        source_created_at=row["source_created_at"],
        source_updated_at=row["source_updated_at"],
        event_at=row["event_at"],
        ingested_at=row["ingested_at"], updated_at=row["updated_at"],
        importance=row["importance"],
        importance_modifiers=row["importance_modifiers"],
        access_count=row["access_count"],
        last_accessed_at=row["last_accessed_at"],
        enrichment_status=row["enrichment_status"],
        enriched_at=row["enriched_at"],
        enriched_hash=row["enriched_hash"],
        last_analyzed_at=row["last_analyzed_at"],
        entity_eligible=row["entity_eligible"],
        entity_skip_reason=row["entity_skip_reason"],
        dead_at=row["dead_at"],
        dead_reason=row["dead_reason"],
        analysis_excluded=row["analysis_excluded"],
        analysis_excluded_at=row["analysis_excluded_at"],
        embedded_hash=row["embedded_hash"],
        doc_kind=row["doc_kind"],
    )


@dataclass(frozen=True)
class DueReasons:
    """Why a node came back from due_for_reanalysis. Each flag maps to exactly the one
    recrawl pass it can change the answer of — see ReanalysisWorker._passes_for."""
    due_class: bool
    due_kind: bool
    due_gate: bool


# Field/row separators for vocabulary digests. Chosen as ASCII unit/record separators so
# they cannot occur in a class name, a description, or a doc-kind label.
_VOCAB_FIELD_SEP = "\x1f"
_VOCAB_ROW_SEP = "\x1e"


def _vocab_digest(rows: "list[tuple[str, str]]") -> str:
    """sha256 over a vocabulary as a SORTED SET of (name, detail) pairs.

    Sorting here is load-bearing, not tidiness: the prompt builders these vocabularies
    come from rank by support count, so a digest taken in their order would change every
    time a count drifted while the vocabulary itself sat still.
    """
    payload = _VOCAB_ROW_SEP.join(
        f"{name}{_VOCAB_FIELD_SEP}{detail}" for name, detail in sorted(rows))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# How often the staleness gate re-checks a note whose vocabularies have not moved.
# entity_gate judges chronological staleness against TODAY, so unlike the other two
# recrawl passes it has no vocabulary to key on and needs a clock of its own.
REANALYZE_GATE_INTERVAL_DAYS = 30


# Hybrid-search fusion knobs. RRF_K is the standard reciprocal-rank-fusion damping
# (rank 1 in one arm scores 1/(K+1)); rank-based fusion means the two arms'
# incomparable scores (ts_rank vs cosine) never need calibrating. Each arm feeds at
# most HYBRID_ARM_POOL candidates into the fusion — both arms are exact scans today
# (no ANN index; fine at personal scale, HNSW is the scaling step).
RRF_K = 60
HYBRID_ARM_POOL = 50

# Chunk-leg overfetch, as a multiple of the arm pool. Two things spend this budget:
# the chunk leg yields one row per CHUNK (several per chunked note, and a strongly
# relevant note contributes several of the top ones), and its filters are applied
# AFTER the ANN limit, so a scoped query post-filters the probe rather than steering
# it. Both shrink the distinct-note yield.
#
# Measured against a private golden set, comparing the resulting kNN arm to the old
# exhaustive scan: fanout 8 left some queries differing, 16 made every top-20 SET
# identical (only deep ranks reorder), 32 was byte-identical. 16 it is.
#
# CAVEAT, do not over-trust that number: 16 x 50 = 800 chunks, which against a
# personal-scale table is very nearly an exhaustive fetch, so the setting
# is effectively UNVALIDATED for scale. Re-run the comparison when the corpus is
# large enough that pool*fanout is a small fraction of node_chunks; if filtered
# queries lose recall there, the structural fix is to make the filter reachable
# inside the ANN scan (a partial index, or denormalising the filter columns onto
# node_chunks) rather than to keep raising this.
ANN_CHUNK_FANOUT = 16

# Node types that are scaffolding, not memories: `entity` stub nodes and `folder`
# directory nodes. They carry a non-empty content_tsv, so before this they competed
# for top-k on short name-shaped queries (measured: a noticeable share of top-20 slots
# on name-shaped probes). search() excludes them unless the caller names a type
# explicitly — an explicit type=... always wins.
SEARCH_SCAFFOLDING_TYPES = ("entity", "folder")

# ANN breadth for concept_candidates' evidence-vote leg: how many of the query's
# nearest memories get to vote on which concept they belong to. MEASURED on a
# private golden set: the /log2(1+member_count) divider clearly lifts vote top-1 --
# do not drop it; the damped vote beats label cosine at top-3; and the union
# label[:3] u vote_damped[:2] beats either alone, close to the measured ceiling. 25
# is what those results were measured against -- re-measure before changing it.
CONCEPT_ENTRY_ANN_K = 25

# Absolute-confidence thresholds. RRF rank is a position artifact: it says where a row
# placed, never whether anything actually matched, so an off-corpus query returns a full
# page of near-centroid notes scoring like a correct answer's. Cosine IS comparable
# across queries, so the top-1 cosine supplies the missing signal.
#
# REPORTED, NEVER ENFORCED — a measured decision, not caution. Measured on a private
# golden set (golden queries vs nonsense / stopword-only off-corpus probes):
#   1. Do NOT filter rows on similarity. Per-row gold and junk distributions overlap —
#      gold's p10 sits below junk's median — so a cut deep enough to remove junk removes
#      real hits. A 0.50 row floor was tried and cost recall outright.
#   2. Do NOT use the margin/peakedness of the distribution (top1 minus median-of-top-20).
#      Junk's max margin exceeded gold's median margin. Tested and rejected.
#   3. top1 is the usable signal, at the SET level only.
#
# CURRENT CALIBRATION -- valid for kb_core.embedding.QUERY_INSTRUCTION_VERSION
# "instructed-v1" ONLY. Queries are embedded with the Qwen3 query instruction
# (kb_core.embedding.QUERY_INSTRUCTION), which lowers top1 cosines relative to bare query
# vectors, so changing the instruction or the embedding model invalidates the pair --
# re-derive, never nudge. The rule, measured write-free by
# evals/retrieval/shadow_confidence.py in BOTH modes (A = search(), B = cluster-crawl
# judge="none"):
#   WEAK   = worst junk top1 across both modes + 0.003 (ceil to 3 dp)
#   STRONG = 0.904 x the lower of the two modes' gold top1 medians
#   junk = evals/retrieval/off_corpus.py's permanent probes; gold = authored search-first
#   items + adjudicated traffic items. With these values every junk probe labels 'none'
#   and a small share of genuine queries (bare person names, keyword bags) is under-called
#   'none'; the 'scoped' label covers faceted queries whose rows a hard filter selected.
#
# This is a static pair, and a static pair rots as the corpus grows. A per-query relative
# signal was tried in a pre-registered, write-free bake-off (evals/retrieval/
# shadow_signals.py; pure functions in kb_core/confidence_signal.py, SHIPPED_RULE = None):
# a background null drawn from a random reservoir, and within-result-set shape statistics
# (gap12, gap_med, z_in, sigma, bg_z, bg_gap_max, bg_pct). NO candidate qualified: every
# one either let junk leak out of 'none' or pushed many gold queries into it, and absolute
# top-1 cosine separated gold from junk better than every relative signal.
# Background-call latency was not the blocker. The harness excludes each query's
# own result ids from the reservoir (reservoir_minus_hits); without that, a top hit could
# be compared against itself. Re-test when off_corpus.py FAILs (any junk probe not
# 'none') or the corpus grows substantially: re-run shadow_signals.py --collect /
# --evaluate and re-derive the pair with shadow_confidence.py.
CONFIDENCE_STRONG = 0.556  # 0.904 x min gold top1 median, both modes
CONFIDENCE_WEAK = 0.512    # worst junk top1 both modes + 0.003, ceil

# Goal-ranking knobs (concept priority).
# "Current" is a membership test — any activity event inside the window makes a concept
# a current goal; it is NOT a frequency contest. Priority orders the always-complete catalog
# (importance × data-recency × retrieval-recency); the floored decays mean a dormant
# project sinks toward its importance-only weight but never disappears. The values live
# in the policy's `[concepts]` section (kb_core.policy.Concepts), not here.

# Entity-resolution ladder knobs. FUZZY_NAME_MIN_SIM: pg_trgm floor for the
# typo rung (below ~0.25 short names start matching everything). ENTITY_DESC_MIN_SIM:
# cosine floor for the description rung (qwen3 puts unrelated text ~0.2-0.3).
# MEMORY_VOTE_POOL: how many nearest memories vote in the evidence rung.
FUZZY_NAME_MIN_SIM = 0.25
ENTITY_DESC_MIN_SIM = 0.3
MEMORY_VOTE_POOL = 25

# FTS OR-fallback fires only for short keyword-bag queries; longer natural-language
# requests OR-match half the corpus and flood the RRF fusion.
OR_FALLBACK_MAX_WORDS = 8
# Keyword-only mode (no query embedding: no embedder configured, or it is down) has no
# vector arm to answer long natural-language questions and no fusion to flood, so the
# AND-miss fallback is allowed far longer queries there. Without it a plain question
# ("what caused the X bug, and how was it fixed?") returns nothing at all. The cap only
# bounds pathological pastes.
OR_FALLBACK_MAX_WORDS_KEYWORD_ONLY = 32


def or_fallback_eligible(query: str, *, embedded: bool) -> bool:
    """Whether an AND-miss on `query` may be retried as OR-joined terms.

    The length gate of the FTS OR-fallback, shared by KnowledgeBase.search() and
    KB_Retrieval's HybridStrategy so the two stay identical by construction. `embedded`
    is whether the caller supplied a query embedding: with one, the hybrid gate is
    exactly OR_FALLBACK_MAX_WORDS as before; without one (keyword-only), the cap is
    OR_FALLBACK_MAX_WORDS_KEYWORD_ONLY.
    """
    cap = OR_FALLBACK_MAX_WORDS if embedded else OR_FALLBACK_MAX_WORDS_KEYWORD_ONLY
    return 1 < len(query.split()) <= cap

# Merge-candidate name arm: minimum trigram similarity across canonical names AND
# aliases for a lexical duplicate suggestion (word-boundary containment qualifies
# regardless). Below ~0.45 short names start pairing with everything.
NAME_MERGE_MIN_SIM = 0.45

# How long an unclaimed consolidation request may sit before the dashboard calls the
# daemon unresponsive. Derived from the ~1s NOTIFY wake, not an operator knob — a
# request waiting on an in-flight pass is reported 'queued', never 'stalled'.
STALE_REQUEST_S = 90.0

# Entity columns are aliased (e_*) so a node+entities join row maps cleanly to both.
_ENTITY_COLUMNS = (
    "e.entity_type AS e_type, e.canonical_name AS e_name, "
    "e.aliases AS e_aliases, e.description AS e_description, e.status AS e_status, "
    "e.pinned AS e_pinned"
)


def _row_to_entity(row: dict) -> Entity:
    return Entity(node=_row_to_node(row), entity_type=row["e_type"],
                  canonical_name=row["e_name"], aliases=row["e_aliases"],
                  description=row["e_description"], status=row["e_status"],
                  pinned=row["e_pinned"])


# Bytes are measured; tokens are estimated from them. Deriving at READ time rather
# than storing a second column means (a) the two can never drift apart and (b)
# recalibrating this constant re-derives every historical row -- including rows
# already written -- with a one-line edit: no UPDATE, no migration, no rewrite.
#
# 3.5 is a STATED DEFAULT, NOT A MEASUREMENT. indent=2 JSON mixes UUIDs (~2.2
# bytes/token -- they tokenize densely), structural punctuation and keys (~3), and
# prose memory content (~4); the folklore chars/4 rule is calibrated on prose and
# undercounts this payload shape. Calibrating against Anthropic's count_tokens is a
# possible follow-up and costs exactly this one line.
_BYTES_PER_TOKEN = 3.5


def _est_tokens(nbytes: "float | None") -> "int | None":
    """Estimated tokens for an exact byte count, rounded once here so the API, the
    dashboard tiles, and any SQL-side consumer all report the same integer.

    None -- never 0 -- when unmeasured: 0 is indistinguishable from a call that
    genuinely returned an empty payload, and it would drag every median and sum
    toward zero. Accepts float because percentile_cont and byte-per-row division
    both produce doubles."""
    return None if nbytes is None else round(nbytes / _BYTES_PER_TOKEN)


# Calls in one MCP session at most this many seconds apart -- from the previous
# call's END to the next call's START -- belong to one turn (one user request).
# Chosen from session-gap distributions: short gaps are follow-ups, multi-minute gaps
# are topic changes, and the ambiguous band sits in between; tune with
# `kb-turns-rebuild`. Like _BYTES_PER_TOKEN this is ONE named number: the write path applies it
# going forward, `kb-turns-rebuild` re-derives history with it, and the dashboard
# discloses it (trace_summary's turn_gap_s).
TURN_GAP_S = 90

_TURN_LOCK_NAMESPACE = 48  # migration that introduced turns; keeps the key space separate from the single-key locks


_REPLAYABLE_TOOLS = ("search_memories", "search_graph")


# One turn per row, computed over mcp_trace grouped by turn_id and joined to the
# opener (id = turn_id) for session/caller/first_query. Shared by trace_turns (list)
# and trace_turn (one id): the same shape from one text. `outcome` precedence:
# opened > returned > error > empty.
_TURNS_SQL = """
    WITH calls AS (
        SELECT t.turn_id, t.id, t.tool, t.at, t.latency_ms, t.result_count,
               t.result_bytes, t.error, t.confidence_level
        FROM mcp_trace t
        JOIN mcp_trace o ON o.id = t.turn_id
        WHERE (%(include_evals)s OR o.caller IS NULL)
          AND (%(turn_id)s::bigint IS NULL OR t.turn_id = %(turn_id)s)
    ), agg AS (
        SELECT turn_id,
               min(at - latency_ms * interval '1 millisecond') AS started_at,
               max(at) AS ended_at,
               count(*) AS calls,
               sum(result_bytes) AS result_bytes,
               count(*) FILTER (WHERE error IS NOT NULL) AS errors,
               bool_or(result_count > 0) AS any_results,
               bool_and(error IS NOT NULL) AS all_errors,
               bool_or(tool = %(tool)s) AS has_tool,
               bool_or(confidence_level = %(confidence)s) AS has_conf
        FROM calls GROUP BY turn_id
    ), tools AS (
        SELECT turn_id, jsonb_object_agg(tool, n) AS tools
        FROM (SELECT turn_id, tool, count(*) AS n FROM calls GROUP BY turn_id, tool) x
        GROUP BY turn_id
    )
    SELECT a.turn_id AS id, o.session_id, o.caller,
           a.started_at, a.ended_at, a.calls, tl.tools,
           coalesce(o.query, o.target_id::text) AS first_query,
           a.result_bytes, a.errors,
           CASE WHEN EXISTS (
                    SELECT 1 FROM mcp_trace f
                    JOIN mcp_trace s ON s.turn_id = f.turn_id
                                    AND s.id <> f.id AND s.at <= f.at
                    WHERE f.turn_id = a.turn_id
                      AND f.tool IN ('get_memory', 'expand')
                      AND f.target_id IS NOT NULL
                      AND s.results @> jsonb_build_array(
                              jsonb_build_object('id', f.target_id::text)))
                THEN 'opened'
                WHEN a.any_results THEN 'returned'
                WHEN a.all_errors THEN 'error'
                ELSE 'empty' END AS outcome,
           j.verdict AS judgment
    FROM agg a
    JOIN mcp_trace o ON o.id = a.turn_id
    JOIN tools tl ON tl.turn_id = a.turn_id
    LEFT JOIN LATERAL (
        SELECT verdict FROM turn_judgment
        WHERE turn_id = a.turn_id
        ORDER BY at DESC, id DESC LIMIT 1
    ) j ON true
    WHERE (%(tool)s::text IS NULL OR a.has_tool)
      AND (%(confidence)s::text IS NULL OR a.has_conf)
      AND (%(judged)s::boolean IS NULL OR (j.verdict IS NOT NULL) = %(judged)s)
    ORDER BY a.ended_at DESC, a.turn_id DESC
    LIMIT %(limit)s
"""


def _synthesize_replay(row: dict) -> dict:
    """A v1-shaped replay from what a pre-capture mcp_trace row holds.

    Absent keys mean NOT RECORDED -- never an empty list, which would claim the arm
    ran and found nothing. Final results become `flat.fused` (in order, with their
    similarity); a search_graph row whose trace carries `entry` gets a crawl with
    entry seeds and per-hop COUNTS only. A search_graph(strategy="hybrid") row has no
    `entry` in its trace, so no crawl is invented for it."""
    results = [r for r in (row["results"] or [])
               if r.get("id") and r.get("kind", "memory") == "memory"]
    fused = [{"id": r["id"],
              **({"sim": r["similarity"]} if r.get("similarity") is not None else {})}
             for r in results]
    out = {"v": 1, "limit": int((row["args"] or {}).get("limit") or 20),
           "flat": {"fused": fused}}
    trace = row["trace"] or {}
    if row["tool"] == "search_graph" and "entry" in trace:
        out["crawl"] = {
            "entry": trace.get("entry") or [],
            "hops": [{"hop": h.get("hop"), "by": h.get("by"), "note": h.get("note"),
                      "offered_count": h.get("offered", 0),
                      "admitted_count": h.get("admitted", 0)}
                     for h in trace.get("hops") or []],
            "stop_reason": trace.get("stop_reason")}
    return out


def _replay_memory_ids(replay: dict) -> "set[str]":
    """Every memory id a replay mentions -- what trace_replay must label."""
    flat = replay.get("flat") or {}
    ids = {r["id"] for k in ("fts", "knn", "fused") for r in flat.get(k) or []}
    crawl = replay.get("crawl") or {}
    ids |= {r["id"] for r in crawl.get("final") or []}
    ids |= {o["id"] for h in crawl.get("hops") or [] for o in h.get("offered") or []}
    for ids_list in (crawl.get("lists") or {}).values():
        ids |= set(ids_list)
    return ids


class KnowledgeBase:
    def __init__(self, pool: ConnectionPool, registry: TypeRegistry,
                 modifier_registry: ModifierRegistry | None = None,
                 policy: Policy | None = None) -> None:
        self._pool = pool
        self._registry = registry
        # The per-install policy. Every daemon/CLI/dashboard/MCP process
        # constructs one KnowledgeBase, so this is the one load per process; tests inject.
        self._policy = policy or load_policy()
        self._modifier_registry = modifier_registry or default_modifier_registry(self._policy)
        self._prompts = PromptResolver(self._policy, prompts_base_dir())
        self._prompt_cache: dict[str, str] = {}
        # None = not probed yet; see _hnsw_iterative_scan.
        self._iterative_scan: "bool | None" = None
        # Per-thread count of node rows _persist_node actually wrote (see node_writes).
        self._writes = threading.local()

    @property
    def policy(self) -> Policy:
        return self._policy

    def prompt(self, name: str) -> str:
        """A shipped prompt (or its overlay), markers expanded, cached for the process.
        The judges that are not enrichment passes (meaning_judge, concept_judge) read
        their system prompts here so the overlay applies to them too."""
        if name not in self._prompt_cache:
            self._prompt_cache[name] = self._prompts.load(name)
        return self._prompt_cache[name]

    def prompt_overlays(self) -> list[str]:
        return self._prompts.overlays()

    # ---- raw table browser (read-only DB inspection for the dashboard) ------
    # A 1:1 window onto Postgres for the dashboard's Database page. Every query runs in a
    # READ ONLY transaction (reset on commit, so the pooled connection isn't left read-only),
    # delegating the SELECT-only catalog/data reads to `db_browse`.
    def browse_tables(self) -> list[dict]:
        """List every non-system base table with column/estimated-row counts."""
        with self._pool.connection() as conn:
            conn.execute("SET TRANSACTION READ ONLY")
            return db_browse.list_tables(conn)

    def browse_table_page(
        self, schema: str, name: str, *, limit: int, offset: int
    ) -> dict | None:
        """A table's column metadata plus one page of its rows, or None if it doesn't exist."""
        with self._pool.connection() as conn:
            conn.execute("SET TRANSACTION READ ONLY")
            meta = db_browse.describe_table(conn, schema, name)
            if meta is None:
                return None
            page = db_browse.read_rows(conn, schema, name, limit=limit, offset=offset)
            return {**meta, **page}

    # ---- write -------------------------------------------------------------
    def node_writes(self) -> int:
        """How many node rows this THREAD has actually written through _persist_node
        (inserted, or changed past the no-op gate) since this KnowledgeBase was built.

        A monotonic counter, read before and after an upsert to tell a real write from
        a no-op re-sync: kb.upsert returns the node either way. Thread-local because the
        daemon's lanes share one KnowledgeBase (promotion writes entity nodes on another
        thread while a pull is running); only differences on one thread mean anything.
        """
        return getattr(self._writes, "n", 0)

    def upsert(self, record: NodeRecord) -> Node:
        validate(record, self._registry)
        nid = node_id(record.source, record.external_id)
        retracted = False
        with self._pool.connection() as conn, conn.transaction():
            prior = conn.execute(
                "SELECT path::text AS path FROM nodes WHERE id = %s", (nid,)).fetchone()
            node = self._persist_node(conn, record)
            # A path change on an existing note may cross an analysis_excluded boundary; a note is a
            # leaf, so reconciling its (single-node) subtree reconciles exactly this note.
            if prior is not None and node.type == "note" and prior["path"] != node.path:
                old_excluded = self._effective_excluded(conn, prior["path"])
                retracted = self._reconcile_exclusion_subtree(conn, node.path, old_excluded)
        if retracted:
            self.reconcile_instances()      # GC any instance left with zero support, post-commit
        return node

    def _persist_node(self, conn: psycopg.Connection, record: NodeRecord) -> Node:
        """Write one node within an OPEN transaction (validate() is the caller's job).

        Shared by upsert() and upsert_entity() so node persistence + lifecycle logging
        stay identical across writers.
        """
        # Postgres text cannot hold U+0000 (psycopg rejects the row before sending it);
        # swap it for the visible ␀ in every text column, BEFORE hashing, so a re-sync of
        # the same bytes hashes identically and stays a no-op (see validation.sanitize_text).
        record = dataclasses.replace(
            record, title=sanitize_text(record.title), content=sanitize_text(record.content),
            summary=sanitize_text(record.summary), tags=[sanitize_text(t) for t in record.tags])
        slug = sanitize_slug(record.slug)
        path = join_path(record.parent_path, slug)
        nid = node_id(record.source, record.external_id)
        chash = content_hash(record.title, record.content)
        parent_id = self._ensure_dirs(conn, record.parent_path)
        prior = conn.execute(
            "SELECT content_hash FROM nodes WHERE id = %s", (nid,)
        ).fetchone()
        # ON CONFLICT (id) makes a re-sync of the *same* node idempotent; a different id landing
        # on an already-occupied (parent_id, slug)/path is a genuine collision, surfaced as a
        # clean PathConflict instead of a raw UniqueViolation.
        with _as_path_conflict(path):
            row = conn.execute(
                f"""
                INSERT INTO nodes (id, source, external_id, type, parent_id, slug, path,
                                   title, content, summary, uri, tags,
                                   source_created_at, source_updated_at, event_at,
                                   content_hash, updated_at)
                VALUES (%(id)s, %(source)s, %(external_id)s, %(type)s, %(parent_id)s,
                        %(slug)s, %(path)s::ltree, %(title)s, %(content)s, %(summary)s,
                        %(uri)s, %(tags)s, %(sca)s, %(sua)s, %(eat)s, %(chash)s, now())
                ON CONFLICT (id) DO UPDATE SET
                    type = EXCLUDED.type, parent_id = EXCLUDED.parent_id,
                    slug = EXCLUDED.slug, path = EXCLUDED.path, title = EXCLUDED.title,
                    content = EXCLUDED.content, summary = EXCLUDED.summary,
                    uri = EXCLUDED.uri, tags = EXCLUDED.tags,
                    -- Source timestamps are hash-gated (same rule as the ingest_log
                    -- write below): a re-sync whose content is unchanged — e.g. a bulk
                    -- file operation that only touched mtimes — must not rewrite
                    -- authorship history. A bulk file operation can flatten a whole
                    -- vault's source_updated_at this way.
                    source_created_at = CASE
                        WHEN nodes.content_hash IS DISTINCT FROM EXCLUDED.content_hash
                        THEN EXCLUDED.source_created_at
                        ELSE nodes.source_created_at END,
                    source_updated_at = CASE
                        WHEN nodes.content_hash IS DISTINCT FROM EXCLUDED.content_hash
                        THEN EXCLUDED.source_updated_at
                        ELSE nodes.source_updated_at END,
                    -- event_at is deterministic from source content (frontmatter/filename,
                    -- never file timestamps), so unlike source_* it is NOT hash-gated:
                    -- recomputing it unconditionally is what backfills existing rows on
                    -- the next sync, and mirror semantics apply (dropping the frontmatter
                    -- date clears it).
                    event_at = EXCLUDED.event_at,
                    content_hash = EXCLUDED.content_hash, updated_at = now()
                -- No-op gate: a re-sync that changes nothing must not create a new
                -- row version. Pull sources re-upsert their whole corpus on a
                -- heartbeat (obsidian runs at period 0), and unconditional rewrites
                -- create a dead tuple plus an HNSW index insert per row — churn
                -- that can corrupt nodes_embedding_hnsw under autovacuum.
                -- content_hash stands in for (title, content); source_* stay out on
                -- purpose (they are hash-gated above — mtime-only touches must skip).
                WHERE (nodes.type, nodes.parent_id, nodes.slug, nodes.path,
                       nodes.summary, nodes.uri, nodes.tags, nodes.event_at,
                       nodes.content_hash)
                      IS DISTINCT FROM
                      (EXCLUDED.type, EXCLUDED.parent_id, EXCLUDED.slug, EXCLUDED.path,
                       EXCLUDED.summary, EXCLUDED.uri, EXCLUDED.tags, EXCLUDED.event_at,
                       EXCLUDED.content_hash)
                RETURNING {_COLUMNS}
                """,
                {
                    "id": nid, "source": record.source, "external_id": record.external_id,
                    "type": record.type, "parent_id": parent_id, "slug": slug, "path": path,
                    "title": record.title, "content": record.content, "summary": record.summary,
                    "uri": record.uri, "tags": record.tags,
                    "sca": record.source_created_at, "sua": record.source_updated_at,
                    "eat": record.event_at,
                    "chash": chash,
                },
            ).fetchone()
        if row is not None:
            self._writes.n = self.node_writes() + 1     # a real write (see node_writes)
        else:
            # The no-op gate skipped the write, so RETURNING produced nothing.
            # ON CONFLICT locked the conflicting row even without updating it,
            # so this read cannot race a concurrent delete.
            row = conn.execute(
                f"SELECT {_COLUMNS} FROM nodes WHERE id = %s", (nid,)).fetchone()
        # Observability: record the lifecycle event in the same transaction as the
        # write, so the log never drifts from what landed. A creation and a real
        # content change are logged; an idempotent re-sync (same content_hash) is
        # not — the log stays signal, not noise.
        if prior is None:
            event = "node_created"
        elif prior["content_hash"] != chash:
            event = "node_updated"
        else:
            event = None
        if event is not None:
            conn.execute(
                """INSERT INTO ingest_log
                       (event, source, external_id, node_id, path, content_hash)
                   VALUES (%(event)s, %(source)s, %(external_id)s, %(node_id)s,
                           %(path)s, %(chash)s)""",
                {"event": event, "source": record.source,
                 "external_id": record.external_id, "node_id": nid,
                 "path": path, "chash": chash},
            )
        return _row_to_node(row)

    def _ensure_dirs(self, conn: psycopg.Connection, parent_path: str) -> UUID | None:
        """mkdir -p: ensure every segment of parent_path exists; return the parent id.

        An existing node at a segment (of any type) is reused as the parent; only
        genuinely missing segments are created as 'folder' nodes. Concurrency-safe:
        `ON CONFLICT DO NOTHING` absorbs a race on any unique constraint (id, path, or
        (parent_id, slug)), and on a lost race we reuse whatever node now holds the path.
        """
        if not parent_path:
            return None
        parent_id: UUID | None = None
        for dir_path in [*ancestor_paths(parent_path), parent_path]:
            existing = conn.execute(
                "SELECT id FROM nodes WHERE path = %s::ltree", (dir_path,)
            ).fetchone()
            if existing:
                parent_id = existing["id"]
                continue
            label = leaf(dir_path)
            fid = node_id("tree", dir_path)
            inserted = conn.execute(
                """
                INSERT INTO nodes (id, source, external_id, type, parent_id, slug, path,
                                   content, content_hash)
                VALUES (%(id)s, 'tree', %(eid)s, 'folder', %(parent_id)s, %(slug)s,
                        %(path)s::ltree, %(content)s, %(chash)s)
                ON CONFLICT DO NOTHING
                RETURNING id
                """,
                {
                    "id": fid, "eid": dir_path, "parent_id": parent_id, "slug": label,
                    "path": dir_path, "content": label, "chash": content_hash(None, label),
                },
            ).fetchone()
            if inserted:
                parent_id = inserted["id"]
            else:
                # Lost the race: another writer created a node at this path. Reuse it.
                parent_id = conn.execute(
                    "SELECT id FROM nodes WHERE path = %s::ltree", (dir_path,)
                ).fetchone()["id"]
        return parent_id

    def reidentify(self, pairs: "list[tuple[UUID, str]]") -> int:
        """Loss-free re-key: give nodes a new external_id (hence a new deterministic id) in
        place, carrying EVERY FK-referencing row across with them — never a prune + recreate.

        `pairs` is (old_node_id, new_external_id). Used when a note gains a stronger identity
        (e.g. a freshly stamped frontmatter id) that supersedes its old path-based one.
        Idempotent: a pair whose recomputed id already equals the old id just refreshes
        external_id.

        Node ids are referenced by ON DELETE CASCADE / NO-ACTION foreign keys, so a plain
        `UPDATE nodes SET id=…` is rejected while children still point at the old id. We drop
        those FKs, re-key node + referrers, then re-add the FKs exactly as captured — all in one
        transaction, so any failure rolls back both the rows and the constraints.

        The carry-list of (table, column) pairs to UPDATE is derived dynamically from
        ``pg_constraint`` (the same rows fetched to drop/re-add the FKs), so it automatically
        covers every current and future node-FK referrer — no hardcoded list to drift.
        ``nodes.parent_id`` (self-referential) is handled by the explicit UPDATE below and
        excluded from the derived loop to avoid double-handling.
        """
        pairs = list(pairs)
        if not pairs:
            return 0
        with self._pool.connection() as conn, conn.transaction():
            fks = conn.execute(
                """SELECT conrelid::regclass::text AS tbl, conname,
                          pg_get_constraintdef(oid) AS def
                   FROM pg_constraint
                   WHERE confrelid = 'nodes'::regclass AND contype = 'f'"""
            ).fetchall()
            for fk in fks:
                conn.execute(f'ALTER TABLE {fk["tbl"]} DROP CONSTRAINT "{fk["conname"]}"')
            # Build the carry-list once from the fks rows: parse each referencing column
            # from the constraint definition; exclude nodes (self-ref parent_id is handled
            # by the explicit UPDATE below).
            _fk_col_re = re.compile(r"FOREIGN KEY \(([^)]+)\) REFERENCES nodes")
            carry: list[tuple[str, str]] = []
            for fk in fks:
                if fk["tbl"] == "nodes":
                    continue  # self-referential; parent_id handled explicitly
                m = _fk_col_re.search(fk["def"])
                if m:
                    carry.extend((fk["tbl"], c.strip()) for c in m.group(1).split(","))
            changed = 0
            for old_id, new_eid in pairs:
                row = conn.execute(
                    "SELECT source FROM nodes WHERE id = %s", (old_id,)
                ).fetchone()
                if row is None:
                    continue
                new_id = node_id(row["source"], new_eid)
                if new_id == old_id:
                    conn.execute(
                        "UPDATE nodes SET external_id = %s WHERE id = %s", (new_eid, old_id))
                    changed += 1
                    continue
                conn.execute("UPDATE nodes SET id = %s, external_id = %s WHERE id = %s",
                             (new_id, new_eid, old_id))
                conn.execute("UPDATE nodes SET parent_id = %s WHERE parent_id = %s",
                             (new_id, old_id))
                for tbl, col in carry:
                    conn.execute(
                        f"UPDATE {tbl} SET {col} = %s WHERE {col} = %s",
                        (new_id, old_id),
                    )
                changed += 1
            for fk in fks:
                conn.execute(
                    f'ALTER TABLE {fk["tbl"]} ADD CONSTRAINT "{fk["conname"]}" {fk["def"]}')
        return changed

    # ---- read (point) ------------------------------------------------------
    def _record_access(self, ids: "list[UUID]") -> None:
        ids = list(ids)
        if not ids:
            return
        with self._pool.connection() as conn:
            conn.execute(
                "UPDATE nodes SET access_count = access_count + 1, last_accessed_at = now() "
                "WHERE id = ANY(%s)", (ids,),
            )

    def get(self, id: UUID, *, track_access: bool = False) -> Node | None:
        with self._pool.connection() as conn:
            row = conn.execute(
                f"SELECT {_COLUMNS} FROM nodes WHERE id = %s", (id,)
            ).fetchone()
        if row is None:
            return None
        if track_access:
            self._record_access([id])
        return _row_to_node(row)

    def get_by_path(self, path: str, *, track_access: bool = False) -> Node | None:
        with self._pool.connection() as conn:
            row = conn.execute(
                f"SELECT {_COLUMNS} FROM nodes WHERE path = %s::ltree", (path,)
            ).fetchone()
        if row is None:
            return None
        if track_access:
            self._record_access([row["id"]])
        return _row_to_node(row)

    # ---- read (tree) -------------------------------------------------------
    def children(self, path: str) -> list[Node]:
        with self._pool.connection() as conn:
            if path == "":
                cur = conn.execute(
                    f"SELECT {_COLUMNS} FROM nodes WHERE parent_id IS NULL ORDER BY slug"
                )
            else:
                cur = conn.execute(
                    f"""SELECT {_COLUMNS} FROM nodes
                        WHERE parent_id = (SELECT id FROM nodes WHERE path = %s::ltree)
                        ORDER BY slug""",
                    (path,),
                )
            return [_row_to_node(r) for r in cur.fetchall()]

    def subtree(self, path: str, *, with_content: bool = True) -> list[Node]:
        """`path` and everything under it. with_content=False skips the note bodies
        (content '' / summary None) for callers that only need metadata."""
        cols = _COLUMNS if with_content else _COLUMNS_NO_BODY
        with self._pool.connection() as conn:
            cur = conn.execute(
                f"SELECT {cols} FROM nodes WHERE path <@ %s::ltree ORDER BY path",
                (path,),
            )
            return [_row_to_node(r) for r in cur.fetchall()]

    def ancestors(self, path: str) -> list[Node]:
        with self._pool.connection() as conn:
            cur = conn.execute(
                f"""SELECT {_COLUMNS} FROM nodes
                    WHERE path @> %s::ltree AND path <> %s::ltree
                    ORDER BY nlevel(path)""",
                (path, path),
            )
            return [_row_to_node(r) for r in cur.fetchall()]

    def healthcheck(self) -> None:
        """Cheap liveness probe: run SELECT 1 through the pool. Raises on any DB failure."""
        with self._pool.connection() as conn:
            conn.execute("SELECT 1")

    # ---- read (search) -----------------------------------------------------
    def _hnsw_iterative_scan(self, conn: "psycopg.Connection") -> bool:
        """Whether this server's pgvector exposes `hnsw.iterative_scan` (>= 0.8).

        Probed once per KnowledgeBase and cached. It has to be a probe rather than a
        version check because the GUC is registered lazily when pgvector's module
        first loads in a backend, and it cannot be attempted inline in the search
        transaction: SET of an unknown parameter raises, which would abort the very
        transaction the search runs in. Failing closed just means filtered ANN keeps
        pgvector's default single-pass behaviour.
        """
        if self._iterative_scan is None:
            try:
                with conn.transaction():
                    conn.execute("SET LOCAL hnsw.iterative_scan = relaxed_order")
                self._iterative_scan = True
            except psycopg.Error:
                self._iterative_scan = False
        return self._iterative_scan

    def search(self, query: str, *, under: str | None = None, source: str | None = None,
               type: str | None = None, tags: list[str] | None = None,
               kind: str | None = None,
               since: "datetime | None" = None, until: "datetime | None" = None,
               embedding: "list[float] | None" = None,
               include_dead: bool = True,
               limit: int = 20) -> list[SearchResult]:
        """Ranked memory search: FTS alone, or hybrid when the caller supplies a query
        embedding — FTS and embedding-kNN run as independent arms and are fused by
        reciprocal rank (RRF_K), so conceptual matches surface even when the words
        differ. The FTS arm keeps the importance/recency-decayed ranking; the kNN arm
        is an exact cosine scan floored at KNN_MIN_SIM. since/until bound each memory's
        event time (COALESCE(event_at, source_created_at), half-open [since, until) —
        the same clock as entity_activity).

        Scaffolding nodes (SEARCH_SCAFFOLDING_TYPES) are excluded unless the caller
        passes an explicit `type`. include_dead=False drops retired memories — off by
        default because historical-lookup questions legitimately want them (filtering
        them unconditionally costs ~0.11 must_recall on those items); pass False for
        "what is the CURRENT state of X" questions.
        """
        # One filter block shared by both arms, so a facet/time scope can never let a
        # vector match leak past it.
        filters = SEARCH_FILTERS
        # importance × retrieval-recency multiplier — the "search priority" factor.
        # Both arms order by it (FTS on ts_rank, kNN on cosine), so an important,
        # recently-retrieved memory outranks an equally-similar forgotten one.
        imp_factor = _IMP_FACTOR
        decayed = ("ts_rank(content_tsv, websearch_to_tsquery('english', %(q)s)) * "
                   + imp_factor)
        # FTS OR-fallback: websearch_to_tsquery is AND-semantics, so a
        # keyword-bag query that never co-occurs verbatim silently empties the FTS
        # arm and hybrid degrades to vector-only. When the AND form matches nothing
        # and the query is a SHORT keyword bag, fall back to OR-joined terms —
        # ts_rank's frequency/proximity scoring then orders the partial matches.
        # Gated at OR_FALLBACK_MAX_WORDS: a long natural-language request would
        # OR-match half the corpus and flood the fusion (measured on a golden query) —
        # long queries are the vector arm's job. Without an embedding there is no
        # vector arm and no fusion, so the cap is far higher (or_fallback_eligible).
        q_fts = query
        if or_fallback_eligible(query, embedded=embedding is not None):
            with self._pool.connection() as conn:
                hit = conn.execute(
                    "SELECT EXISTS (SELECT 1 FROM nodes WHERE content_tsv @@ "
                    "websearch_to_tsquery('english', %(q)s)) AS hit",
                    {"q": query}).fetchone()["hit"]
            if not hit:
                q_fts = " OR ".join(query.split())
        params: dict = {"q": q_fts, "under": under, "source": source, "type": type,
                        "tags": tags, "kind": kind, "since": since, "until": until,
                        "limit": limit, "floor": self._policy.importance.recency_floor,
                        "ln2": math.log(2),
                        "hl": self._policy.importance.recency_half_life_seconds,
                        "nostub": list(SEARCH_SCAFFOLDING_TYPES),
                        "inc_dead": include_dead}
        if embedding is None:
            sql = f"""
                SELECT {_COLUMNS},
                       ts_rank(content_tsv, websearch_to_tsquery('english', %(q)s)) AS relevance,
                       {decayed} AS score,
                       NULL::float AS similarity,
                       ts_headline('english', content,
                                   websearch_to_tsquery('english', %(q)s)) AS snippet
                FROM nodes
                WHERE content_tsv @@ websearch_to_tsquery('english', %(q)s)
                  {filters}
                -- `id` is the final tiebreak, mirroring 0037's kNN tiebreak (the
                -- hybrid branch's `knn` CTE): on score ties this branch was
                -- previously free to return any permutation. Found by the mode-A
                -- equivalence proof.
                ORDER BY score DESC, id
                LIMIT %(limit)s
                """
        else:
            params["vec"] = "[" + ",".join(repr(float(x)) for x in embedding) + "]"
            params["pool"] = max(limit, HYBRID_ARM_POOL)
            params["rrfk"] = RRF_K
            params["annnotes"] = params["pool"]
            params["annchunks"] = params["pool"] * ANN_CHUNK_FANOUT
            sql = f"""
                WITH fts AS (
                    -- `id` is the final tiebreak, mirroring the FTS-only branch
                    -- above, fts_candidates(), and this query's own `knn` CTE
                    -- below -- ts_rank ties are common (short/
                    -- templated content) and this CTE was the one place that
                    -- fix missed, so a prior write that relocated a tied row's
                    -- tuple (record_access, the eval harness's access-count
                    -- restore) could silently reorder the untiebroken rank and
                    -- change which row fell at the pool/top-k boundary --
                    -- exactly the "eval reruns were not reproducible" failure
                    -- mode the `knn` CTE's own comment already names.
                    SELECT id, {decayed} AS score,
                           row_number() OVER (ORDER BY {decayed} DESC, id) AS r
                    FROM nodes
                    WHERE content_tsv @@ websearch_to_tsquery('english', %(q)s)
                      {filters}
                    ORDER BY r LIMIT %(pool)s
                ), cand AS (
                    -- A note's semantic score is the best of its whole-note vector
                    -- and its chunk vectors (node_chunks, migration 0033): one
                    -- vector cannot represent a long note — the embedder clips at
                    -- ~6k chars and even fully-embedded long notes dilute specific
                    -- passages into a topic average.
                    --
                    -- SHAPE IS LOAD-BEARING (migration 0037). This was one scan over
                    -- `nodes` computing greatest(own vector, correlated max over its
                    -- chunks). That is exactly the shape no ANN index can serve: with
                    -- no ORDER BY/LIMIT on a distance expression there is nothing for
                    -- HNSW to answer, so it planned as a Seq Scan with a per-row
                    -- SubPlan and stayed that way even with an index present and
                    -- enable_seqscan=off. Adding indexes alone bought nothing.
                    --
                    -- So each vector source is now its OWN ANN-ordered leg — ordered
                    -- by the raw `<=>` distance ascending, which is the only form the
                    -- index can serve (`1 - (...)` DESC is an opaque expression to the
                    -- planner) — and the union is max-reduced per note, reproducing
                    -- the greatest() semantics. Same answer, index-shaped.
                    SELECT id, max(similarity) AS similarity,
                           max(importance) AS importance
                    FROM (
                        (SELECT nodes.id, nodes.importance,
                                1 - (nodes.embedding <=> %(vec)s::vector) AS similarity
                         FROM nodes
                         WHERE nodes.embedding IS NOT NULL
                           {filters}
                         ORDER BY nodes.embedding <=> %(vec)s::vector
                         LIMIT %(annnotes)s)
                        UNION ALL
                        -- The chunk ANN scan MUST be its own join-free subquery. As
                        -- `FROM node_chunks c JOIN nodes ...  ORDER BY c.embedding
                        -- <=> v` the planner cannot produce an HNSW plan at all —
                        -- not a costing preference, a structural refusal: it fell
                        -- back to a full node_chunks_pkey scan even with
                        -- enable_seqscan=off. Ordering the chunks alone first, then
                        -- joining, is what lets the index serve it.
                        --
                        -- The filters therefore land AFTER the chunk limit, so a
                        -- selective scope post-filters an ANN result rather than
                        -- steering it. ANN_CHUNK_FANOUT is the headroom that buys
                        -- back; verified against the golden set's filtered items
                        -- (a path+window item and a doc_kind item).
                        (SELECT nodes.id, nodes.importance,
                                1 - ch.dist AS similarity
                         FROM (SELECT node_id, (embedding <=> %(vec)s::vector) AS dist
                               FROM node_chunks
                               ORDER BY embedding <=> %(vec)s::vector
                               LIMIT %(annchunks)s) ch
                         JOIN nodes ON nodes.id = ch.node_id
                         WHERE TRUE
                           {filters})
                    ) legs
                    GROUP BY id
                ), knn AS (
                    -- Raw cosine ordering, importance as TIEBREAK only: the
                    -- corpus's importance is often bimodal (boosted vs default),
                    -- so any multiplicative importance factor hands one class a
                    -- flat edge that swamps real cosine differences. Importance
                    -- keeps its full weight in the FTS arm and concept ranking.
                    --
                    -- No similarity floor here on purpose: gold and off-corpus cosine
                    -- distributions overlap per-row, so any cut deep enough to remove
                    -- junk also removes real hits (measured). "Nothing matched" is
                    -- answered at the set level by search_confidence() instead.
                    -- `id` is the final tiebreak so this arm is DETERMINISTIC. On
                    -- (similarity, importance) ties Postgres was free to return any
                    -- permutation, and since `r` feeds the RRF fusion below, identical
                    -- inputs could yield different fused scores run to run — which is
                    -- why eval reruns were not reproducible.
                    SELECT id, similarity,
                           row_number() OVER (ORDER BY similarity DESC,
                                              importance DESC, id) AS r
                    FROM cand
                    ORDER BY r LIMIT %(pool)s
                ), fused AS (
                    SELECT COALESCE(fts.id, knn.id) AS fid,
                           COALESCE(1.0 / (%(rrfk)s + fts.r), 0)
                             + COALESCE(1.0 / (%(rrfk)s + knn.r), 0) AS rrf,
                           fts.score AS score, knn.similarity AS similarity
                    FROM fts FULL OUTER JOIN knn ON knn.id = fts.id
                )
                SELECT {_COLUMNS}, fused.rrf AS relevance,
                       COALESCE(fused.score, 0) AS score, fused.similarity,
                       ts_headline('english', content,
                                   websearch_to_tsquery('english', %(q)s)) AS snippet
                FROM fused JOIN nodes ON nodes.id = fused.fid
                ORDER BY fused.rrf DESC, nodes.path
                LIMIT %(limit)s
                """
        with self._pool.connection() as conn:
            # REQUIRED once the HNSW indexes exist (0037) — an ANN scan silently
            # under-returns without it, in two separate ways:
            #
            #   1. hnsw.ef_search defaults to 40, and that is a HARD CAP on rows an
            #      index scan yields regardless of LIMIT. HYBRID_ARM_POOL is 50, so
            #      even an unfiltered query lost 10 of its 50 candidates. Measured
            #      directly: LIMIT 50 returned 40 rows, and 50 with this set.
            #   2. The filter is applied to what the scan hands back, so a selective
            #      `under`/`kind`/`since` can spend the whole budget on rejects.
            #
            # Both look like ranking problems and neither raises. `relaxed_order` (not
            # strict) is right because the union is re-sorted downstream by `knn`'s
            # row_number(), so intra-scan ordering buys nothing and costs time.
            if embedding is not None and self._hnsw_iterative_scan(conn):
                with conn.transaction():
                    conn.execute("SET LOCAL hnsw.iterative_scan = relaxed_order")
                    rows = conn.execute(sql, params).fetchall()
            else:
                rows = conn.execute(sql, params).fetchall()
            results = [
                SearchResult(node=_row_to_node(r), rank=r["relevance"],
                             snippet=r["snippet"], score=r["score"],
                             similarity=r["similarity"])
                for r in rows
            ]
        self._record_access([res.node.id for res in results])
        return results

    def list(self, *, under: str | None = None, source: str | None = None,
             type: str | None = None, tags: list[str] | None = None,
             kind: str | None = None,
             limit: int = 50, offset: int = 0,
             with_content: bool = True) -> list[Node]:
        # with_content=False: metadata only (content '' / summary None) -- see _COLUMNS_NO_BODY.
        cols = _COLUMNS if with_content else _COLUMNS_NO_BODY
        with self._pool.connection() as conn:
            cur = conn.execute(
                f"""
                SELECT {cols} FROM nodes
                WHERE (%(under)s::ltree IS NULL OR path <@ %(under)s::ltree)
                  AND (%(source)s::text IS NULL OR source = %(source)s::text)
                  AND (%(type)s::text IS NULL OR type = %(type)s::text)
                  AND (%(tags)s::text[] IS NULL OR tags @> %(tags)s::text[])
                  AND (%(kind)s::text IS NULL OR doc_kind = %(kind)s::text)
                ORDER BY path
                LIMIT %(limit)s OFFSET %(offset)s
                """,
                {"under": under, "source": source, "type": type, "tags": tags,
                 "kind": kind, "limit": limit, "offset": offset},
            )
            return [_row_to_node(r) for r in cur.fetchall()]

    # ---- read primitives (KB_Retrieval fuses over these) --------------------
    @contextmanager
    def read_snapshot(self):
        """One pinned connection in REPEATABLE READ, so every primitive in one
        query sees ONE snapshot.

        Fusion moved from a single SQL statement into Python, which turned one
        round trip into three. Without this, an ingest landing between the FTS and
        kNN fetches would make the mode-A equivalence proof flaky for a reason that
        has nothing to do with fusion.
        """
        with self._pool.connection() as conn:
            with conn.transaction():
                conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
                yield conn

    def order_by_path(self, ids: "list", *, conn=None) -> "list":
        """`ids` ordered by `nodes.path`, server-side.

        The final tiebreak in mode A is `ORDER BY fused.rrf DESC, nodes.path`,
        which uses ltree's NATIVE comparator.

        Measured over 394 random legal paths:
            ltree-native == Python str sort     -> TRUE
            ltree-native == ORDER BY path::text -> FALSE (diverges at row 0)

        So Python is not the trap, and an earlier version of this docstring said
        it was. Byte order happens to agree with ltree because labels are
        [A-Za-z0-9_] and the '.' separator (0x2E) sorts below every legal label
        character, making bytewise and label-wise comparison coincide. The trap
        is the obvious rewrite: `_COLUMNS` already selects `path::text`, so
        ordering by that value -- in SQL, or via any locale-aware sort -- yields
        a different order. Taking the ordering from the server, by the ltree
        column itself, removes the dependency on that charset coincidence.
        """
        if not ids:
            return []
        sql = "SELECT id FROM nodes WHERE id = ANY(%(ids)s) ORDER BY path"
        params = {"ids": [str(i) for i in ids]}
        if conn is not None:
            rows = conn.execute(sql, params).fetchall()
        else:
            with self._pool.connection() as c:
                rows = c.execute(sql, params).fetchall()
        return [r["id"] for r in rows]

    def fts_matches_any(self, query: str, *, conn=None) -> bool:
        """Does the AND-form tsquery match anything at all, corpus-wide?

        This is the OR-fallback probe, lifted verbatim from search() (its
        `SELECT EXISTS` before the OR rewrite) INCLUDING its known defect: it omits the shared filter block, so it asks a
        corpus-global question while the arm asks a scoped one (a known defect).
        Fixing that changes results and must be a separate, measured change --
        never part of the equivalence lift.
        """
        sql = ("SELECT EXISTS (SELECT 1 FROM nodes WHERE content_tsv @@ "
               "websearch_to_tsquery('english', %(q)s)) AS hit")
        if conn is not None:
            return conn.execute(sql, {"q": query}).fetchone()["hit"]
        with self._pool.connection() as c:
            return c.execute(sql, {"q": query}).fetchone()["hit"]

    def fts_candidates(self, query: str, *, under=None, source=None, type=None,
                       tags=None, kind=None, since=None, until=None,
                       include_dead: bool = True, pool: int = HYBRID_ARM_POOL,
                       conn=None) -> "list[dict]":
        """The lexical arm as candidates: `[{id, relevance, score, ordinal}]`.

        `relevance` is the raw ts_rank -- SearchResult.rank's FTS-only meaning
        (domain.SearchResult), mirroring search()'s FTS-only `relevance` alias.
        `score` is ts_rank x the importance/retrieval-recency multiplier --
        SearchResult.score; it is also what orders this arm, matching search()'s
        FTS-only branch `ORDER BY score DESC, id`.
        `ordinal` is the server-side row_number that mode A's `fts` CTE already
        computed. Returning it is what stops Python from re-deriving a Postgres
        ordering across float ties.

        `id` is the final tiebreak, mirroring knn_candidates' and search()'s
        FTS-only branch: ts_rank ties are common (short/templated
        content), and this is the arm search() runs whenever the caller has no
        embedding, so an untiebroken ordinal here silently disagreed with
        search()'s now-deterministic ordering at the pool boundary.
        """
        decayed = ("ts_rank(content_tsv, websearch_to_tsquery('english', %(q)s)) * "
                   + _IMP_FACTOR)
        sql = f"""
            SELECT id,
                   ts_rank(content_tsv, websearch_to_tsquery('english', %(q)s)) AS relevance,
                   {decayed} AS score,
                   row_number() OVER (ORDER BY {decayed} DESC, id) AS ordinal
            FROM nodes
            WHERE content_tsv @@ websearch_to_tsquery('english', %(q)s)
              {SEARCH_FILTERS}
            ORDER BY ordinal LIMIT %(pool)s
            """
        params = {"q": query, "under": under, "source": source, "type": type,
                  "tags": tags, "kind": kind, "since": since, "until": until,
                  "inc_dead": include_dead, "pool": pool,
                  "floor": self._policy.importance.recency_floor,
                  "ln2": math.log(2),
                  "hl": self._policy.importance.recency_half_life_seconds,
                  "nostub": list(SEARCH_SCAFFOLDING_TYPES)}
        if conn is not None:
            return [dict(r) for r in conn.execute(sql, params).fetchall()]
        with self._pool.connection() as c:
            return [dict(r) for r in c.execute(sql, params).fetchall()]

    def knn_candidates(self, embedding: "list[float]", *, under=None, source=None,
                       type=None, tags=None, kind=None, since=None, until=None,
                       include_dead: bool = True, pool: int = HYBRID_ARM_POOL,
                       conn=None) -> "list[dict]":
        """The vector arm as candidates: `[{id, similarity, importance, ordinal}]`.

        Two independently ANN-ordered legs (whole-note and chunk), max-reduced per
        note. SHAPE IS LOAD-BEARING (migration 0037). The comments in this method are
        only a summary -- the CANONICAL record of why this shape cannot be
        simplified is inside search()'s `cand`/`knn` CTEs: the join'd
        chunk leg falling back to a full `node_chunks_pkey` scan even with
        `enable_seqscan=off`, the post-filter tradeoff with its golden-set evidence,
        and why `relaxed_order` beats `strict`. Read search() first
        if this shape ever looks removable.

        MIGRATION OBLIGATION: if search() is ever retired or its SQL deleted, move
        those full comment blocks HERE BEFORE deleting them -- they are the only
        in-repo record of the measurements behind this shape, and this docstring is
        not a substitute for them.
        """
        params = {"vec": "[" + ",".join(repr(float(x)) for x in embedding) + "]",
                  "under": under, "source": source, "type": type, "tags": tags,
                  "kind": kind, "since": since, "until": until,
                  "inc_dead": include_dead, "pool": pool,
                  "nostub": list(SEARCH_SCAFFOLDING_TYPES),
                  "annnotes": pool, "annchunks": pool * ANN_CHUNK_FANOUT}
        sql = f"""
            WITH cand AS (
                SELECT id, max(similarity) AS similarity,
                       max(importance) AS importance
                FROM (
                    (SELECT nodes.id, nodes.importance,
                            1 - (nodes.embedding <=> %(vec)s::vector) AS similarity
                     FROM nodes
                     WHERE nodes.embedding IS NOT NULL
                       {SEARCH_FILTERS}
                     ORDER BY nodes.embedding <=> %(vec)s::vector
                     LIMIT %(annnotes)s)
                    UNION ALL
                    -- The chunk ANN scan MUST be its own join-free subquery. As
                    -- `FROM node_chunks c JOIN nodes ... ORDER BY c.embedding <=> v`
                    -- the planner cannot produce an HNSW plan at all -- not a costing
                    -- preference, a structural refusal. Filters therefore land AFTER
                    -- the chunk limit; ANN_CHUNK_FANOUT is the headroom that buys back.
                    (SELECT nodes.id, nodes.importance,
                            1 - ch.dist AS similarity
                     FROM (SELECT node_id, (embedding <=> %(vec)s::vector) AS dist
                           FROM node_chunks
                           ORDER BY embedding <=> %(vec)s::vector
                           LIMIT %(annchunks)s) ch
                     JOIN nodes ON nodes.id = ch.node_id
                     WHERE TRUE
                       {SEARCH_FILTERS})
                ) legs
                GROUP BY id
            )
            -- Raw cosine, importance as TIEBREAK only: the corpus's importance is
            -- often bimodal, so a multiplicative factor swamps real cosine differences.
            -- No similarity floor on purpose -- gold and off-corpus distributions
            -- overlap per-row (measured). `id` is the final tiebreak so this arm is
            -- DETERMINISTIC; without it, ties permuted and fed unstable RRF input.
            SELECT id, similarity, importance,
                   row_number() OVER (ORDER BY similarity DESC,
                                      importance DESC, id) AS ordinal
            FROM cand
            ORDER BY ordinal LIMIT %(pool)s
            """

        def _go(c):
            # REQUIRED once the HNSW indexes exist (0037): hnsw.ef_search defaults to
            # 40, a HARD CAP on rows an index scan yields regardless of LIMIT, and
            # HYBRID_ARM_POOL is 50 -- so even an unfiltered query lost 10 candidates.
            if self._hnsw_iterative_scan(c):
                c.execute("SET LOCAL hnsw.iterative_scan = relaxed_order")
            return [dict(r) for r in c.execute(sql, params).fetchall()]

        if conn is not None:
            return _go(conn)
        with self._pool.connection() as c:
            with c.transaction():
                return _go(c)

    def similarity_for(self, ids: "list", embedding: "list[float]", *,
                       under=None, source=None, type=None, tags=None, kind=None,
                       since=None, until=None, include_dead: bool = True,
                       conn=None) -> "dict":
        """Score an ARBITRARY, ALREADY-KNOWN list of ids against `embedding`:
        `{id: greatest(whole-note cosine, max chunk cosine)}` -- the same
        max-reduction knn_candidates uses, for ids the caller already HAS (found by
        walking concept_members/entity_links), not ids an ANN scan must FIND.

        This is knn_candidates' shape deliberately INVERTED, because the two answer
        different questions:
          - knn_candidates: "which ~50 rows, out of ALL of nodes/node_chunks, are
            nearest `embedding`?" -- an unknown-until-computed top-K, only
            answerable by handing Postgres an `ORDER BY <=> LIMIT` it can serve with
            HNSW. That is 0037's whole justification for that method's two-leg,
            filters-after-limit shape.
          - similarity_for: "for THESE ids, what is their cosine?" -- every id is
            scored, none are searched for. There is nothing for an ANN index to do,
            and NO `ORDER BY`/`LIMIT` on a distance expression appears anywhere
            below. Each leg is an equality lookup keyed by `ids`: the node leg on
            `nodes.id` (nodes_pkey), the chunk leg on `node_chunks.node_id`, which
            is the LEADING column of that table's PK `(node_id, seq)`.

        Do NOT copy knn_candidates' `_hnsw_iterative_scan` / `SET LOCAL
        hnsw.iterative_scan` dance in here. That exists ONLY because knn_candidates'
        plan is an HNSW index scan capped by ef_search=40. This method produces no
        ANN index scan, so the GUC is irrelevant and setting it would be silent
        copy-paste debt.

        Because there is no LIMIT to post-filter after, the chunk leg carries
        SEARCH_FILTERS up front in its WHERE clause -- knn_candidates'
        post-filter-after-limit tradeoff does not apply here.

        Output contract, deliberately UNLIKE fts_candidates/knn_candidates (which
        only ever return matches):
          - an id unknown to `nodes`, or excluded by the shared filter block, is
            OMITTED -- the same "never outflank a facet" contract as everywhere else
            that interpolates SEARCH_FILTERS: a row a facet excludes must not be
            scored at all, not merely scored None.
          - an id that IS in scope but has neither a whole-note embedding nor any
            chunk embedding maps to `None` -- present, but genuinely unscoreable.
            The caller is fusing over a bounded, already-decided pool and needs to
            tell "not in this pool's scope" from "in scope, nothing to compare".
          - no similarity floor, matching knn_candidates. A floor was measured
            harmful; see that method and kb_retrieval/negatives.py.

        WARNING -- entity/folder ids. SEARCH_FILTERS' scaffolding exclusion
        (SEARCH_SCAFFOLDING_TYPES) drops `type IN ('entity','folder')` unless the
        caller passes an explicit `type=`. So an entity-node id passed here without
        `type='entity'` is OMITTED, **not** scored None: `result[entity_id]` raises
        KeyError. A caller must either split its call by node type or be prepared
        for those ids to be missing. (Verified by execution. On a real
        corpus entity/folder nodes carry no `nodes.embedding` and appear in no
        `node_chunks` row, so they would score None anyway if let through -- but the
        omission is the behaviour to code against.)

        MUST agree with knn_candidates for the same (note, vector) pair -- both are
        the ruler fusion measures every row with, and a divergence would silently
        corrupt fusion. Pinned by
        test_similarity_for_agrees_with_knn_candidates_for_the_same_note_and_vector.
        """
        if not ids:
            return {}
        params = {"ids": [str(i) for i in ids],
                  "vec": "[" + ",".join(repr(float(x)) for x in embedding) + "]",
                  "under": under, "source": source, "type": type, "tags": tags,
                  "kind": kind, "since": since, "until": until,
                  "inc_dead": include_dead,
                  "nostub": list(SEARCH_SCAFFOLDING_TYPES)}
        sql = f"""
            WITH legs AS (
                -- Whole-note leg: one row per in-scope id, whether or not it HAS an
                -- embedding. `NULL <=> vec` is NULL, not an error (verified), which
                -- is what lets an in-scope, wholly-unembedded id surface with
                -- similarity NULL below rather than silently vanishing.
                (SELECT nodes.id, 1 - (nodes.embedding <=> %(vec)s::vector) AS similarity
                 FROM nodes
                 WHERE nodes.id = ANY(%(ids)s)
                   {SEARCH_FILTERS})
                UNION ALL
                -- Chunk leg: a plain equality join, NOT an ANN scan -- see the
                -- docstring. Filters land in the WHERE clause up front; there is no
                -- limit to dodge by deferring them.
                (SELECT nodes.id, 1 - (c.embedding <=> %(vec)s::vector) AS similarity
                 FROM node_chunks c
                 JOIN nodes ON nodes.id = c.node_id
                 WHERE c.node_id = ANY(%(ids)s)
                   {SEARCH_FILTERS})
            )
            -- max() ignores NULLs and returns NULL only when every value in the
            -- group is NULL (verified) -- exactly the greatest(whole-note,
            -- best-chunk) reduction knn_candidates uses, computed the same way, and
            -- exactly the semantics the "in scope but unscoreable -> None" contract
            -- depends on.
            SELECT id, max(similarity) AS similarity
            FROM legs
            GROUP BY id
            """

        def _go(c):
            return {r["id"]: r["similarity"] for r in c.execute(sql, params).fetchall()}

        if conn is not None:
            return _go(conn)
        with self._pool.connection() as c:
            return _go(c)

    def background_reservoir_ids(self, n: int, *, seed: str = "0", conn=None) -> "list":
        """A repeatable pseudo-random sample of `n` live, embedded notes: the reservoir a
        confidence background is scored against (via similarity_for, the production
        ruler). ORDER BY md5(id || seed) instead of random(), so a seed reproduces its
        sample exactly -- the bake-off's fixed seeds, production's refresh epoch."""
        def _go(c):
            return [r["id"] for r in c.execute(
                "SELECT id FROM nodes WHERE type = 'note' AND dead_at IS NULL "
                "AND embedding IS NOT NULL ORDER BY md5(id::text || %(seed)s) "
                "LIMIT %(n)s", {"seed": seed, "n": n}).fetchall()]
        if conn is not None:
            return _go(conn)
        with self._pool.connection() as c:
            return _go(c)

    def concept_candidates(self, embedding: "list[float]", *,
                           k: int = CONCEPT_ENTRY_ANN_K, damp: bool = True,
                           under=None, source=None, type=None, tags=None,
                           kind=None, since=None, until=None,
                           include_dead: bool = True, conn=None) -> "list[dict]":
        """The crawl's ENTRY step: candidate concepts from TWO independent families
        in one call -- `[{concept_id, name, description, member_count, label_rank,
        vote_rank, vote_score}]`.

        Family A (`label_rank`): cosine between `embedding` and each ACTIVE
        concept's OWN embedding, which is of `f"{name}\\n{description}".strip()`
        (consolidate.py `_embed_labels`). Scanned in FULL, not via an ANN
        index: a personal corpus has a few dozen active concepts at most, and a seq scan over that many rows is strictly cheaper than an
        index probe. Do NOT add an HNSW/ivfflat index on concepts.embedding
        reflexively if this ever looks slow -- an internal audit put the crossover
        near 100 concepts; below that an index is pure overhead.

        Family B (`vote_rank` / `vote_score`): the query's own ANN top-`k` over
        nodes.embedding -- whole-note vectors ONLY (unlike knn_candidates this does
        not also scan node_chunks; this is a coarse "which topics are germane" entry
        signal, not a memory-precision arm) -- joined through concept_members with
        `member_kind = 'memory'` ONLY. Entity members of active concepts are
        measured 0% classes (every one an instance); they are a
        different axis and out of scope for a memory-evidence vote.

        Each matched concept's vote is `sum(sim * cm.score)`, then -- when `damp`,
        the default -- divided by `log2(1 + member_count)`: the same ubiquity-damper
        SHAPE as `_EXPAND_DAMP`, over the concept's own stored `member_count` rather
        than a joined support count. MEASURED as a clear top-1 gain on a private
        golden set -- do not drop it. `damp=False` exists for A/B evaluation against
        that measurement; production callers leave it True. `vote_score` is
        whichever value actually produced `vote_rank`, mirroring how
        knn_candidates' `similarity` is both the returned metric and its own
        ranking key.

        `c.status = 'active'` inside the vote join is NOT redundant guard-railing.
        concept_members rows for a RETIRED concept are left in place until that
        concept is next matched or overwritten (apply_concept_run only replaces
        members for concepts touched in the SAME run, never on retirement),
        so an unguarded join would let a retired concept's stale
        membership vote -- and, worse than merely appearing, consume a rank slot in
        `ranked_votes` and shift a legitimate concept's vote_rank.

        SEARCH_FILTERS is interpolated into family B's memory ANN scan because that
        leg selects memories and the house rule applies. Family A never touches
        `nodes`: a concept is not itself facetable by
        under/source/type/tags/kind/since/until, so no filter applies to that leg.

        A concept with NEITHER a label match NOR a vote is omitted entirely.
        `label_rank`/`vote_rank` are 1-based server-side row_number()s and EITHER
        MAY BE None for a concept present in only one family -- callers must not
        assume both are populated, and must not sort on a rank column without
        filtering None first.
        """
        k = max(1, int(k))
        params = {"vec": "[" + ",".join(repr(float(x)) for x in embedding) + "]",
                  "k": k, "damp": bool(damp),
                  "under": under, "source": source, "type": type, "tags": tags,
                  "kind": kind, "since": since, "until": until,
                  "inc_dead": include_dead,
                  "nostub": list(SEARCH_SCAFFOLDING_TYPES)}
        sql = f"""
            WITH labels AS (
                -- Family A. Full scan, no ANN index wanted (see docstring).
                SELECT id AS concept_id,
                       row_number() OVER (ORDER BY embedding <=> %(vec)s::vector ASC,
                                          id) AS label_rank
                FROM concepts
                WHERE status = 'active' AND embedding IS NOT NULL
            ), ann AS (
                -- Family B stage 1: the query's ANN top-k, whole-note vectors only.
                SELECT id, 1 - (embedding <=> %(vec)s::vector) AS sim
                FROM nodes
                WHERE embedding IS NOT NULL
                  {SEARCH_FILTERS}
                ORDER BY embedding <=> %(vec)s::vector
                LIMIT %(k)s
            ), votes AS (
                -- Family B stage 2: damped evidence vote. The c.status='active'
                -- join is load-bearing, not defensive -- see docstring.
                SELECT cm.concept_id,
                       sum(ann.sim * cm.score)
                         / (CASE WHEN %(damp)s::bool
                                 THEN ln(1 + greatest(1, max(c.member_count))) / ln(2)
                                 ELSE 1 END) AS vote_score
                FROM ann
                JOIN concept_members cm ON cm.node_id = ann.id
                                        AND cm.member_kind = 'memory'
                JOIN concepts c ON c.id = cm.concept_id AND c.status = 'active'
                GROUP BY cm.concept_id
            ), ranked_votes AS (
                SELECT concept_id, vote_score,
                       row_number() OVER (ORDER BY vote_score DESC, concept_id) AS vote_rank
                FROM votes
            )
            SELECT c.id AS concept_id, c.name, c.description, c.member_count,
                   labels.label_rank, ranked_votes.vote_rank, ranked_votes.vote_score
            FROM concepts c
            LEFT JOIN labels ON labels.concept_id = c.id
            LEFT JOIN ranked_votes ON ranked_votes.concept_id = c.id
            WHERE c.status = 'active'
              AND (labels.label_rank IS NOT NULL OR ranked_votes.vote_rank IS NOT NULL)
            -- Presentation order only -- NOT a fused rank. Python owns fusion (the
            -- measured label[:3] u vote_damped[:2] union). This just makes iteration
            -- order deterministic, best-of-either-family first.
            ORDER BY least(coalesce(labels.label_rank, 2147483647),
                           coalesce(ranked_votes.vote_rank, 2147483647)),
                     c.id
            """

        def _go(c):
            # Same HNSW correctness fix as knn_candidates: a no-op at the default
            # k=25 (well under ef_search's 40 hard cap), kept unconditionally
            # because k is caller-overridable.
            if self._hnsw_iterative_scan(c):
                c.execute("SET LOCAL hnsw.iterative_scan = relaxed_order")
            return [dict(r) for r in c.execute(sql, params).fetchall()]

        if conn is not None:
            return _go(conn)
        with self._pool.connection() as c:
            with c.transaction():
                return _go(c)

    def last_consolidation(self, *, conn=None):
        """When the derived concept layer was last rebuilt, or None if never.

        `trace.consolidated_at` (part of the disclosure surface) exists so a caller
        can tell "the crawl found nothing" from "the concept layer is cold" -- an
        empty `trace.entry` looks identical in both cases otherwise. Reads the
        latest FINISHED, SUCCESSFUL run: `finished_at` is NULL while a run is in
        flight, and a 'failed' run's timestamp would overstate freshness.
        """
        sql = ("SELECT max(finished_at) AS at FROM concept_runs "
               "WHERE status = 'done' AND finished_at IS NOT NULL")
        if conn is not None:
            return conn.execute(sql).fetchone()["at"]
        with self._pool.connection() as c:
            return c.execute(sql).fetchone()["at"]

    def hydrate(self, ids: "list", query: str, *, conn=None) -> "dict":
        """Full rows + snippets for the FINAL page only: `{id: (Node, snippet)}`.

        This is the third round trip that Python-side fusion implies. It is cheaper
        than the statement it replaces, which ran ts_headline over the fused set
        rather than the returned page. Unknown ids are omitted, not raised -- a
        caller fusing over two arms may hold an id that a concurrent delete removed.
        """
        if not ids:
            return {}
        sql = f"""
            SELECT {_COLUMNS},
                   ts_headline('english', content,
                               websearch_to_tsquery('english', %(q)s)) AS snippet
            FROM nodes WHERE id = ANY(%(ids)s)
            """
        params = {"ids": [str(i) for i in ids], "q": query}
        if conn is not None:
            rows = conn.execute(sql, params).fetchall()
        else:
            with self._pool.connection() as c:
                rows = c.execute(sql, params).fetchall()
        return {r["id"]: (_row_to_node(r), r["snippet"]) for r in rows}

    def node_meta(self, ids: "list", *, conn=None) -> "dict":
        """Metadata-only lookup for RENDERING a candidate: `{id: {"title", "doc_kind",
        "event_at"}}`.

        The cheap half of hydrate(). The crawl's hop judge renders every offered frontier
        row as `"[i] title . doc_kind . sim=... . via=... . event_at|undated"`, which needs
        exactly these three columns and nothing else. hydrate() is reserved BY CONTRACT for
        the final returned page, and a frontier row is usually not on it, so calling
        hydrate() here would both break that contract and pay `ts_headline` over up to 40
        rows whose snippets nothing ever reads.

        NO FILTERS, on purpose, and this is the one read primitive in this class that
        interpolates neither SEARCH_FILTERS nor SEARCH_SCAFFOLDING_TYPES. Every id that
        reaches this method already passed its own arm's filter block -- concept_frontier's,
        bridge_frontier's, or the flat pool's -- so a second application could only ever
        REMOVE a row the frontier has already committed to offering, leaving a hole at an
        index the judge is about to reference by number. A facet is enforced where
        candidates are selected; this method only describes ids someone else already
        selected. (Consequence, pinned by a test: a type='entity' node IS described here,
        while similarity_for OMITS the same id.)

        NO `ORDER BY`. The caller owns the order -- the frontier menu is the primitives'
        server-side `ordinal`, decided long before this call -- and an ordering here would
        be a second, silent one competing with it. That is exactly the kind of drift this
        codebase audits for, so its absence is stated rather than left to be noticed.

        Ids with no row are OMITTED, not returned with null fields. A caller must be able to
        tell "there is no such node any more" from "a node with no title": the first is a
        row to drop from the frontier, the second renders as untitled and stays. Pre-seeding
        the result from `ids` would turn a concurrently-deleted memory into an untitled live
        one at an admissible index.

        One index lookup on `nodes_pkey` (`id uuid PRIMARY KEY`, migration 0001), no joins,
        no vector, no headline.
        """
        if not ids:
            return {}
        sql = ("SELECT id, title, doc_kind, event_at FROM nodes "
               "WHERE id = ANY(%(ids)s)")
        params = {"ids": [str(i) for i in ids]}
        if conn is not None:
            rows = conn.execute(sql, params).fetchall()
        else:
            with self._pool.connection() as c:
                rows = c.execute(sql, params).fetchall()
        return {r["id"]: {"title": r["title"], "doc_kind": r["doc_kind"],
                          "event_at": r["event_at"]} for r in rows}

    def record_access(self, ids: "list") -> None:
        """Public alias for the retrieval-recency bump.

        `search()` calls the private `_record_access`; a strategy living in another
        module must not reach through the underscore. Same behaviour, one caller-
        visible name. NOTE this is a WRITE on the read path -- it feeds the
        importance/recency multiplier that the FTS arm orders by, which is why the
        eval harness snapshots and restores these columns.
        """
        self._record_access(ids)

    def concept_frontier(self, concept_ids: "list", *, under=None, source=None,
                         type=None, tags=None, kind=None, since=None, until=None,
                         include_dead: bool = True, conn=None) -> dict:
        """The members of `concept_ids`, split by kind:
        `{"memories": [...], "entities": [...]}`.

        memories: `[{id, concept_id, cm_score, ordinal}]` -- SEARCH_FILTERS applies,
        the same as every other memory-selecting primitive, so a facet can never be
        outflanked.

        entities: `[{entity_id, concept_id, cm_score, name, support, ordinal}]` --
        `support` is the entity's TOTAL distinct-memory count from entity_links (not
        scoped to this call), the same quantity entity_profile() calls `mentions`,
        mirroring the `sup.support` idiom expand() already uses for ubiquity-damping.
        Returning it here saves the caller a round trip per entity.

        SEARCH_FILTERS is DELIBERATELY NOT applied to the entity arm, and this is
        mechanical, not stylistic. Every entity node is type='entity',
        source='entity' and lives under its own `entities.*` subtree, so (a) a
        memory-content `under=` scope would zero out 100% of them, and (b)
        SEARCH_SCAFFOLDING_TYPES = ('entity','folder') means the bare type/nostub
        clause would too, unless the caller passed type='entity' -- which no caller
        filtering memories does. Worse: `entities` has no `path`/`source`/`type`/
        `tags`/`doc_kind`/`dead_at`/`event_at`/`source_created_at` column of its own
        and this arm never joins `nodes`, so interpolating the block here is a HARD
        SQL ERROR (`column "path" does not exist`), not a silent over-filter. A
        caller wanting a document facet to constrain which entities surface should
        intersect against the (filtered) memory arm or against bridge_frontier's own
        SEARCH_FILTERS.

        No `entity_type='class'` filter either. Restricting to ACTIVE concepts is
        what makes that safe: entity members of active concepts are, in practice,
        not classes. That is an emergent property of how
        consolidation populates concept_members, not something this primitive
        enforces -- if it regresses, the fix belongs in consolidation, and this
        primitive faithfully hands back whatever concept_members contains (proved by
        test: a manually-seeded class member IS returned, undropped).

        Unlimited by design -- no `pool`/`LIMIT` on either arm. Active-concept
        memory membership is small relative to the corpus, though it drifts upward
        as the corpus grows. It is not expected to approach a pool-sized cap, so a
        LIMIT here would be an unrequested truncation rather than a safety net.

        `ordinal` is a single dense row_number() per arm across ALL requested
        concept_ids -- NOT reset per concept. `(concept_id, node_id)` is the
        concept_members PK, so `ORDER BY cm.score DESC, cm.node_id, cm.concept_id`
        is fully deterministic even when one node is a member of two requested
        concepts (which yields two rows, one per membership).
        """
        if not concept_ids:
            return {"memories": [], "entities": []}
        mem_sql = f"""
            SELECT cm.node_id AS id, cm.concept_id, cm.score AS cm_score,
                   row_number() OVER (ORDER BY cm.score DESC, cm.node_id, cm.concept_id)
                       AS ordinal
            FROM concept_members cm
            JOIN concepts c ON c.id = cm.concept_id AND c.status = 'active'
            JOIN nodes ON nodes.id = cm.node_id
            WHERE cm.concept_id = ANY(%(ids)s::uuid[])
              AND cm.member_kind = 'memory'
              {SEARCH_FILTERS}
            ORDER BY ordinal
            """
        # No SEARCH_FILTERS here -- see the docstring; it would raise, not narrow.
        # c.status='active' is what keeps the measured 0%-classes invariant
        # load-bearing for THIS primitive regardless of what status a caller-supplied
        # concept_id happens to carry.
        ent_sql = """
            WITH sup AS (
                SELECT entity_id, count(DISTINCT memory_id) AS support
                FROM entity_links GROUP BY entity_id
            )
            SELECT cm.node_id AS entity_id, cm.concept_id, cm.score AS cm_score,
                   e.canonical_name AS name, coalesce(sup.support, 0)::int AS support,
                   row_number() OVER (ORDER BY cm.score DESC, cm.node_id, cm.concept_id)
                       AS ordinal
            FROM concept_members cm
            JOIN concepts c ON c.id = cm.concept_id AND c.status = 'active'
            JOIN entities e ON e.node_id = cm.node_id
            LEFT JOIN sup ON sup.entity_id = cm.node_id
            WHERE cm.concept_id = ANY(%(ids)s::uuid[])
              AND cm.member_kind = 'entity'
            ORDER BY ordinal
            """
        params = {"ids": [str(i) for i in concept_ids], "under": under,
                  "source": source, "type": type, "tags": tags, "kind": kind,
                  "since": since, "until": until, "inc_dead": include_dead,
                  "nostub": list(SEARCH_SCAFFOLDING_TYPES)}

        def _go(c):
            memories = [dict(r) for r in c.execute(mem_sql, params).fetchall()]
            entities = [dict(r) for r in c.execute(ent_sql, params).fetchall()]
            return {"memories": memories, "entities": entities}

        if conn is not None:
            return _go(conn)
        # This is the first candidate primitive that issues TWO data-returning
        # SELECTs per call. Under the pool's default READ COMMITTED each statement
        # would get its OWN snapshot, so a consolidation run landing between them
        # could return `memories` and `entities` from two points in time -- the exact
        # class of bug read_snapshot() exists to prevent, but here it is INSIDE one
        # primitive's own round trip, where the caller has no seam to opt in through.
        with self._pool.connection() as c:
            with c.transaction():
                c.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
                return _go(c)

    def bridge_frontier(self, entity_ids: "list", embedding: "list[float]", *,
                        fanout: int, under=None, source=None, type=None, tags=None,
                        kind=None, since=None, until=None, include_dead: bool = True,
                        conn=None) -> "list[dict]":
        """For each of `entity_ids`, its top-`fanout` mentioning memories by cosine
        to `embedding`: `[{memory_id, via_entity_id, via_entity_name, entity_support,
        similarity, ordinal}]`.

        `fanout` is REQUIRED, with no default, on purpose: this is the hub control,
        and a caller must state the cap it is choosing rather than inherit one.

        THE ONLY HUB CONTROL -- both predicates, applied together, neither alone:
          - `el.link_type = 'mentions'`          excludes belongs_to (class) edges
          - `e.entity_type = 'instance'`         excludes class entities structurally

        entity_links has no CHECK tying link_type to entity_type. Today they
        correlate 100% (mentions/instance, belongs_to/class, verified on a real
        corpus) but that is data, not a guarantee, which is why both predicates
        are written explicitly rather than relying on either implying the other.
        This also removes 100% of the NULL weights -- the NULLs are exactly the
        belongs_to rows -- and `entity_links.weight` is NEVER used for ordering or
        scoring here regardless: it is a deterministic function of link_type with a
        handful of exceptions, and weight-ordered selection measured ADVERSE.

        A statistical p90-support gate was designed and DELETED: measured, it is
        anti-SUBJECT, not anti-hub. It blocks the most-referenced subjects (a
        vault's main projects and tools) -- exactly what queries are about. See negatives.py.

        PER-ENTITY CAP SHAPE: a window function partitioned by entity_id, evaluated
        INSIDE the `ranked` CTE, before the outer ORDER BY/ordinal. PARTITION BY
        resets the counter per entity, so `rn <= fanout` keeps up to `fanout` rows
        for EVERY requested entity independently. A plain `ORDER BY similarity DESC
        LIMIT fanout` over the whole set would keep the globally-best rows and could
        starve a real but less-central entity entirely (proved by test). A cap
        ordered only by `id` would slice a random uuid range, since bridge score is
        constant across one entity's fan-out -- hence the cosine ordering.

        Cosine is computed directly against `nodes.embedding` (whole-note), NOT the
        nodes-union-node_chunks max-reduce shape knn_candidates uses. Deliberate:
        the candidate set is already bounded by the entity_links join (indexed on
        entity_id; an instance entity has tens of mentions at most, a handful on
        average), so this is an exact ranking over an already-small, already-scoped set,
        not a corpus-scale candidate-generation problem. No HNSW/iterative_scan
        handling is needed for the same reason -- no ANN index is consulted; the
        window sort runs over the join's materialized rows. NOTE for callers: this
        makes `similarity` here a MENU signal, not the same ruler `similarity_for`
        returns, and the two can differ for a chunk-matched note.

        `entity_support` is the same TOTAL distinct-memory count as
        concept_frontier's `support` and entity_profile's `mentions` -- not scoped to
        the fanout cap or to this call's other entity_ids -- so a caller can apply
        its own hub-damping and can disclose "took 8 of 40" without a second query.

        SEARCH_FILTERS applies to the memory side (nodes joined via el.memory_id):
        this primitive IS selecting memories, unlike concept_frontier's entity arm.

        There is no cap on the TOTAL row count, only the per-entity `fanout`, so
        rows scale as len(entity_ids) x fanout. Today's callers pass small entity
        sets (the crawl caps its own bridge seeds); a caller passing hundreds
        should cap upstream.
        """
        if not entity_ids:
            return []
        params = {
            "entity_ids": [str(i) for i in entity_ids],
            "vec": "[" + ",".join(repr(float(x)) for x in embedding) + "]",
            "fanout": fanout,
            "under": under, "source": source, "type": type, "tags": tags,
            "kind": kind, "since": since, "until": until, "inc_dead": include_dead,
            "nostub": list(SEARCH_SCAFFOLDING_TYPES),
        }
        sql = f"""
            WITH ranked AS (
                SELECT el.memory_id, el.entity_id, e.canonical_name AS via_entity_name,
                       1 - (nodes.embedding <=> %(vec)s::vector) AS similarity,
                       row_number() OVER (
                           PARTITION BY el.entity_id
                           ORDER BY nodes.embedding <=> %(vec)s::vector, nodes.id
                       ) AS rn
                FROM entity_links el
                JOIN entities e ON e.node_id = el.entity_id
                               AND e.entity_type = 'instance'
                JOIN nodes ON nodes.id = el.memory_id
                WHERE el.link_type = 'mentions'
                  AND el.entity_id = ANY(%(entity_ids)s::uuid[])
                  AND nodes.embedding IS NOT NULL
                  {SEARCH_FILTERS}
            ),
            sup AS (
                SELECT entity_id, count(DISTINCT memory_id) AS support
                FROM entity_links GROUP BY entity_id
            )
            SELECT ranked.memory_id, ranked.entity_id AS via_entity_id,
                   ranked.via_entity_name,
                   coalesce(sup.support, 0)::int AS entity_support,
                   ranked.similarity,
                   row_number() OVER (ORDER BY ranked.similarity DESC,
                                       ranked.entity_id, ranked.memory_id) AS ordinal
            FROM ranked
            LEFT JOIN sup ON sup.entity_id = ranked.entity_id
            WHERE ranked.rn <= %(fanout)s
            ORDER BY ordinal
            """
        if conn is not None:
            return [dict(r) for r in conn.execute(sql, params).fetchall()]
        with self._pool.connection() as c:
            return [dict(r) for r in c.execute(sql, params).fetchall()]

    # ---- write (edit) ------------------------------------------------------
    def update(self, id: UUID, patch: NodePatch) -> Node:
        """Read-merge-write, in ONE transaction on ONE connection, with the read taken
        `FOR UPDATE`. `move`/`delete`/`upsert` are all single-transaction already (this
        was the one outlier) -- the read used to be `self.get(id)` on its own connection
        and the write a second, separate one, so a field patch.summary=None field falls
        back to whatever `current` looked like AT READ TIME, not at write time. Two
        concurrent partial patches to the SAME node (A sets tags, B sets summary) used to
        both read the same pre-update snapshot and each write back the OTHER's field as
        stale -- a lost update. `FOR UPDATE` here serializes them: B's SELECT blocks until
        A's transaction commits, then reads A's already-written row, so B's merge folds
        its own patch onto A's result instead of clobbering it. Unreachable before MCP
        tools moved to worker threads; in-process serialisation on the event loop was
        the only guard before that.
        """
        with self._pool.connection() as conn, conn.transaction():
            row = conn.execute(
                f"SELECT {_COLUMNS} FROM nodes WHERE id = %s FOR UPDATE", (id,)
            ).fetchone()
            if row is None:
                raise LookupError(f"node not found: {id}")
            current = _row_to_node(row)
            # Same NUL rule as _persist_node: Postgres text cannot hold U+0000, so a patch
            # carrying one is stored with the visible ␀ instead of failing the write.
            title = sanitize_text(patch.title if patch.title is not None else current.title)
            content = sanitize_text(
                patch.content if patch.content is not None else current.content)
            summary = sanitize_text(
                patch.summary if patch.summary is not None else current.summary)
            uri = patch.uri if patch.uri is not None else current.uri
            tags = [sanitize_text(t) for t in
                    (patch.tags if patch.tags is not None else current.tags)]
            doc_kind = patch.doc_kind if patch.doc_kind is not None else current.doc_kind
            if not content or not content.strip():
                raise ValidationError("content must be non-empty")
            chash = content_hash(title, content)
            updated = conn.execute(
                f"""UPDATE nodes SET title=%(title)s, content=%(content)s, summary=%(summary)s,
                        uri=%(uri)s, tags=%(tags)s, doc_kind=%(doc_kind)s,
                        content_hash=%(chash)s, updated_at=now()
                    WHERE id=%(id)s RETURNING {_COLUMNS}""",
                {"id": id, "title": title, "content": content, "summary": summary,
                 "uri": uri, "tags": tags, "doc_kind": doc_kind, "chash": chash},
            ).fetchone()
        return _row_to_node(updated)

    def move(self, id: UUID, new_parent_path: str) -> Node:
        node = self.get(id)
        if node is None:
            raise LookupError(f"node not found: {id}")
        old_path = node.path
        new_path = join_path(new_parent_path, node.slug)
        if new_path == old_path or new_path.startswith(old_path + "."):
            raise ValueError(f"cannot move {old_path} into itself or its own subtree")
        with self._pool.connection() as conn, conn.transaction():
            old_excluded = self._effective_excluded(conn, old_path)
            new_parent_id = self._ensure_dirs(conn, new_parent_path)
            # The destination slug may already be taken by a sibling under new_parent; report that
            # as a PathConflict rather than leaking the underlying UniqueViolation.
            with _as_path_conflict(new_path):
                conn.execute(
                    """
                    UPDATE nodes
                    SET path = CASE WHEN path = %(old)s::ltree
                                    THEN %(new)s::ltree
                                    ELSE %(new)s::ltree || subpath(path, nlevel(%(old)s::ltree))
                               END,
                        parent_id = CASE WHEN id = %(id)s THEN %(pid)s::uuid ELSE parent_id END,
                        updated_at = now()
                    WHERE path <@ %(old)s::ltree
                    """,
                    {"new": new_path, "old": old_path, "id": id, "pid": new_parent_id},
                )
            # inherited depends on ancestors, which changed for the whole moved subtree.
            self._recompute_in_conn(conn, under=new_path)
            # Crossing an analysis_excluded boundary changes whether the subtree's notes may
            # influence the entity system; reconcile their influence accordingly.
            retracted = self._reconcile_exclusion_subtree(conn, new_path, old_excluded)
        if retracted:
            self.reconcile_instances()      # GC any instance left with zero support, post-commit
        return self.get(id)

    def delete(self, id: UUID, *, recursive: bool = False) -> int:
        node = self.get(id)
        if node is None:
            return 0
        where = "path <@ %(p)s::ltree" if recursive else "id = %(p)s"
        with self._pool.connection() as conn:
            # Log each deleted node so removals are observable (the symmetric counterpart
            # to node_created). rowcount of the INSERT == nodes deleted.
            cur = conn.execute(
                f"""WITH del AS (
                        DELETE FROM nodes WHERE {where}
                        RETURNING id, source, external_id, path::text AS path, content_hash
                    )
                    INSERT INTO ingest_log
                        (event, source, external_id, node_id, path, content_hash)
                    SELECT 'node_deleted', source, external_id, id, path, content_hash
                    FROM del""",
                {"p": node.path if recursive else id},
            )
            deleted = cur.rowcount
        # Deleting memories severs their entity links via cascade; collect any instance thereby
        # left with zero support so a delete doesn't leak orphaned entity nodes. Skipped when the
        # deleted node IS an entity (a different operation — never recreates the others).
        if deleted and node.source != ENTITY_SOURCE:
            self.reconcile_instances()
        return deleted

    # ---- reconciliation ----------------------------------------------------
    def external_ids_for_source(self, source: str) -> set[str]:
        with self._pool.connection() as conn:
            cur = conn.execute(
                "SELECT external_id FROM nodes WHERE source = %s", (source,)
            )
            return {r["external_id"] for r in cur.fetchall()}

    def prune(self, source: str, keep_external_ids: set[str]) -> int:
        """Delete nodes of `source` whose external_id isn't in keep, logging each as
        'node_deleted' so a mirror-prune is observable (not a silent burst of recreations).
        Returns count."""
        with self._pool.connection() as conn:
            cur = conn.execute(
                """WITH del AS (
                       DELETE FROM nodes
                       WHERE source = %(source)s AND external_id <> ALL(%(keep)s::text[])
                       RETURNING id, source, external_id, path::text AS path, content_hash
                   )
                   INSERT INTO ingest_log
                       (event, source, external_id, node_id, path, content_hash)
                   SELECT 'node_deleted', source, external_id, id, path, content_hash
                   FROM del""",
                {"source": source, "keep": list(keep_external_ids)},
            )
            return cur.rowcount

    def prune_empty_folders(self) -> int:
        """Remove folder nodes that hold no non-folder node anywhere beneath them — the
        symmetric counterpart to `mkdir -p`. Folders are created on demand only to contain
        notes (an empty vault directory never makes one), so once the last note under a
        folder is gone the folder is pure scaffolding and should disappear with it. Without
        this, deleting a vault directory's notes prunes the notes but strands the now-empty
        folder nodes in the tree.

        One predicate handles arbitrary nesting: a folder whose only descendants are other
        (empty) folders has no non-folder descendant either, so the whole dead subtree is
        removed in a single pass. Each removal is logged 'node_deleted'. The reserved entity
        root is preserved (reset_entities keeps it intentionally even when empty). Returns
        the number of folders removed.
        """
        with self._pool.connection() as conn:
            cur = conn.execute(
                """WITH del AS (
                       DELETE FROM nodes f
                       WHERE f.type = 'folder'
                         AND f.path <> %(eroot)s::ltree
                         AND NOT EXISTS (
                             SELECT 1 FROM nodes d
                             WHERE d.path <@ f.path AND d.type <> 'folder')
                       RETURNING id, source, external_id, path::text AS path, content_hash
                   )
                   INSERT INTO ingest_log
                       (event, source, external_id, node_id, path, content_hash)
                   SELECT 'node_deleted', source, external_id, id, path, content_hash
                   FROM del""",
                {"eroot": ENTITY_ROOT},
            )
            return cur.rowcount

    # ---- enrichment (two-phase) -------------------------------------------
    def flag_stale_enrichment(self, *, source: str | None = None) -> int:
        """Mark nodes whose content changed since last enrichment as 'pending'.

        Keys off content_hash (not updated_at, which advances on every re-upsert):
        a node needs (re)enrichment iff enriched_hash differs from content_hash.
        Already-'pending' nodes are left alone, and so are in-flight 'processing' ones —
        a claimed node's hashes stay distinct until its batch settles it (mark_enriched /
        mark_enrichment_failed pin enriched_hash), so re-flagging it here only falsified
        the observable state: every source re-sync knocked whole claimed batches back to
        'pending', and the dashboard read "N pending, 0 processing" mid-enrichment.
        Stranded 'processing' rows (daemon killed mid-batch) are requeued once at daemon
        boot instead — see requeue_stuck_processing. Returns rows flagged.
        """
        sql = ("UPDATE nodes SET enrichment_status = 'pending' "
               "WHERE enriched_hash IS DISTINCT FROM content_hash "
               "AND enrichment_status NOT IN ('pending', 'processing')")
        params: dict = {}
        if source is not None:
            sql += " AND source = %(source)s"
            params["source"] = source
        with self._pool.connection() as conn:
            return conn.execute(sql, params).rowcount

    def has_pending_enrichment(self) -> bool:
        """True iff any node sits at enrichment_status='pending' — the enrichment worker's
        drain signal (one cheap EXISTS; runs after every batch and after ingest syncs)."""
        with self._pool.connection() as conn:
            return conn.execute(
                "SELECT EXISTS(SELECT 1 FROM nodes WHERE enrichment_status = 'pending') "
                "AS more").fetchone()["more"]

    def requeue_stuck_processing(self) -> int:
        """Requeue nodes stranded at 'processing' back to 'pending'. Returns rows moved.

        A daemon killed mid-batch leaves its claimed nodes at 'processing' with nobody
        coming back for them, and flag_stale_enrichment deliberately skips that status.
        The ingest daemon calls this once at boot — the only safe moment, since it is the
        single claimer and no batch can be in flight before its scheduler starts.
        """
        with self._pool.connection() as conn:
            return conn.execute(
                "UPDATE nodes SET enrichment_status = 'pending' "
                "WHERE enrichment_status = 'processing'").rowcount

    def release_enrichment_claim(self, id: UUID) -> None:
        """Hand one claimed node back to 'pending', untouched and unlogged. Used when a
        provider outage ends a batch before this node was started: nothing was attempted,
        so there is nothing to record (contrast defer_enrichment, for the node that hit
        the outage)."""
        with self._pool.connection() as conn:
            conn.execute("UPDATE nodes SET enrichment_status = 'pending' "
                         "WHERE id = %s AND enrichment_status = 'processing'", (id,))

    def claim_enrichment(self, limit: int = 10) -> "list[Node]":
        """Atomically move up to `limit` pending nodes to 'processing' and return them.

        Flip-then-process (not a held FOR UPDATE) so slow enrichers don't hold row locks.
        The worker processes a claimed batch serially, so a smaller `limit` just means fewer
        nodes parked in 'processing' per sweep (tunable via KB_ENRICH_BATCH at the daemon).
        """
        with self._pool.connection() as conn:
            rows = conn.execute(
                f"""UPDATE nodes SET enrichment_status = 'processing'
                    WHERE id IN (
                        SELECT id FROM nodes WHERE enrichment_status = 'pending'
                        ORDER BY ingested_at LIMIT %(limit)s
                        FOR UPDATE SKIP LOCKED
                    )
                    RETURNING {_COLUMNS}""",
                {"limit": limit},
            ).fetchall()
        return [_row_to_node(r) for r in rows]

    def mark_enriched(self, id: UUID, *, detail: "dict | None" = None,
                      class_hash: "str | None" = None,
                      kind_hash: "str | None" = None,
                      gate: bool = False) -> None:
        # Flip to 'done' and log the completion atomically: the CTE only emits an
        # ingest_log row if the node actually existed (empty upd -> no log row).
        # `detail` (e.g. {"providers": [...]} — which provider:model backends served
        # this enrichment) lands on the log row; None keeps the column's usual {} shape.
        # Dependency cursors and the gate clock advance only for reported executed passes.
        with self._pool.connection() as conn:
            conn.execute(
                """WITH upd AS (
                       UPDATE nodes SET enrichment_status = 'done', enriched_at = now(),
                              enriched_hash = content_hash, last_analyzed_at = now(),
                              last_gate_analyzed_at = CASE WHEN %(gate)s THEN now()
                                                           ELSE last_gate_analyzed_at END,
                              analyzed_class_hash = COALESCE(%(ch)s, analyzed_class_hash),
                              analyzed_kind_hash = COALESCE(%(kh)s, analyzed_kind_hash),
                              attempted_class_hash = CASE WHEN %(ch)s IS NOT NULL
                                                          THEN NULL
                                                          ELSE attempted_class_hash END,
                              attempted_kind_hash = CASE WHEN %(kh)s IS NOT NULL
                                                         THEN NULL
                                                         ELSE attempted_kind_hash END,
                              recrawl_cursor_adoption_pending = false
                       WHERE id = %(id)s
                       RETURNING id, source, external_id, path::text AS path, content_hash
                   )
                   INSERT INTO ingest_log
                       (event, source, external_id, node_id, path, content_hash, detail)
                   SELECT 'enrichment_done', source, external_id, id, path, content_hash,
                          %(detail)s
                   FROM upd""",
                {"id": id, "ch": class_hash, "kh": kind_hash, "gate": gate,
                 "detail": Jsonb(detail if detail is not None else {})},
            )

    def set_embedding(self, id: UUID, vector: "list[float]", *, embedded_hash: str) -> None:
        """Store a node's embedding and the hash of the text it was built from.

        The vector is sent as a pgvector literal ('[v1,v2,…]') and cast ::vector, so we avoid
        pulling in the pgvector Python adapter. `embedded_hash` lets EmbeddingEnricher skip
        re-embedding unchanged text. A derived field — separate from NodePatch (the user-facing
        text-edit path)."""
        literal = "[" + ",".join(repr(float(x)) for x in vector) + "]"
        with self._pool.connection() as conn:
            conn.execute(
                "UPDATE nodes SET embedding = %(v)s::vector, embedded_hash = %(h)s "
                "WHERE id = %(id)s",
                {"v": literal, "h": embedded_hash, "id": id},
            )

    def set_chunk_embeddings(self, id: UUID,
                             chunks: "list[tuple[int, int, list[float]]]") -> None:
        """Replace a node's chunk vectors (derived data — see migration 0033).

        `chunks` is [(char_start, char_end, vector), …]; an empty list clears them
        (a note that shrank below the chunking threshold). Same pgvector-literal
        convention as set_embedding."""
        with self._pool.connection() as conn, conn.transaction():
            conn.execute("DELETE FROM node_chunks WHERE node_id = %s", (id,))
            for seq, (start, end, vector) in enumerate(chunks):
                literal = "[" + ",".join(repr(float(x)) for x in vector) + "]"
                conn.execute(
                    """INSERT INTO node_chunks
                           (node_id, seq, char_start, char_end, embedding)
                       VALUES (%(id)s, %(seq)s, %(s)s, %(e)s, %(v)s::vector)""",
                    {"id": id, "seq": seq, "s": start, "e": end, "v": literal})

    def chunk_count(self, id: UUID) -> int:
        with self._pool.connection() as conn:
            return conn.execute(
                "SELECT count(*) AS n FROM node_chunks WHERE node_id = %s",
                (id,)).fetchone()["n"]

    def unembedded_nodes(self, limit: int = 100) -> "list[Node]":
        """Nodes with no embedding yet (embedding IS NULL), oldest first — for backfill."""
        with self._pool.connection() as conn:
            rows = conn.execute(
                f"SELECT {_COLUMNS} FROM nodes WHERE embedding IS NULL "
                "ORDER BY ingested_at LIMIT %(limit)s",
                {"limit": limit},
            ).fetchall()
        return [_row_to_node(r) for r in rows]

    def mark_enrichment_failed(self, id: UUID, *, error: str | None = None) -> None:
        # Pin enriched_hash to current content so identical content isn't retried in a
        # loop; a later content change makes the hash distinct -> re-flagged 'pending'.
        # Stamp last_analyzed_at too, so a failing node moves to the back of the crawl.
        # Log 'enrichment_failed' atomically with the flip (symmetric to mark_enriched),
        # carrying the reason in `detail` so a failure is visible — and explainable — in
        # the ingest log / dashboard, not just an opaque bump in the 'failed' status count.
        with self._pool.connection() as conn:
            conn.execute(
                """WITH upd AS (
                       UPDATE nodes SET enrichment_status = 'failed',
                              enriched_hash = content_hash, last_analyzed_at = now()
                       WHERE id = %(id)s
                       RETURNING id, source, external_id, path::text AS path, content_hash
                   )
                   INSERT INTO ingest_log
                       (event, source, external_id, node_id, path, content_hash, detail)
                   SELECT 'enrichment_failed', source, external_id, id, path, content_hash,
                          %(detail)s
                   FROM upd""",
                {"id": id, "detail": Jsonb({"error": error} if error else {})},
            )

    def defer_enrichment(self, id: UUID, *, provider: str,
                         until: "datetime | None" = None, reason: str = "") -> None:
        """Return a claimed node to the queue because its PROVIDER is down — the
        transient counterpart of mark_enrichment_failed. Status goes back to
        'pending' with enriched_hash untouched (the worker claims purely by status,
        so the node is re-attempted as soon as the lane resumes), the deferral is
        logged so the node's timeline stays honest, and the provider-wide pause is
        recorded so the enrich lane stops claiming (see pause_provider). Deliberately
        does NOT stamp last_analyzed_at: deferral says nothing about the document."""
        with self._pool.connection() as conn:
            conn.execute(
                """WITH upd AS (
                       UPDATE nodes SET enrichment_status = 'pending'
                       WHERE id = %(id)s
                       RETURNING id, source, external_id, path::text AS path, content_hash
                   )
                   INSERT INTO ingest_log
                       (event, source, external_id, node_id, path, content_hash, detail)
                   SELECT 'enrichment_deferred', source, external_id, id, path,
                          content_hash, %(detail)s
                   FROM upd""",
                {"id": id,
                 "detail": Jsonb({"provider": provider, "error": reason,
                                  "until": until.isoformat() if until else None})})
        self.pause_provider(provider, until=until, reason=reason)

    def retry_failed_enrichment(
        self, *, source: str | None = None, node_ids: "list[UUID] | None" = None
    ) -> int:
        """Refresh the failure pile: move 'failed' enrichments back to 'pending' so the
        worker re-attempts them on its next tick. Returns the number re-queued.

        A failure is otherwise terminal — mark_enrichment_failed pins enriched_hash to
        content_hash so unchanged content isn't retried in a hot loop. That's right for a
        permanently-bad document but wrong after a *transient* outage (e.g. the LLM backend
        was down), which strands a pile of nodes at 'failed'. This is the manual lever to
        re-drive that pile once the cause is fixed. `source` scopes it broadly; `node_ids`
        scopes it precisely — the dashboard passes the subset whose error class is actually
        transient, so structural failures (e.g. a duplicate-slug violation) aren't re-queued
        into a loop they can't escape. Re-queuing leaves enriched_hash untouched — the worker
        claims purely by status, not by hash.
        """
        sql = ("UPDATE nodes SET enrichment_status = 'pending' "
               "WHERE enrichment_status = 'failed'")
        params: dict = {}
        if source is not None:
            sql += " AND source = %(source)s"
            params["source"] = source
        if node_ids is not None:
            # Empty set => nothing to do; short-circuit so we don't re-queue everything.
            if not node_ids:
                return 0
            sql += " AND id = ANY(%(ids)s)"
            params["ids"] = list(node_ids)
        with self._pool.connection() as conn:
            return conn.execute(sql, params).rowcount

    # A pause with no provider-stated reset re-probes on this interval: long enough
    # that a dead provider isn't hammered, short enough that recovery after an
    # interactive /login is noticed within minutes.
    _DEFAULT_PROVIDER_PAUSE = timedelta(minutes=15)

    def pause_provider(self, provider: str, *, until: "datetime | None" = None,
                       reason: str = "") -> None:
        """Record that `provider` is refusing calls until `until` (default: the
        re-probe interval from now). GREATEST keeps the LATER of old and new `until`,
        so concurrent worker threads deferring the same outage can never shorten a
        pause already in force."""
        with self._pool.connection() as conn:
            conn.execute(
                """INSERT INTO provider_pause (provider, until, reason)
                   VALUES (%(p)s, coalesce(%(u)s, now() + %(dflt)s), %(r)s)
                   ON CONFLICT (provider) DO UPDATE SET
                       until = GREATEST(provider_pause.until, EXCLUDED.until),
                       reason = EXCLUDED.reason, updated_at = now()""",
                {"p": provider, "u": until, "dflt": self._DEFAULT_PROVIDER_PAUSE,
                 "r": reason})

    def provider_paused(self, provider: str) -> "datetime | None":
        """The pause expiry for `provider` if one is in force, else None. Expired rows
        are dead by definition — nothing deletes them; the next pause overwrites."""
        with self._pool.connection() as conn:
            row = conn.execute(
                "SELECT until FROM provider_pause WHERE provider = %s AND until > now()",
                (provider,)).fetchone()
        return row["until"] if row else None

    def active_pauses(self) -> "list[dict]":
        """Unexpired provider pauses — for the healthcheck and /api/ingest/status."""
        with self._pool.connection() as conn:
            return conn.execute(
                "SELECT provider, until, reason FROM provider_pause "
                "WHERE until > now() ORDER BY provider").fetchall()

    # ---- recrawl dependency cursors ---------------------------------------
    def active_class_hash(self) -> str:
        """Digest of the ACTIVE entity-class vocabulary — the only input that can change
        what `extract_entities` assigns (a no-active-fit name is kept UNTYPED until a type
        activates). Not-yet-active candidate names are deliberately excluded: their vote
        counts shift on nearly every recrawl, so including them would leave this hash
        almost never stable and recover none of the savings."""
        with self._pool.connection() as conn:
            rows = conn.execute(
                "SELECT canonical_name, COALESCE(description, '') AS description "
                "FROM entities WHERE entity_type = %(t)s AND status = 'active'",
                {"t": ENTITY_CLASS}).fetchall()
        return _vocab_digest([(r["canonical_name"], r["description"]) for r in rows])

    def doc_kind_vocab_hash(self) -> str:
        """Digest of the doc-kind vocabulary offered to the `doc_kind` pass — the same list
        `doc_kinds_for_prompt` injects, hashed as a set so rank drift is invisible."""
        return _vocab_digest([(kind, "") for kind in self.doc_kinds_for_prompt()])

    def due_for_reanalysis(self, sources: "list[str]", *, class_hash: str,
                           kind_hash: str,
                           limit: int = 10) -> "list[tuple[Node, DueReasons]]":
        """The nodes due for re-analysis, each paired with the reasons it is due.

        Only first-pass-enriched nodes ('done') are crawlable, so recrawl never races the
        ingest pipeline on a node it is still enriching. This reads enrichment_status as
        that gate but never writes it (mark_analyzed advances only the cursors).

        A node is due when an input that can change a recrawl pass's ANSWER has moved:
          - analyzed_class_hash != the active class vocabulary  -> extract_entities;
          - analyzed_kind_hash  != the doc-kind vocabulary      -> doc_kind;
          - last_gate_analyzed_at older than REANALYZE_GATE_INTERVAL_DAYS -> entity_gate.

        A failed vocabulary pass records the hash it attempted without advancing the analyzed
        cursor. That suppresses an identical poisoned retry every tick, while a later hash move
        makes the node due again. The gate pass has its own last_gate_analyzed_at clock, so
        unrelated class/kind work cannot postpone its date-driven review.

        A node's own CONTENT is deliberately not consulted: a content change is handled by
        the enrichment lane (flag_stale_enrichment's enriched_hash gate), which re-runs the
        full pipeline. Recrawl exists only for vocabulary convergence.

        This replaced a single corpus-wide watermark (max(at) over ingest_log's
        node_created/node_updated) that was uncorrelated to the node under test, so one new
        node anywhere re-queued the whole corpus -- and since promotion's entity nodes are
        themselves node_created events fed by this very crawl's extract_entities pass, the
        crawl manufactured the events that made it due and never idled.
        """
        if not sources:
            return []
        params = {"sources": list(sources), "limit": limit,
                  "ch": class_hash, "kh": kind_hash,
                  "gate_days": REANALYZE_GATE_INTERVAL_DAYS}
        with self._pool.connection() as conn:
            rows = conn.execute(
                f"""SELECT {_COLUMNS},
                           (analyzed_class_hash IS DISTINCT FROM %(ch)s
                            AND attempted_class_hash IS DISTINCT FROM %(ch)s) AS due_class,
                           (analyzed_kind_hash IS DISTINCT FROM %(kh)s
                            AND attempted_kind_hash IS DISTINCT FROM %(kh)s) AS due_kind,
                           (last_gate_analyzed_at IS NULL
                            OR last_gate_analyzed_at
                               < now() - make_interval(days => %(gate_days)s)) AS due_gate
                    FROM nodes
                    WHERE source = ANY(%(sources)s)
                      AND enrichment_status = 'done'
                      AND dead_at IS NULL          -- retired memories are frozen
                      AND NOT EXISTS (SELECT 1 FROM nodes a
                                      WHERE a.analysis_excluded AND nodes.path <@ a.path)
                      AND ((analyzed_class_hash IS DISTINCT FROM %(ch)s
                            AND attempted_class_hash IS DISTINCT FROM %(ch)s)
                           OR (analyzed_kind_hash IS DISTINCT FROM %(kh)s
                               AND attempted_kind_hash IS DISTINCT FROM %(kh)s)
                           OR last_gate_analyzed_at IS NULL
                           OR last_gate_analyzed_at
                              < now() - make_interval(days => %(gate_days)s))
                    ORDER BY last_analyzed_at ASC NULLS FIRST, ingested_at ASC
                    LIMIT %(limit)s""",
                params,
            ).fetchall()
        return [(_row_to_node(r), DueReasons(due_class=r["due_class"],
                                             due_kind=r["due_kind"],
                                             due_gate=r["due_gate"])) for r in rows]

    def adopt_vocabulary_cursors(self, *, class_hash: str, kind_hash: str) -> int:
        """Stamp the current vocabulary onto nodes that were analyzed before the cursors
        existed. Returns rows stamped.

        Called once at daemon boot. Without it every pre-migration node reads as due on the
        first tick and the whole corpus is re-swept (every note through every recrawl pass) to
        re-derive answers it already holds -- those nodes are assumed to have been analyzed
        under this same vocabulary.
        Migration 0046 marks that exact legacy population. New enrichment rows are never
        eligible for adoption, so a restart cannot swallow a vocabulary change they did not
        see. Existing non-NULL cursors are preserved independently. Re-running is a no-op.
        """
        with self._pool.connection() as conn:
            return conn.execute(
                """UPDATE nodes
                   SET analyzed_class_hash = COALESCE(analyzed_class_hash, %(ch)s),
                       analyzed_kind_hash = COALESCE(analyzed_kind_hash, %(kh)s),
                       recrawl_cursor_adoption_pending = false
                   WHERE recrawl_cursor_adoption_pending""",
                {"ch": class_hash, "kh": kind_hash}).rowcount

    def recrawl_fingerprint(self, node_id: UUID) -> tuple:
        """What a recrawl could change about a node — its kind, gate state, and staged
        candidate set — as a comparable value.

        LLMEnricher deletes and re-stages a memory's candidates on every run, so a no-op
        recrawl and a productive one leave identical rows with identical timestamps. Without
        this there is no signal at all for whether a sweep earned its tokens.
        """
        with self._pool.connection() as conn:
            row = conn.execute(
                "SELECT doc_kind, entity_eligible, dead_at FROM nodes WHERE id = %s",
                               (node_id,)).fetchone()
            cands = conn.execute(
                "SELECT entity_type, normalized_name FROM entity_candidates "
                "WHERE memory_id = %s", (node_id,)).fetchall()
        return ((row["doc_kind"], row["entity_eligible"], row["dead_at"]) if row else None,
                frozenset((c["entity_type"], c["normalized_name"]) for c in cands))

    def mark_analyzed(self, id: UUID, *, class_hash: "str | None" = None,
                      kind_hash: "str | None" = None, gate: bool = False,
                      attempted_class_hash: "str | None" = None,
                      attempted_kind_hash: "str | None" = None) -> None:
        """Advance the re-analysis cursors for a node. Deliberately does NOT touch
        enrichment_status — re-analysis is decoupled from the ingest state. Also projects
        the recrawl completion into node_processing (audit: last_done_at/run_count).

        Each analyzed hash is stamped ONLY when its pass actually ran on this node. A requested
        vocabulary pass skipped by the enricher is recorded through attempted_*_hash instead,
        providing backoff without claiming convergence. The independent gate clock advances
        only when `gate` is true. Failures go through mark_analysis_failed.
        """
        sets = ["last_analyzed_at = now()", "recrawl_cursor_adoption_pending = false"]
        params: dict = {"id": id}
        if class_hash is not None:
            sets.append("analyzed_class_hash = %(ch)s")
            sets.append("attempted_class_hash = NULL")
            params["ch"] = class_hash
        if kind_hash is not None:
            sets.append("analyzed_kind_hash = %(kh)s")
            sets.append("attempted_kind_hash = NULL")
            params["kh"] = kind_hash
        if attempted_class_hash is not None:
            sets.append("attempted_class_hash = %(ach)s")
            params["ach"] = attempted_class_hash
        if attempted_kind_hash is not None:
            sets.append("attempted_kind_hash = %(akh)s")
            params["akh"] = attempted_kind_hash
        if gate:
            sets.append("last_gate_analyzed_at = now()")
        with self._pool.connection() as conn:
            conn.execute(f"UPDATE nodes SET {', '.join(sets)} WHERE id = %(id)s", params)
            conn.execute(
                """INSERT INTO node_processing
                       (node_id, task, status, queued_at, last_done_at, run_count, updated_at)
                   VALUES (%s, 'recrawl', 'done', now(), now(), 1, now())
                   ON CONFLICT (node_id, task) DO UPDATE SET
                       status = 'done', last_done_at = now(), updated_at = now(),
                       run_count = node_processing.run_count + 1""",
                (id,),
            )

    def mark_analysis_failed(self, id: UUID, *, class_hash: "str | None" = None,
                             kind_hash: "str | None" = None,
                             gate: bool = False) -> None:
        """Record a failed recrawl attempt without claiming convergence.

        The attempted hashes suppress a hot retry under the identical vocabulary. The
        analyzed hashes stay untouched, so the node becomes due again when that dependency
        moves. When the gate was requested, its independent clock provides date-driven backoff.
        """
        sets = ["last_analyzed_at = now()", "recrawl_cursor_adoption_pending = false"]
        params: dict = {"id": id}
        if class_hash is not None:
            sets.append("attempted_class_hash = %(ch)s")
            params["ch"] = class_hash
        if kind_hash is not None:
            sets.append("attempted_kind_hash = %(kh)s")
            params["kh"] = kind_hash
        if gate:
            sets.append("last_gate_analyzed_at = now()")
        with self._pool.connection() as conn:
            conn.execute(f"UPDATE nodes SET {', '.join(sets)} WHERE id = %(id)s", params)
            conn.execute(
                """INSERT INTO node_processing
                       (node_id, task, status, queued_at, last_done_at, run_count, updated_at)
                   VALUES (%s, 'recrawl', 'done', now(), now(), 1, now())
                   ON CONFLICT (node_id, task) DO UPDATE SET
                       status = 'done', last_done_at = now(), updated_at = now(),
                       run_count = node_processing.run_count + 1""",
                (id,),
            )

    def crawlable_node(self, id: UUID, sources: "list[str]") -> "Node | None":
        """Fetch one node by id, but only if it is crawlable for re-analysis: first-pass-
        enriched ('done'), not retired, and from a covered source. Backs the MANUAL re-crawl
        trigger, which targets a single node directly — the same crawlable gate as
        due_for_reanalysis, minus every dependency cursor (the user asked for it explicitly,
        so freshness is irrelevant). Returns None when the node is gone or not crawlable, so
        the worker quietly skips it."""
        if not sources:
            return None
        with self._pool.connection() as conn:
            row = conn.execute(
                f"""SELECT {_COLUMNS} FROM nodes
                    WHERE id = %(id)s
                      AND source = ANY(%(sources)s)
                      AND enrichment_status = 'done'
                      AND dead_at IS NULL
                      AND NOT EXISTS (SELECT 1 FROM nodes a
                                      WHERE a.analysis_excluded AND nodes.path <@ a.path)""",
                {"id": id, "sources": list(sources)},
            ).fetchone()
        return _row_to_node(row) if row else None

    # -- MCP retrieval traces (dashboard Retrieval page + offline evals) -----------

    def log_trace(self, *, tool: str, args: dict, latency_ms: int,
                  request_id: "UUID | None" = None,
                  session_id: "str | None" = None, caller: "str | None" = None,
                  query: "str | None" = None, target_id: "UUID | None" = None,
                  results: "list[dict] | None" = None, result_count: int = 0,
                  confidence_level: "str | None" = None,
                  best_similarity: "float | None" = None,
                  strategy: "str | None" = None, judge: "str | None" = None,
                  crawl_mode: "str | None" = None, trace: "dict | None" = None,
                  llm_calls: int = 0, embedder_ok: bool = True,
                  result_bytes: "int | None" = None,
                  replay: "dict | None" = None,
                  error: "str | None" = None) -> None:
        """Append one trace row. Callers (KB_MCP's trace seam) wrap this in
        try/except: tracing is advisory and must never break a tool call."""
        with self._pool.connection() as conn:
            # ONE transaction (the pool has no autocommit): lock, lookup, insert.
            # The clock is clock_timestamp(), NOT now(): now() is the transaction's
            # BEGIN time, and a thread can BEGIN before another thread takes the
            # lock, so a later lock-holder could record an EARLIER `at` than the
            # opener it joined -- the batch rule would then disagree with this one.
            # clock_timestamp() is read after the lock is held, so within a session
            # `at` order == lock order == id order.
            # The clock is read ONCE and used by both the lookup and the stored
            # `at`, so the incremental and batch rules see the same instant.
            # This write sits on the tool's synchronous response path, so every lock
            # wait in it is bounded: past 2 s (behind the advisory lock below, or a
            # kb-turns-rebuild table lock) it raises into KB_MCP's advisory trace
            # guard -- a warning, and the call still answers. First statement, so
            # psycopg's implicit BEGIN precedes it and SET LOCAL scopes to this write.
            conn.execute("SET LOCAL lock_timeout = '2s'")
            # ROW EXCLUSIVE is the lock INSERT takes anyway, so it never blocks other
            # writers or readers. Taking it BEFORE the lookup means a running
            # mcp_trace_rebuild_turns (which holds EXCLUSIVE) makes this write wait
            # until the rebuild commits, and a rebuild that starts after this write
            # holds the table waits for this write to commit -- so a write can never
            # read a pre-rebuild turn_id and land it after the rebuild. The 2 s
            # lock_timeout above bounds the wait; a timeout falls into the advisory guard.
            conn.execute("LOCK TABLE mcp_trace IN ROW EXCLUSIVE MODE")
            turn_id = None
            if session_id is not None:
                # Serialise writes within THIS session only. Without it two calls of
                # a parallel burst (0 s apart) each see no predecessor and open two
                # turns for one request. Released at commit, held ~1 ms. Two-key
                # form so the session hash can never collide with a single-key lock
                # such as hashtext('kb_consolidate').
                conn.execute(
                    "SELECT pg_advisory_xact_lock(%(ns)s::int, hashtext(%(sid)s))",
                    {"ns": _TURN_LOCK_NAMESPACE, "sid": session_id})
            at = conn.execute("SELECT clock_timestamp() AS t").fetchone()["t"]
            if session_id is not None:
                prev = conn.execute(
                    """SELECT turn_id FROM mcp_trace
                       WHERE session_id = %(sid)s
                         AND at >= %(at)s::timestamptz
                                   - %(latency_ms)s::int * interval '1 millisecond'
                                   - %(gap)s::int * interval '1 second'
                       ORDER BY at DESC, id DESC
                       LIMIT 1""",
                    {"sid": session_id, "at": at, "latency_ms": latency_ms,
                     "gap": TURN_GAP_S},
                ).fetchone()
                if prev is not None:
                    turn_id = prev["turn_id"]
            # Mint the id first so the opener's turn_id can be its own id in ONE
            # statement: NOT NULL holds at insert and a writer that forgets turn_id
            # fails loudly instead of silently self-grouping.
            conn.execute(
                """WITH n AS (SELECT nextval('mcp_trace_id_seq') AS id)
                   INSERT INTO mcp_trace
                       (id, at, turn_id, request_id, session_id, caller, tool, args,
                        query, target_id, latency_ms, results, result_count,
                        confidence_level, best_similarity, strategy, judge,
                        crawl_mode, trace, llm_calls, embedder_ok, result_bytes,
                        replay, error)
                   OVERRIDING SYSTEM VALUE
                   SELECT n.id, %(at)s, coalesce(%(turn_id)s::bigint, n.id),
                          coalesce(%(request_id)s, gen_random_uuid()),
                          %(session_id)s, %(caller)s, %(tool)s, %(args)s,
                          %(query)s, %(target_id)s, %(latency_ms)s, %(results)s,
                          %(result_count)s, %(confidence_level)s,
                          %(best_similarity)s, %(strategy)s, %(judge)s,
                          %(crawl_mode)s, %(trace)s, %(llm_calls)s,
                          %(embedder_ok)s, %(result_bytes)s, %(replay)s,
                          %(error)s
                   FROM n""",
                {"at": at, "turn_id": turn_id, "request_id": request_id,
                 "session_id": session_id, "caller": caller, "tool": tool,
                 "args": Jsonb(args), "query": query, "target_id": target_id,
                 "latency_ms": latency_ms,
                 "results": Jsonb(results) if results is not None else None,
                 "result_count": result_count,
                 "confidence_level": confidence_level,
                 "best_similarity": best_similarity, "strategy": strategy,
                 "judge": judge, "crawl_mode": crawl_mode,
                 "trace": Jsonb(trace) if trace is not None else None,
                 "llm_calls": llm_calls, "embedder_ok": embedder_ok,
                 "result_bytes": result_bytes,
                 "replay": Jsonb(replay) if replay is not None else None,
                 "error": error},
            )

    def rebuild_turns(self) -> int:
        """Re-derive every row's turn_id with the batch rule
        (mcp_trace_rebuild_turns, from 0048) at the gap in force. Returns the
        number of rows whose turn_id changed; 0 means history already agrees with
        TURN_GAP_S. Idempotent. Run via `kb-turns-rebuild` after retuning the
        constant -- the write path applies the new value going forward on its own.
        The rebuild takes an EXCLUSIVE lock on mcp_trace for its transaction: reads
        continue, and live log_trace writes wait for it rather than race it (so a
        write can never inherit a pre-rebuild turn_id)."""
        with self._pool.connection() as conn:
            return conn.execute(
                "SELECT mcp_trace_rebuild_turns(%(gap)s::int) AS moved",
                {"gap": TURN_GAP_S},
            ).fetchone()["moved"]

    def trace_recent(self, *, limit: int = 50, tool: "str | None" = None,
                     confidence: "str | None" = None,
                     judged: "bool | None" = None,
                     include_evals: bool = False,
                     turn_id: "int | None" = None) -> "list[dict]":
        """Newest trace rows first, scalar columns only -- args/results/trace
        payloads stay behind trace_get so the list view doesn't pay for them.
        `judgment` carries the latest verdict across judges, None if unjudged.
        Eval-harness traffic (caller set) is hidden unless include_evals.
        `turn_id` scopes the list to one turn's calls (still newest first)."""
        with self._pool.connection() as conn:
            rows = conn.execute(
                """SELECT t.id, t.at, t.request_id, t.session_id, t.turn_id,
                          t.caller, t.tool, t.query, t.target_id, t.latency_ms,
                          t.result_count, t.confidence_level, t.best_similarity,
                          t.strategy, t.judge, t.crawl_mode, t.llm_calls,
                          t.embedder_ok, t.result_bytes, t.error,
                          (t.replay IS NOT NULL) AS has_replay,
                          j.verdict AS judgment
                   FROM mcp_trace t
                   LEFT JOIN LATERAL (
                       SELECT verdict FROM retrieval_judgment
                       WHERE trace_id = t.id
                       ORDER BY at DESC, id DESC LIMIT 1
                   ) j ON true
                   WHERE (%(tool)s::text IS NULL OR t.tool = %(tool)s)
                     AND (%(confidence)s::text IS NULL
                          OR t.confidence_level = %(confidence)s)
                     AND (%(judged)s::boolean IS NULL
                          OR (j.verdict IS NOT NULL) = %(judged)s)
                     AND (%(include_evals)s OR t.caller IS NULL)
                     AND (%(turn_id)s::bigint IS NULL OR t.turn_id = %(turn_id)s)
                   ORDER BY t.at DESC, t.id DESC
                   LIMIT %(limit)s""",
                {"limit": limit, "tool": tool, "confidence": confidence,
                 "judged": judged, "include_evals": include_evals,
                 "turn_id": turn_id},
            ).fetchall()
        for r in rows:
            r["result_tokens"] = _est_tokens(r["result_bytes"])
        return rows

    def trace_turns(self, *, limit: int = 50, tool: "str | None" = None,
                    confidence: "str | None" = None, judged: "bool | None" = None,
                    include_evals: bool = False) -> "list[dict]":
        """One row per turn -- the calls one agent made for one user request --
        newest ended_at first (a turn still receiving calls floats to the top).
        `tool`/`confidence` keep turns CONTAINING a matching call; `judged` applies to
        turn verdicts; eval turns (opener's caller set) are hidden unless
        include_evals. `tools` is {tool: count}; `first_query` is the opener's query
        or target id; `result_tokens` derives from summed bytes over measured calls
        and is None when none was measured (never 0); `judgment` is the latest turn
        verdict across judges."""
        with self._pool.connection() as conn:
            rows = conn.execute(_TURNS_SQL, {
                "limit": limit, "tool": tool, "confidence": confidence,
                "judged": judged, "include_evals": include_evals, "turn_id": None,
            }).fetchall()
        for r in rows:
            r["result_tokens"] = _est_tokens(r["result_bytes"])
        return rows

    def trace_turn(self, id: int) -> "dict | None":
        """One turn with its calls and judgments: the trace_turns row (eval turns
        included -- a known id resolves regardless), `traces` = that turn's
        trace_recent rows oldest first (so the drill-down reads as a timeline and
        reuses the call row shape), `judgments` newest first. None unless `id` is an
        opener: a follower's id is not a turn."""
        with self._pool.connection() as conn:
            rows = conn.execute(_TURNS_SQL, {
                "limit": 1, "tool": None, "confidence": None, "judged": None,
                "include_evals": True, "turn_id": id,
            }).fetchall()
            if not rows:
                return None
            turn = rows[0]
            judgments = conn.execute(
                """SELECT id, verdict, judged_by, notes, at
                   FROM turn_judgment WHERE turn_id = %(id)s
                   ORDER BY at DESC, id DESC""", {"id": id},
            ).fetchall()
        turn["result_tokens"] = _est_tokens(turn["result_bytes"])
        # trace_recent is newest first; a turn reads as a timeline. 1000 caps a
        # pathological turn (real turns are a few dozen calls at most).
        turn["traces"] = list(reversed(
            self.trace_recent(limit=1000, include_evals=True, turn_id=id)))
        turn["judgments"] = judgments
        return turn

    def trace_summary(self, *, days: int = 7, include_evals: bool = False) -> dict:
        """Aggregates over the trailing window: volume, latency percentiles,
        confidence distribution, degradation and error counts, plus
        followup_rate -- of the window's turns that contain a search, the share
        where the same turn later opened a result one of its searches returned
        (get_memory/expand on a returned id), the implicit success signal. Per
        turn since 0048 (per session before); the denominator excludes sessionless
        turns, which are one call each and can never be followed. `turns`,
        `calls_per_turn_median`, `tokens_per_turn_median` (None when no turn had a
        measured call) and `turn_gap_s` describe the window's requests.

        `calls`, `errors`, `embedder_failures`, and `llm_calls` are over EVERY
        traced tool. `latency_p50_ms`, `latency_p90_ms`, `weak_or_none`, and
        `weak_or_none_rate` are scoped to tool IN ('search_memories',
        'search_graph') -- only those two ever set confidence_level, and mixing
        in get_memory/expand/find_entity/entity_activity (a few ms each, no
        confidence at all) diluted both the latency percentiles and the
        weak-or-none rate toward the non-search calls' shape. weak_or_none_rate's
        denominator is the search-call count, not the overall call count (None
        when the window had zero searches). None on an empty window.

        Token fields price the window's context cost: `tokens_measured` counts the
        calls carrying a `result_bytes` measurement (rows written before 0042 have
        none and are excluded from every token aggregate, though they still count in
        `calls`), `tokens_per_result` is window-total tokens over window-total result
        rows, and `bytes_per_token` reports the divisor in force so a consumer can
        state the assumption."""
        with self._pool.connection() as conn:
            agg = conn.execute(
                """SELECT count(*) AS count,
                          count(*) FILTER (WHERE NOT embedder_ok) AS embedder_failures,
                          count(*) FILTER (WHERE error IS NOT NULL) AS errors,
                          coalesce(sum(llm_calls), 0) AS llm_calls
                   FROM mcp_trace
                   WHERE at >= now() - make_interval(days => %(days)s)
                     AND (%(include_evals)s OR caller IS NULL)""",
                {"days": days, "include_evals": include_evals},
            ).fetchone()
            search_agg = conn.execute(
                """SELECT count(*) AS count,
                          percentile_cont(0.5) WITHIN GROUP (ORDER BY latency_ms) AS p50,
                          percentile_cont(0.9) WITHIN GROUP (ORDER BY latency_ms) AS p90,
                          count(*) FILTER (WHERE confidence_level IN ('weak', 'none')) AS weak_or_none
                   FROM mcp_trace
                   WHERE tool IN ('search_memories', 'search_graph')
                     AND at >= now() - make_interval(days => %(days)s)
                     AND (%(include_evals)s OR caller IS NULL)""",
                {"days": days, "include_evals": include_evals},
            ).fetchone()
            dist = conn.execute(
                """SELECT confidence_level, count(*) AS n
                   FROM mcp_trace
                   WHERE at >= now() - make_interval(days => %(days)s)
                     AND (%(include_evals)s OR caller IS NULL)
                     AND confidence_level IS NOT NULL
                   GROUP BY confidence_level""",
                {"days": days, "include_evals": include_evals},
            ).fetchall()
            # Per-turn figures: a turn counts in the window when its OPENER ended
            # inside it. followup_rate is per turn now (was per session, 0039):
            # of the turns with at least one search, the share where a later
            # get_memory/expand in the SAME turn fetched an id one of that turn's
            # searches returned. Sessionless turns (one call each, never followable)
            # are left out of the denominator so they cannot drag the rate down.
            turns = conn.execute(
                """WITH opened AS (
                       SELECT o.id AS turn_id, o.session_id
                       FROM mcp_trace o
                       WHERE o.turn_id = o.id
                         AND o.at >= now() - make_interval(days => %(days)s)
                         AND (%(include_evals)s OR o.caller IS NULL)
                   ), per AS (
                       SELECT t.turn_id, t.session_id,
                              count(c.id) AS calls,
                              sum(c.result_bytes) AS bytes,
                              bool_or(c.tool IN ('search_memories', 'search_graph'))
                                  AS has_search,
                              bool_or(c.tool IN ('get_memory', 'expand')
                                      AND c.target_id IS NOT NULL
                                      AND EXISTS (
                                          SELECT 1 FROM mcp_trace s
                                          WHERE s.turn_id = c.turn_id
                                            AND s.id <> c.id AND s.at <= c.at
                                            AND s.tool IN ('search_memories', 'search_graph')
                                            AND s.results @> jsonb_build_array(
                                                    jsonb_build_object('id', c.target_id::text))))
                                  AS followed
                       FROM opened t
                       JOIN mcp_trace c ON c.turn_id = t.turn_id
                       GROUP BY t.turn_id, t.session_id
                   )
                   SELECT count(*) AS turns,
                          percentile_cont(0.5) WITHIN GROUP (ORDER BY calls) AS calls_median,
                          percentile_cont(0.5) WITHIN GROUP (ORDER BY bytes)
                              FILTER (WHERE bytes IS NOT NULL) AS bytes_median,
                          count(*) FILTER (WHERE has_search AND session_id IS NOT NULL)
                              AS searching,
                          count(*) FILTER (WHERE has_search AND followed) AS followed
                   FROM per""",
                {"days": days, "include_evals": include_evals},
            ).fetchone()
            # Aggregate over EXACT bytes and divide once, rather than rounding each
            # row to tokens first -- rounding never accumulates into the totals.
            tok = conn.execute(
                """SELECT count(*) AS measured,
                          percentile_cont(0.5) WITHIN GROUP (ORDER BY result_bytes)
                              AS median_bytes,
                          sum(result_bytes) AS total_bytes,
                          sum(result_count) AS total_rows
                   FROM mcp_trace
                   WHERE at >= now() - make_interval(days => %(days)s)
                     AND (%(include_evals)s OR caller IS NULL)
                     AND result_bytes IS NOT NULL""",
                {"days": days, "include_evals": include_evals},
            ).fetchone()
            by_tool = conn.execute(
                """SELECT tool, count(*) AS calls,
                          percentile_cont(0.5) WITHIN GROUP (ORDER BY result_bytes)
                              AS median_bytes,
                          sum(result_bytes) AS total_bytes
                   FROM mcp_trace
                   WHERE at >= now() - make_interval(days => %(days)s)
                     AND (%(include_evals)s OR caller IS NULL)
                     AND result_bytes IS NOT NULL
                   GROUP BY tool
                   ORDER BY sum(result_bytes) DESC""",
                {"days": days, "include_evals": include_evals},
            ).fetchall()
        return {
            "days": days,
            "calls": agg["count"],
            "latency_p50_ms": search_agg["p50"],
            "latency_p90_ms": search_agg["p90"],
            "weak_or_none": search_agg["weak_or_none"],
            "weak_or_none_rate": (search_agg["weak_or_none"] / search_agg["count"])
                                 if search_agg["count"] else None,
            "embedder_failures": agg["embedder_failures"],
            "errors": agg["errors"],
            "llm_calls": agg["llm_calls"],
            "confidence_levels": {row["confidence_level"]: row["n"] for row in dist},
            "followup_rate": (turns["followed"] / turns["searching"])
                             if turns["searching"] else None,
            "turns": turns["turns"],
            "calls_per_turn_median": turns["calls_median"],
            "tokens_per_turn_median": _est_tokens(turns["bytes_median"]),
            "turn_gap_s": TURN_GAP_S,
            # tokens_measured is not decoration: on a 90d window shortly after this
            # shipped, most rows predate the column, and a total without its coverage
            # reads as "the KB is cheap" when it means "we only measured Tuesday".
            "tokens_measured": tok["measured"],
            "tokens_median": _est_tokens(tok["median_bytes"]),
            "tokens_total": _est_tokens(tok["total_bytes"]),
            # Window total / window rows -- NOT the mean of per-call ratios, so one
            # fat call cannot be averaged away by many thin ones.
            "tokens_per_result": (_est_tokens(tok["total_bytes"] / tok["total_rows"])
                                  if tok["total_rows"] else None),
            "bytes_per_token": _BYTES_PER_TOKEN,
            "tokens_by_tool": [
                {"tool": t["tool"], "calls": t["calls"],
                 "tokens_median": _est_tokens(t["median_bytes"]),
                 "tokens_total": _est_tokens(t["total_bytes"])}
                for t in by_tool
            ],
        }

    def retrieval_heatmap(self, *, days: "int | None" = 30, bucket: str = "day",
                          tz: str = "UTC", path: "str | None" = None,
                          limit: int = 200, include_evals: bool = False) -> dict:
        """Per-memory retrieval counts per day/week bucket, from mcp_trace.results:
        `surfaced` (any traced tool returned the memory) vs `opened` (get_memory
        returned it), each (trace, memory) pair counted once. `days=None` is all
        history; `tz` is an IANA zone for bucket boundaries; `path` scopes to a
        folder (ltree `<@`). Eval traffic (caller IS NOT NULL) is excluded unless
        include_evals. Raises ValueError on a bad bucket/tz/path/days. Read-only."""
        with self._pool.connection() as conn:
            return _retrieval_heatmap(conn, days=days, bucket=bucket, tz=tz, path=path,
                                      limit=limit, include_evals=include_evals)

    def trace_get(self, id: int) -> "dict | None":
        """One trace with full payloads, its judgments (newest first), and result
        labels resolved live per kind -- memory/entity ids against nodes, concept
        ids against concepts. A vanished id renders as '(deleted)': traces store
        ids only, never labels, so rows can't bloat or go stale."""
        with self._pool.connection() as conn:
            row = conn.execute(
                # Explicit columns, NOT `*`: selecting the replay would de-TOAST a
                # payload this method never ships (it has its own route, trace_replay).
                """SELECT id, at, request_id, session_id, turn_id, caller, tool, args,
                          query, target_id, latency_ms, results, result_count,
                          confidence_level, best_similarity, strategy, judge,
                          crawl_mode, trace, llm_calls, embedder_ok, error,
                          result_bytes, (replay IS NOT NULL) AS has_replay
                   FROM mcp_trace WHERE id = %(id)s""", {"id": id},
            ).fetchone()
            if row is None:
                return None
            judgments = conn.execute(
                """SELECT id, verdict, judged_by, notes, at
                   FROM retrieval_judgment WHERE trace_id = %(id)s
                   ORDER BY at DESC, id DESC""", {"id": id},
            ).fetchall()
            results = row["results"] or []
            ids = [r["id"] for r in results if r.get("id")]
            labels: "dict[str, str]" = {}
            if ids:
                for r in conn.execute(
                        """SELECT id::text AS id,
                                  coalesce(title, path::text) AS label
                           FROM nodes WHERE id = ANY(%(ids)s::uuid[])""",
                        {"ids": ids}).fetchall():
                    labels[r["id"]] = r["label"]
                for r in conn.execute(
                        """SELECT id::text AS id, name AS label
                           FROM concepts WHERE id = ANY(%(ids)s::uuid[])""",
                        {"ids": ids}).fetchall():
                    labels.setdefault(r["id"], r["label"])
        row["results"] = [{**r, "label": labels.get(r.get("id"), "(deleted)")}
                          for r in results]
        # `judgments` is already newest-first (ORDER BY at DESC, id DESC), so its
        # head is the latest verdict -- same "latest wins across judges" contract
        # trace_recent's LATERAL join gives `judgment`. TraceDetail extends Trace
        # and Trace declares `judgment` required; without this the detail route's
        # payload silently omitted a field its own type promised.
        row["judgment"] = judgments[0]["verdict"] if judgments else None
        row["judgments"] = judgments
        row["result_tokens"] = _est_tokens(row["result_bytes"])
        return row

    def trace_replay(self, id: int) -> "dict | None":
        """One search trace's replay with live memory labels.

        `completeness` is "full" when the row carries a captured replay, "partial"
        when it predates capture (synthesized by _synthesize_replay). Memory ids are
        labelled NOW -- a vanished one reads '(deleted)' with deleted=True. Crawl
        parents captured without a label (hop-2 bridge entities harvested mid-crawl)
        are labelled from nodes; concept labels were frozen at capture because a
        concept rebuild replaces concept ids. None for an unknown id, a non-search
        tool, a trace that errored, or a row with neither replay nor results."""
        with self._pool.connection() as conn:
            row = conn.execute(
                """SELECT id, at, tool, query, args, results, trace, replay, error
                   FROM mcp_trace WHERE id = %(id)s""", {"id": id}).fetchone()
            if (row is None or row["tool"] not in _REPLAYABLE_TOOLS
                    or row["error"] is not None):
                return None
            replay = row["replay"]
            completeness = "full"
            if replay is None and row["results"] is None:
                return None   # results never recorded: nothing honest to replay
            if replay is None:
                completeness = "partial"
                replay = _synthesize_replay(row)
            mem_ids = sorted(_replay_memory_ids(replay))
            labels: dict = {}
            if mem_ids:
                for r in conn.execute(
                        """SELECT id::text AS id, coalesce(title, path::text) AS title,
                                  path::text AS path, doc_kind
                           FROM nodes WHERE id = ANY(%(ids)s::uuid[])""",
                        {"ids": mem_ids}).fetchall():
                    labels[r["id"]] = {"title": r["title"], "path": r["path"],
                                       "doc_kind": r["doc_kind"], "deleted": False}
            for i in mem_ids:
                labels.setdefault(i, {"title": "(deleted)", "path": None,
                                      "doc_kind": None, "deleted": True})
            hops = (replay.get("crawl") or {}).get("hops") or []
            unlabelled = sorted({p["id"] for h in hops for o in h.get("offered") or []
                                 for p in o.get("parents") or []
                                 if p.get("label") is None})
            if unlabelled:
                names = {r["id"]: r["label"] for r in conn.execute(
                    """SELECT id::text AS id, coalesce(title, path::text) AS label
                       FROM nodes WHERE id = ANY(%(ids)s::uuid[])""",
                    {"ids": unlabelled}).fetchall()}
                for h in hops:
                    for o in h.get("offered") or []:
                        for p in o.get("parents") or []:
                            if p.get("label") is None:
                                p["label"] = names.get(p["id"], "(deleted)")
        return {"trace_id": row["id"], "tool": row["tool"], "query": row["query"],
                "at": row["at"], "args": row["args"], "completeness": completeness,
                "replay": replay, "labels": labels}

    def add_judgment(self, trace_id: int, *, verdict: str, judged_by: str,
                     notes: "str | None" = None) -> dict:
        """Append one adjudication. Append-only by design: re-judging inserts a
        new row and readers take the latest per judged_by."""
        if verdict not in ("good", "partial", "bad"):
            raise ValueError(f"unknown verdict: {verdict!r} "
                             "(expected good | partial | bad)")
        with self._pool.connection() as conn:
            if conn.execute("SELECT 1 FROM mcp_trace WHERE id = %(id)s",
                            {"id": trace_id}).fetchone() is None:
                raise LookupError(f"no trace with id {trace_id}")
            row = conn.execute(
                """INSERT INTO retrieval_judgment (trace_id, verdict, judged_by, notes)
                   VALUES (%(trace_id)s, %(verdict)s, %(judged_by)s, %(notes)s)
                   RETURNING id, trace_id, verdict, judged_by, notes, at""",
                {"trace_id": trace_id, "verdict": verdict,
                 "judged_by": judged_by, "notes": notes},
            ).fetchone()
        return row

    def add_turn_judgment(self, turn_id: int, *, verdict: str, judged_by: str,
                          notes: "str | None" = None) -> dict:
        """Append one request-level adjudication. Append-only like add_judgment:
        re-judging inserts a new row and readers take the latest verdict across
        judges (newest first). `turn_id` must be an opener (a row whose turn_id is
        its own id) -- the table's FK cannot express that, so it is enforced here."""
        if verdict not in ("good", "partial", "bad"):
            raise ValueError(f"unknown verdict: {verdict!r} "
                             "(expected good | partial | bad)")
        with self._pool.connection() as conn:
            if conn.execute(
                    "SELECT 1 FROM mcp_trace WHERE id = %(id)s AND turn_id = id",
                    {"id": turn_id}).fetchone() is None:
                raise LookupError(f"no turn with id {turn_id}")
            row = conn.execute(
                """INSERT INTO turn_judgment (turn_id, verdict, judged_by, notes)
                   VALUES (%(turn_id)s, %(verdict)s, %(judged_by)s, %(notes)s)
                   RETURNING id, turn_id, verdict, judged_by, notes, at""",
                {"turn_id": turn_id, "verdict": verdict,
                 "judged_by": judged_by, "notes": notes},
            ).fetchone()
        return row

    # ---- MCP tool enablement -------------------------------------------------
    # The inventory is published by KB_MCP (which is the only thing that knows what
    # tools exist) and the flags are flipped by the dashboard. Both sides go through
    # here so no SQL leaks into either.

    def publish_tool_inventory(self, tools: "list[tuple[str, str]]") -> None:
        """Upsert one row per registered MCP tool as (name, 'read'|'write').

        Called on KB_MCP startup. `enabled` is deliberately absent from the DO UPDATE
        set: a restart must never re-enable a tool that was switched off."""
        if not tools:
            return
        with self._pool.connection() as conn:
            conn.cursor().executemany(
                """INSERT INTO mcp_tool_state (tool, kind)
                   VALUES (%s, %s)
                   ON CONFLICT (tool) DO UPDATE SET kind = EXCLUDED.kind""",
                tools,
            )

    # ---- Skill and agent library -----------------------------------------------
    # Thin pool/transaction wrappers over kb_core.skills; every rule lives there.
    # Defaults keep the dashboard's view (newest revision, pending items listed); every
    # agent-facing caller passes default="approved" / include_unapproved=False itself
    # (KB_MCP does). The library download reads approved revisions only, by construction.

    def search_skills(self, query: "str | None" = None, cursor: "str | None" = None, *,
                      include_archived: bool = False, include_unapproved: bool = True,
                      page_size: int = skills.PAGE_SIZE) -> dict:
        with self._pool.connection() as conn:
            return skills.search(conn, query, cursor, kind="skill",
                                 include_archived=include_archived,
                                 include_unapproved=include_unapproved, page_size=page_size)

    def search_agents(self, query: "str | None" = None, cursor: "str | None" = None, *,
                      include_archived: bool = False, include_unapproved: bool = True,
                      page_size: int = skills.PAGE_SIZE) -> dict:
        with self._pool.connection() as conn:
            return skills.search(conn, query, cursor, kind="agent",
                                 include_archived=include_archived,
                                 include_unapproved=include_unapproved, page_size=page_size)

    def get_skill_file(self, name: str, path: str, revision: int) -> dict:
        with self._pool.connection() as conn:
            return skills.get_file(conn, name, path, revision, kind="skill")

    def get_skill(self, name: str, revision: "int | None" = None, *,
                  default: str = "latest") -> dict:
        with self._pool.connection() as conn:
            return skills.get(conn, name, revision, kind="skill", default=default)

    def get_agent(self, name: str, revision: "int | None" = None, *,
                  default: str = "latest") -> dict:
        with self._pool.connection() as conn:
            return skills.get(conn, name, revision, kind="agent", default=default)

    def upsert_skill(self, name: str, expected_revision: "int | None", files: dict,
                     change_summary: str, *, actor: "str | None" = None,
                     agent_label: "str | None" = None) -> dict:
        with self._pool.connection() as conn, conn.transaction():
            return skills.upsert(conn, name, expected_revision, files, change_summary,
                                 kind="skill", actor=actor, agent_label=agent_label)

    def upsert_agent(self, name: str, expected_revision: "int | None", agent_md,
                     change_summary: str, *, actor: "str | None" = None,
                     agent_label: "str | None" = None) -> dict:
        with self._pool.connection() as conn, conn.transaction():
            return skills.upsert(conn, name, expected_revision, {skills.AGENT_FILE: agent_md},
                                 change_summary, kind="agent", actor=actor,
                                 agent_label=agent_label)

    def delete_skill(self, name: str, expected_revision: "int | None" = None, *,
                     actor: "str | None" = None) -> dict:
        """Archive, never a hard delete: revisions stay readable (see kb_core.skills.archive)."""
        with self._pool.connection() as conn, conn.transaction():
            return skills.archive(conn, name, expected_revision, kind="skill", actor=actor)

    def delete_agent(self, name: str, expected_revision: "int | None" = None, *,
                     actor: "str | None" = None) -> dict:
        with self._pool.connection() as conn, conn.transaction():
            return skills.archive(conn, name, expected_revision, kind="agent", actor=actor)

    def review_skill(self, name: str, revision: int, package_hash: str, approve: bool,
                     notes: str, *, actor: "str | None" = None,
                     reviewer_label: "str | None" = None) -> dict:
        with self._pool.connection() as conn, conn.transaction():
            return skills.review(conn, name, revision, package_hash, approve, notes,
                                 kind="skill", actor=actor, reviewer_label=reviewer_label)

    def review_agent(self, name: str, revision: int, package_hash: str, approve: bool,
                     notes: str, *, actor: "str | None" = None,
                     reviewer_label: "str | None" = None) -> dict:
        with self._pool.connection() as conn, conn.transaction():
            return skills.review(conn, name, revision, package_hash, approve, notes,
                                 kind="agent", actor=actor, reviewer_label=reviewer_label)

    def pending_reviews(self, kind: "str | None" = None) -> dict:
        with self._pool.connection() as conn:
            return skills.pending_reviews(conn, kind)

    def library_etag(self, kind: str) -> str:
        """The download's ETag, without building the archive (a 304 costs one query)."""
        with self._pool.connection() as conn:
            return library_export.etag(conn, kind)

    def export_library(self, kind: str) -> "tuple[str, bytes]":
        with self._pool.connection() as conn, conn.transaction():
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
            return library_export.export(conn, kind)

    def skill_usage(self, *, days: int = 30, tz: str = "UTC") -> dict:
        """Library-wide skill usage for the dashboard (kb_core.skills.usage)."""
        with self._pool.connection() as conn:
            return skills.usage(conn, days, tz)

    def skill_usage_for(self, name: str, *, days: int = 30, tz: str = "UTC") -> dict:
        """One skill's usage for the dashboard (kb_core.skills.usage_for)."""
        with self._pool.connection() as conn:
            return skills.usage_for(conn, name, days, tz)

    def tool_states(self) -> "list[dict]":
        """Every known MCP tool with its kind and enabled flag, reads before writes."""
        with self._pool.connection() as conn:
            return conn.execute(
                """SELECT tool, kind, enabled, updated_at, updated_by
                     FROM mcp_tool_state
                    ORDER BY kind, tool"""
            ).fetchall()

    def set_tool_enabled(self, tool: str, enabled: bool, *,
                         updated_by: "str | None" = None) -> "dict | None":
        """Flip one tool. Returns the updated row, or None if the tool is unknown --
        the API turns that None into a 404 rather than inventing inventory."""
        with self._pool.connection() as conn:
            return conn.execute(
                """UPDATE mcp_tool_state
                      SET enabled = %(enabled)s, updated_at = now(),
                          updated_by = %(updated_by)s
                    WHERE tool = %(tool)s
                RETURNING tool, kind, enabled, updated_at, updated_by""",
                {"tool": tool, "enabled": enabled, "updated_by": updated_by},
            ).fetchone()

    # ---- Content-age decay master switch ---------------------------------------

    def age_decay_enabled(self) -> bool:
        """The dashboard's content-age-decay master switch (age_decay_state, single
        row, seeded true by 0044; missing row degrades to enabled). Read once per
        analysis call — at most one extra SELECT per consolidation pass or promotion
        sweep, so no cache."""
        with self._pool.connection() as conn:
            row = conn.execute("SELECT enabled FROM age_decay_state LIMIT 1").fetchone()
        return bool(row["enabled"]) if row else True

    def set_age_decay(self, enabled: bool, *, updated_by: "str | None" = None) -> dict:
        """Flip the decay switch; returns the updated row so the dashboard renders
        what the server confirmed (mirrors set_tool_enabled's no-optimistic-UI rule)."""
        with self._pool.connection() as conn:
            row = conn.execute(
                """UPDATE age_decay_state
                   SET enabled = %(e)s, updated_at = now(), updated_by = %(by)s
                   RETURNING enabled, updated_at, updated_by""",
                {"e": enabled, "by": updated_by}).fetchone()
        if row is None:
            raise LookupError("age_decay_state row missing — run migrations")
        return dict(row)

    def request_reanalysis(self, id: UUID) -> None:
        """Ask the re-analysis daemon (kb-mcp-reanalyzed) to re-crawl one node now, out of band
        of its timer, and log the request as an explicit 'queued for recrawl' state. Records a
        pending task='recrawl' row (cleared to 'done' by mark_analyzed), then emits NOTIFY on the
        'kb_reanalyze' channel; the wake is fire-and-forget when no daemon is listening."""
        with self._pool.connection() as conn:
            conn.execute(
                """INSERT INTO node_processing
                       (node_id, task, status, queued_at, updated_at)
                   VALUES (%s, 'recrawl', 'pending', now(), now())
                   ON CONFLICT (node_id, task) DO UPDATE SET
                       status = 'pending', queued_at = now(), updated_at = now()""",
                (id,),
            )
            conn.execute("SELECT pg_notify('kb_reanalyze', %s)", (str(id),))

    def due_for_backfill(self, sources: "list[str]", *, before, limit: int = 10) -> "list[Node]":
        """One-time full crawl: every first-pass-enriched ('done') node that existed at `before`
        and hasn't been analyzed since, stalest first. Unlike due_for_reanalysis it ignores
        every dependency cursor: the `--backfill` run analyzes the whole existing corpus
        exactly once. Bounded to ingested_at <= before (only pre-existing nodes); each processed
        node gets last_analyzed_at > before via mark_analyzed and drops out, so the loop
        terminates."""
        if not sources:
            return []
        with self._pool.connection() as conn:
            rows = conn.execute(
                f"""SELECT {_COLUMNS} FROM nodes
                    WHERE source = ANY(%(sources)s)
                      AND enrichment_status = 'done'
                      AND dead_at IS NULL          -- retired memories are frozen: never recrawled
                      AND NOT EXISTS (SELECT 1 FROM nodes a
                                      WHERE a.analysis_excluded AND nodes.path <@ a.path)
                      AND ingested_at <= %(before)s
                      AND (last_analyzed_at IS NULL OR last_analyzed_at < %(before)s)
                    ORDER BY last_analyzed_at ASC NULLS FIRST, ingested_at ASC
                    LIMIT %(limit)s""",
                {"sources": list(sources), "before": before, "limit": limit},
            ).fetchall()
        return [_row_to_node(r) for r in rows]

    def delete_by_external_id(self, source: str, external_id: str, *,
                              recursive: bool = False) -> int:
        """Delete by (source, external_id) — the push-delete path. Returns count."""
        return self.delete(node_id(source, external_id), recursive=recursive)

    # ---- observability -----------------------------------------------------
    def ingest_log(self, *, limit: int = 50, source: str | None = None,
                   event: str | None = None) -> "list[dict]":
        """Recent node-lifecycle events, newest first — the ingestion observability log.

        One row per event: node_created | node_updated | node_deleted | enrichment_done |
        enrichment_failed | enrichment_deferred | ingest_failed | ingest_resolved. `event`
        filters to a single kind (e.g. 'enrichment_failed' for the failures view); failed
        rows carry the reason in `detail` ({"error": …}); ingest_resolved carries
        {"reason": "gone from source" | "dismissed" | "landed"} — "landed" covers a stuck
        record whose recovery upsert was a no-op (unchanged content writes no node event,
        so sync/drain write this explicitly; the engine declines it when a node event
        already ended the streak). The log is append-only and FK-free, so it survives
        prune/delete as a historical record.
        """
        sql = ("SELECT id, at, event, source, external_id, node_id, path, content_hash, "
               "detail FROM ingest_log")
        clauses: list[str] = []
        params: dict = {}
        if source is not None:
            clauses.append("source = %(source)s")
            params["source"] = source
        if event is not None:
            clauses.append("event = %(event)s")
            params["event"] = event
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY at DESC, id DESC LIMIT %(limit)s"
        params["limit"] = limit
        with self._pool.connection() as conn:
            return conn.execute(sql, params).fetchall()

    # ---- stuck ingest records (records the engine refused to store) --------
    # A pull record that fails every tick, or a push event that failed once, is otherwise
    # visible only in ingest.log (a note carrying a NUL byte would fail every tick, logging an
    # identical line each time). The engine records ONE 'ingest_failed' per change of error text
    # and one 'ingest_resolved' when the record leaves the source or is dismissed, on the
    # ordinary ingest_log table — no per-tick writes, no new table.
    def _stuck_rows(self, conn: psycopg.Connection, *, source: str | None,
                    limit: int | None) -> "list[dict]":
        return conn.execute(_STUCK_INGEST_SQL,
                            {"source": source, "limit": limit}).fetchall()

    def stuck_ingest_records(self, *, limit: int = 50) -> "list[dict]":
        """Records whose latest ingest_log event is 'ingest_failed', across all sources,
        newest first — the dashboard banner's read model. Each row: source, external_id,
        node_id, path, error (detail->>'error'), since (the `at` of the current-error row,
        i.e. when the current error text began)."""
        with self._pool.connection() as conn:
            return self._stuck_rows(conn, source=None, limit=limit)

    def open_ingest_failures(self, source: str) -> "dict[str, dict]":
        """{external_id: {"node_id", "error", "path", "at"}} for every record of `source`
        that is currently stuck. The pull path fetches this once per sync and hands it to
        log_ingest_failed as the dedup set, so a stuck record costs zero writes per tick."""
        with self._pool.connection() as conn:
            rows = self._stuck_rows(conn, source=source, limit=None)
        return {r["external_id"]: {"node_id": r["node_id"], "error": r["error"],
                                   "path": r["path"], "at": r["since"]}
                for r in rows}

    def log_ingest_failed(self, source: str, external_id: str, error: str, *,
                          path: "str | None" = None,
                          open: "dict[str, dict] | None" = None) -> bool:
        """Record that (source, external_id) failed to land with `error`, unless it is
        already stuck with this exact text. `open` (from open_ingest_failures) is the
        dedup set and is updated in place when a row is written; None means look the
        source's open set up now (the push drain's convenience path — failures there are
        rare). `path` is the intended ltree path when the caller could derive it. `error`
        is run through sanitize_text before dedup comparison and storage: an exception
        message may quote source content, so it is the one remaining path for a NUL byte
        to reach a text parameter (Postgres text/JSON cannot hold U+0000).
        Returns True iff a row was written."""
        error = sanitize_text(error)
        if open is None:
            open = self.open_ingest_failures(source)
        prior = open.get(external_id)
        if prior is not None and prior["error"] == error:
            return False
        nid = node_id(source, external_id)
        with self._pool.connection() as conn:
            row = conn.execute(
                """INSERT INTO ingest_log
                       (event, source, external_id, node_id, path, detail)
                   VALUES ('ingest_failed', %(source)s, %(external_id)s, %(node_id)s,
                           %(path)s, %(detail)s)
                   RETURNING at""",
                {"source": source, "external_id": external_id, "node_id": nid,
                 "path": path, "detail": Jsonb({"error": error})},
            ).fetchone()
        open[external_id] = {"node_id": nid, "error": error, "path": path,
                             "at": row["at"]}
        return True

    def log_ingest_resolved(self, source: str, external_id: str, *,
                            reason: str = "gone from source") -> bool:
        """Close a stuck record: write 'ingest_resolved' (path copied from its open
        failure) iff its latest event is still 'ingest_failed'. A record that already
        landed or was already resolved is left alone. The check-then-insert here is not
        serialized (no advisory lock), so two concurrent resolvers -- a Dismiss racing a
        tick, or a double click -- can each observe 'ingest_failed' as the latest event and
        each write a row: an audit-only duplicate (the record's state, not stuck, is the
        same either way), not a correctness problem. This guard covers the common cases
        (the double click, the already-landed record) cheaply; it does not guarantee
        exactly one row per resolution. Returns True iff a row was written."""
        nid = node_id(source, external_id)
        with self._pool.connection() as conn, conn.transaction():
            latest = conn.execute(
                """SELECT event, path FROM ingest_log WHERE node_id = %(nid)s
                   ORDER BY at DESC, id DESC LIMIT 1""",
                {"nid": nid},
            ).fetchone()
            if latest is None or latest["event"] != "ingest_failed":
                return False
            conn.execute(
                """INSERT INTO ingest_log
                       (event, source, external_id, node_id, path, detail)
                   VALUES ('ingest_resolved', %(source)s, %(external_id)s, %(node_id)s,
                           %(path)s, %(detail)s)""",
                {"source": source, "external_id": external_id, "node_id": nid,
                 "path": latest["path"], "detail": Jsonb({"reason": reason})},
            )
        return True

    def enrichment_status_counts(self) -> "dict[str, int]":
        """Live node counts per enrichment status — 'processing' is work in flight."""
        with self._pool.connection() as conn:
            rows = conn.execute(
                "SELECT enrichment_status AS status, count(*) AS n "
                "FROM nodes GROUP BY enrichment_status"
            ).fetchall()
        return {r["status"]: r["n"] for r in rows}

    def processing_nodes(self) -> "list[Node]":
        """Nodes currently being enriched (enrichment_status='processing').

        Powers the dashboard's "began analysis" notifications: the UI diffs this set on each
        live change to spot a memory that just entered the worker.
        """
        with self._pool.connection() as conn:
            rows = conn.execute(
                f"SELECT {_COLUMNS} FROM nodes WHERE enrichment_status = 'processing' "
                "ORDER BY last_analyzed_at DESC NULLS LAST"
            ).fetchall()
        return [_row_to_node(r) for r in rows]

    def processing_map(self, node_ids: "list[UUID]") -> "dict[UUID, list[dict]]":
        """Active processing tasks per node, batched (no N+1). Maps node id -> list of
        {'task', 'status'} for rows in 'pending'/'processing'. Nodes with no active task are
        absent. Backs the graph's per-node processing badges."""
        if not node_ids:
            return {}
        with self._pool.connection() as conn:
            rows = conn.execute(
                "SELECT node_id, task, status FROM node_processing "
                "WHERE node_id = ANY(%s) AND status IN ('pending', 'processing') "
                "ORDER BY task",
                (list(node_ids),),
            ).fetchall()
        out: "dict[UUID, list[dict]]" = {}
        for r in rows:
            out.setdefault(r["node_id"], []).append(
                {"task": r["task"], "status": r["status"]}
            )
        return out

    def content_changed_map(self) -> "dict[str, datetime]":
        """Last REAL content change per path, from ingest_log hash transitions. An event
        counts only when its content_hash differs from the previous logged hash at the
        same path, so delete/recreate churn (same bytes, fresh node id) doesn't move it —
        unlike nodes.updated_at, which every pipeline re-upsert touches. Keyed by path:
        node ids churn across recreations but the path is the stable identity. Backs the
        graph's recency coloring."""
        with self._pool.connection() as conn:
            rows = conn.execute(
                """WITH ev AS (
                       SELECT path, at, content_hash,
                              lag(content_hash) OVER (PARTITION BY path ORDER BY at, id)
                                  AS prev_hash
                       FROM ingest_log
                       WHERE event IN ('node_created', 'node_updated')
                         AND path IS NOT NULL)
                   SELECT path, max(at) AS changed_at
                   FROM ev
                   WHERE prev_hash IS NULL OR content_hash IS DISTINCT FROM prev_hash
                   GROUP BY path"""
            ).fetchall()
        return {r["path"]: r["changed_at"] for r in rows}

    def sync_state(self) -> "list[dict]":
        """Per-source sync checkpoints, most recently synced first."""
        with self._pool.connection() as conn:
            return conn.execute(
                "SELECT source, last_synced_at, cursor FROM sync_state "
                "ORDER BY last_synced_at DESC"
            ).fetchall()

    # ---- edges (graph overlay) --------------------------------------------
    def link(self, source_id: UUID, target_id: UUID, relation_type: str,
             *, weight: float | None = None) -> Edge:
        """Create or update a typed, directed edge between two nodes. Idempotent on the
        (source, target, relation_type) key — re-linking updates the weight. Entity-mention
        edges use relation_type='mentions'; memory<->memory edges use other relation_types.
        """
        with self._pool.connection() as conn:
            conn.execute(
                """INSERT INTO edges (source_id, target_id, relation_type, weight)
                   VALUES (%(s)s, %(t)s, %(r)s, %(w)s)
                   ON CONFLICT (source_id, target_id, relation_type)
                   DO UPDATE SET weight = EXCLUDED.weight, updated_at = now()""",
                {"s": source_id, "t": target_id, "r": relation_type, "w": weight},
            )
        return Edge(source_id, target_id, relation_type, weight)

    def unlink(self, source_id: UUID, target_id: UUID, relation_type: str) -> int:
        with self._pool.connection() as conn:
            return conn.execute(
                "DELETE FROM edges WHERE source_id = %s AND target_id = %s "
                "AND relation_type = %s", (source_id, target_id, relation_type),
            ).rowcount

    # ---- embedding dirty-flag helpers -------------------------------------
    def _dirty_entity(self, conn: "psycopg.Connection", entity_id: "UUID") -> None:
        """Mark an entity's embedding stale. Call inside an existing transaction/connection
        immediately after any mutation that changes what the embedding should encode:
        name/aliases/description updates, or mention-context changes (link added/removed)."""
        conn.execute(
            "UPDATE entities SET embed_dirty = true WHERE node_id = %s", (entity_id,)
        )

    def is_embed_dirty(self, entity_id: "UUID") -> bool:
        """Return True if the entity's embed_dirty flag is set (stale embedding)."""
        with self._pool.connection() as conn:
            row = conn.execute(
                "SELECT embed_dirty FROM entities WHERE node_id = %s", (entity_id,)
            ).fetchone()
        return bool(row and row["embed_dirty"])

    def clear_embed_dirty(self, entity_id: "UUID", *,
                          vector: "list[float]", text_hash: str) -> None:
        """Persist an embedding and clear the dirty flag in one atomic write.
        Called by the background embed pass after computing the new vector."""
        with self._pool.connection() as conn:
            conn.execute(
                "UPDATE entities SET embedding = %s, embed_text_hash = %s, embed_dirty = false "
                "WHERE node_id = %s",
                (vector, text_hash, entity_id),
            )

    def get_embedding(self, entity_id: "UUID") -> "list[float] | None":
        """Return the stored embedding vector for an entity, or None if not yet embedded."""
        with self._pool.connection() as conn:
            row = conn.execute(
                "SELECT embedding FROM entities WHERE node_id = %s", (entity_id,)
            ).fetchone()
        return list(row["embedding"]) if row and row["embedding"] is not None else None

    def _entity_embed_inputs(self, conn: "psycopg.Connection", entity_id: "UUID") -> dict:
        """Gather all kwargs for embedding.build_embed_text for one entity.

        Uses the PASSED conn (caller holds it in a pool.connection() block) — never opens
        a new pool connection to avoid deadlock/exhaustion.

        Returns dict(name, aliases, description, kind, mentions, members).
        """
        from kb_core.entities import ENTITY_CLASS, MENTIONS, BELONGS_TO

        row = conn.execute(
            "SELECT canonical_name, aliases, description, entity_type "
            "FROM entities WHERE node_id = %s",
            (entity_id,),
        ).fetchone()
        if row is None:
            raise ValueError(f"Entity {entity_id} not found")

        kind: str = row["entity_type"]  # 'instance' or 'class'
        # link_type that memories use when referencing this entity
        link_type = BELONGS_TO if kind == ENTITY_CLASS else MENTIONS

        # Top-20 linked memories (title, first 200 chars of content), ordered by recency
        mention_rows = conn.execute(
            "SELECT n.title, left(n.content, 200) AS snippet "
            "FROM nodes n "
            "WHERE n.id IN ("
            "  SELECT memory_id FROM entity_links WHERE entity_id = %s AND link_type = %s"
            ") "
            "ORDER BY n.updated_at DESC NULLS LAST "
            "LIMIT 20",
            (entity_id, link_type),
        ).fetchall()
        mentions = [(r["title"] or "", r["snippet"] or "") for r in mention_rows]

        # For a class: collect member instance canonical names; for an instance: empty list
        members: list[str] = []
        if kind == ENTITY_CLASS:
            member_rows = conn.execute(
                "SELECT e.canonical_name FROM entities e "
                "WHERE e.node_id IN ("
                "  SELECT instance_id FROM entity_class_links WHERE class_id = %s"
                ")",
                (entity_id,),
            ).fetchall()
            members = [r["canonical_name"] for r in member_rows]

        return dict(
            name=row["canonical_name"],
            aliases=list(row["aliases"] or []),
            description=row["description"],
            kind=kind,
            mentions=mentions,
            members=members,
        )

    def reembed_dirty_entities(self, client: "object", *, batch: int = 32) -> int:
        """Embed all dirty entities in batches, skipping re-embed when text hash is unchanged.

        For each dirty entity:
        - assembles embed text via build_embed_text(_entity_embed_inputs(...))
        - computes embed_text_hash; if it matches the stored hash, just clears embed_dirty
        - otherwise collects for a batched client.embed() call, then stores vector + hash

        Returns the total count of entities processed (embedded or hash-skipped).
        """
        from kb_core.embedding import build_embed_text, embed_text_hash

        processed = 0
        while True:
            with self._pool.connection() as conn:
                ids = [
                    r["node_id"]
                    for r in conn.execute(
                        "SELECT node_id FROM entities WHERE embed_dirty = true LIMIT %s",
                        (batch,),
                    ).fetchall()
                ]
                if not ids:
                    break

                # Partition into hash-matched (just clear flag) vs needs-embed
                to_embed: list = []
                texts: dict = {}
                hashes: dict = {}

                for eid in ids:
                    ins = self._entity_embed_inputs(conn, eid)
                    text = build_embed_text(**ins)
                    h = embed_text_hash(text)
                    prev_row = conn.execute(
                        "SELECT embed_text_hash FROM entities WHERE node_id = %s", (eid,)
                    ).fetchone()
                    prev_hash = prev_row["embed_text_hash"] if prev_row else None

                    if prev_hash == h:
                        # Text unchanged — just clear the dirty flag, no model call
                        conn.execute(
                            "UPDATE entities SET embed_dirty = false WHERE node_id = %s", (eid,)
                        )
                    else:
                        to_embed.append(eid)
                        texts[eid] = text
                        hashes[eid] = h

                processed += len(ids)

            # Embed the batch that needs new vectors (outside the conn block to avoid
            # holding the connection open during a potentially slow network call)
            if to_embed:
                vectors = client.embed([texts[eid] for eid in to_embed])
                for eid, vec in zip(to_embed, vectors):
                    self.clear_embed_dirty(eid, vector=vec, text_hash=hashes[eid])

            if len(ids) < batch:
                break

        return processed

    # ---- entity links (memory -> entity overlay; separate from `edges`) ----
    def link_entity(self, memory_id: UUID, entity_id: UUID, link_type: str, *,
                    weight: float | None = None, role: "str | None" = None) -> None:
        """Create or update a memory -> entity link in `entity_links`. Idempotent on
        (memory_id, entity_id, link_type). link_type is 'belongs_to' (class) or 'mentions'
        (instance). This is the entity overlay; `link`/`edges` stay memory<->memory.

        `role` carries the mention's relationship to the memory and feeds evidence ranking in
        _entity_judge_view. As in stage_entity_candidate, the column has no CHECK constraint,
        so a value outside ROLES is normalized to NULL here rather than reaching the column."""
        from kb_core.entities import ROLES

        if role not in ROLES:
            role = None
        with self._pool.connection() as conn:
            conn.execute(
                """INSERT INTO entity_links (memory_id, entity_id, link_type, weight, role)
                   VALUES (%(m)s, %(e)s, %(lt)s, %(w)s, %(r)s)
                   ON CONFLICT (memory_id, entity_id, link_type)
                   DO UPDATE SET weight = EXCLUDED.weight,
                                 role = COALESCE(EXCLUDED.role, entity_links.role),
                                 updated_at = now()""",
                {"m": memory_id, "e": entity_id, "lt": link_type, "w": weight, "r": role},
            )
            self._dirty_entity(conn, entity_id)

    def memories_linked(self, entity_id: UUID, *, link_type: str | None = None) -> "list[Node]":
        """Memories pointing at an entity (in-links) — e.g. 'memories about Avery'. The
        subquery (vs a JOIN) keeps _COLUMNS unambiguous; entity_links shares timestamp names."""
        sql = (f"SELECT {_COLUMNS} FROM nodes WHERE id IN "
               "(SELECT memory_id FROM entity_links WHERE entity_id = %(e)s")
        params: dict = {"e": entity_id}
        if link_type is not None:
            sql += " AND link_type = %(lt)s"; params["lt"] = link_type
        sql += ") ORDER BY importance DESC, path"
        with self._pool.connection() as conn:
            return [_row_to_node(r) for r in conn.execute(sql, params).fetchall()]

    def memories_linked_page(self, entity_id: UUID, *, link_type: str | None = None,
                             limit: int = 25, offset: int = 0,
                             ) -> "tuple[list[tuple[Node, str, float | None]], int]":
        """One importance-ordered page of the memories pointing at an entity, plus the
        total count — the dashboard's paged sibling of memories_linked(). Rows are
        (Node, link_type, weight), pre-aggregated to one row per memory exactly like
        entity_activity() (min link_type / max weight; see the ambiguity note there)."""
        params: dict = {"e": entity_id, "limit": limit, "offset": offset}
        lt_filter = ""
        if link_type is not None:
            lt_filter = "AND link_type = %(lt)s"; params["lt"] = link_type
        sql = (
            "WITH links AS ("
            "  SELECT memory_id, min(link_type) AS link_type, max(weight) AS weight"
            f"  FROM entity_links WHERE entity_id = %(e)s {lt_filter}"
            "  GROUP BY memory_id) "
            f"SELECT {_COLUMNS}, l.link_type AS el_link_type, l.weight AS el_weight, "
            "count(*) OVER () AS el_total "
            "FROM nodes JOIN links l ON l.memory_id = nodes.id "
            "ORDER BY importance DESC, path LIMIT %(limit)s OFFSET %(offset)s"
        )
        with self._pool.connection() as conn:
            rows = conn.execute(sql, params).fetchall()
        total = rows[0]["el_total"] if rows else 0
        return [(_row_to_node(r), r["el_link_type"], r["el_weight"]) for r in rows], total

    def entity_activity(self, entity_id: UUID, *, since: "datetime | None" = None,
                        until: "datetime | None" = None, kind: str | None = None,
                        link_type: str | None = None, limit: int = 50,
                        offset: int = 0) -> "list[tuple[Node, str, float | None]]":
        """Time-ordered memories linked to an entity — 'what has X been involved in, when'.

        The timeline sibling of memories_linked(): newest event first, where a memory's
        event time is COALESCE(event_at, source_created_at) — the real-world date when
        the source declared one (migration 0031), else the hash-gated source timestamp.
        since/until bound that same expression, half-open [since, until). kind filters by
        doc_kind. Rows are (Node, link_type, weight); a memory linked under both link
        types (schema-possible, not seen in practice) is deduped to one row.

        ORDERING. Notes WITH a real event_at come first, in true time
        order; undated notes follow. Often only a minority of notes carry a real
        event_at, and the rest fall back to a source timestamp that bulk file operations
        can flatten into a few ~1-second blocks covering most of the mention graph — so interleaving
        them by that value does not order the timeline, it fabricates one, and a
        genuinely dated memory loses its place to an accident of file mtime. Within
        equal timestamps the tiebreak is importance then link weight, not `path`:
        alphabetical order is filesystem-walk order, which can bury a query's gold
        answer well down the list. `path` remains last so ordering stays deterministic.
        """
        params: dict = {"e": entity_id, "limit": limit, "offset": offset}
        lt_filter = ""
        if link_type is not None:
            lt_filter = "AND link_type = %(lt)s"; params["lt"] = link_type
        where = ["TRUE"]
        if since is not None:
            where.append("COALESCE(nodes.event_at, nodes.source_created_at) >= %(since)s")
            params["since"] = since
        if until is not None:
            where.append("COALESCE(nodes.event_at, nodes.source_created_at) < %(until)s")
            params["until"] = until
        if kind is not None:
            where.append("nodes.doc_kind = %(kind)s"); params["kind"] = kind
        # Links are pre-aggregated in a CTE (one row per memory) so the outer SELECT can
        # keep _COLUMNS unqualified — the CTE exposes no column that nodes also has (see
        # the ambiguity note on memories_linked).
        sql = (
            "WITH links AS ("
            "  SELECT memory_id, min(link_type) AS link_type, max(weight) AS weight"
            f"  FROM entity_links WHERE entity_id = %(e)s {lt_filter}"
            "  GROUP BY memory_id) "
            f"SELECT {_COLUMNS}, l.link_type AS el_link_type, l.weight AS el_weight "
            "FROM nodes JOIN links l ON l.memory_id = nodes.id "
            f"WHERE {' AND '.join(where)} "
            "ORDER BY (nodes.event_at IS NOT NULL) DESC, "
            "COALESCE(nodes.event_at, nodes.source_created_at) DESC NULLS LAST, "
            "nodes.importance DESC, l.weight DESC NULLS LAST, "
            "nodes.path LIMIT %(limit)s OFFSET %(offset)s"
        )
        with self._pool.connection() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [(_row_to_node(r), r["el_link_type"], r["el_weight"]) for r in rows]

    def entities_of(self, memory_id: UUID, *, link_type: str | None = None) -> "list[Node]":
        """Entities a memory links to (out-links) — its classes and/or instances."""
        sql = (f"SELECT {_COLUMNS} FROM nodes WHERE id IN "
               "(SELECT entity_id FROM entity_links WHERE memory_id = %(m)s")
        params: dict = {"m": memory_id}
        if link_type is not None:
            sql += " AND link_type = %(lt)s"; params["lt"] = link_type
        sql += ") ORDER BY importance DESC, path"
        with self._pool.connection() as conn:
            return [_row_to_node(r) for r in conn.execute(sql, params).fetchall()]

    def active_entity_provenance(self) -> "list[tuple[Entity, list[tuple[Node, str]]]]":
        """Every ACTIVE entity paired with the memories it was found in (its entity_links
        in-links), each tagged with its link_type ('belongs_to' for a class, 'mentions' for
        an instance). The provenance read for "what entities came from what memories": three
        batched reads — active entities, their links, and the linked memory nodes — joined in
        Python, so there's no per-entity round-trip. An entity with no surviving links comes
        back with an empty list. Memories strongest-first within each entity; entities ordered
        by type then name."""
        with self._pool.connection() as conn:
            ent_rows = conn.execute(
                f"SELECT {_COLUMNS}, {_ENTITY_COLUMNS} "
                "FROM nodes JOIN entities e ON e.node_id = nodes.id "
                "WHERE e.status = %(s)s ORDER BY e.entity_type, e.canonical_name",
                {"s": STATUS_ACTIVE},
            ).fetchall()
            # Links are kept out of the node SELECT: entity_links shares timestamp column names
            # with nodes, which would make _COLUMNS ambiguous in a join (see memories_linked).
            link_rows = conn.execute(
                "SELECT entity_id, memory_id, link_type FROM entity_links "
                "WHERE entity_id IN (SELECT node_id FROM entities WHERE status = %(s)s)",
                {"s": STATUS_ACTIVE},
            ).fetchall()
            mem_ids = list({r["memory_id"] for r in link_rows})
            mems: dict[UUID, Node] = {}
            if mem_ids:
                mems = {r["id"]: _row_to_node(r) for r in conn.execute(
                    f"SELECT {_COLUMNS} FROM nodes WHERE id = ANY(%(ids)s)",
                    {"ids": mem_ids}).fetchall()}
        by_entity: dict[UUID, list[tuple[Node, str]]] = {}
        for r in link_rows:
            m = mems.get(r["memory_id"])
            if m is not None:
                by_entity.setdefault(r["entity_id"], []).append((m, r["link_type"]))
        for pairs in by_entity.values():
            pairs.sort(key=lambda p: (-(p[0].importance or 0.0), p[0].path))
        return [(_row_to_entity(e), by_entity.get(e["id"], [])) for e in ent_rows]

    def neighbors(self, id: UUID, *, relation_type: str | None = None,
                  direction: str = "out") -> "list[Node]":
        """Nodes connected to `id` by an edge. direction='out' -> nodes it points to (it is
        the edge source); 'in' -> nodes pointing to it. Optional relation_type filter."""
        near, far = (("source_id", "target_id") if direction == "out"
                     else ("target_id", "source_id"))
        sql = (f"SELECT {_COLUMNS} FROM nodes WHERE id IN "
               f"(SELECT {far} FROM edges WHERE {near} = %(id)s")
        params: dict = {"id": id}
        if relation_type is not None:
            sql += " AND relation_type = %(r)s"
            params["r"] = relation_type
        sql += ") ORDER BY importance DESC, path"
        with self._pool.connection() as conn:
            return [_row_to_node(r) for r in conn.execute(sql, params).fetchall()]

    def get_edges(self, id: UUID, *, relation_type: str | None = None,
                  direction: str = "out") -> "list[Edge]":
        """The raw edges incident to `id` (exposes weights), strongest first."""
        near = "source_id" if direction == "out" else "target_id"
        sql = ("SELECT source_id, target_id, relation_type, weight FROM edges "
               f"WHERE {near} = %(id)s")
        params: dict = {"id": id}
        if relation_type is not None:
            sql += " AND relation_type = %(r)s"
            params["r"] = relation_type
        sql += " ORDER BY weight DESC NULLS LAST"
        with self._pool.connection() as conn:
            return [Edge(r["source_id"], r["target_id"], r["relation_type"], r["weight"])
                    for r in conn.execute(sql, params).fetchall()]

    # ---- concept consolidation (derived concept layer) ----------------------
    def consolidation_signal(self) -> dict:
        """Cheap churn probe for the consolidate daemon: has anything the concept
        graph consumes changed since the last completed run, and is enrichment
        still writing? One round-trip. All timestamps travel out with the DB's
        own now() so the caller never mixes in the host clock. 'failed' notes are
        deliberately NOT backlog — a permanently failed note must not gate
        consolidation forever (the daily backstop covers stuck 'processing').

        The note half reads enriched_at, NOT updated_at: the always-due obsidian pull
        (sources.toml period_seconds=0) re-upserts every note each ~20s scheduler tick and the
        upsert bumps updated_at unconditionally, which would hold the quiet window shut forever
        (the churn path would never fire). enriched_at moves only when enrichment completes,
        which is hash-gated upstream, i.e. only when graph inputs actually changed; a brand-new
        note is covered by backlog until its enrichment lands."""
        with self._pool.connection() as conn:
            row = conn.execute(
                """SELECT
                       GREATEST(
                           (SELECT max(updated_at) FROM entity_links),
                           (SELECT max(enriched_at) FROM nodes WHERE type = 'note')
                       ) AS last_change,
                       (SELECT count(*) FROM nodes
                        WHERE type = 'note'
                          AND enrichment_status IN ('pending', 'processing')) AS backlog,
                       (SELECT max(finished_at) FROM concept_runs
                        WHERE status = 'done') AS last_run_finished,
                       now() AS db_now""").fetchone()
        return dict(row)

    def concept_mention_edges(self) -> "list[dict]":
        """One row per live-memory --mentions--> active-instance link, with the entity's
        distinct-memory support. The bipartite half of the consolidation graph; class
        (belongs_to) links are deliberately excluded — a class is a hub that would glue
        every same-kind memory together. Exclusion is the inherited kind (own-flag or
        any ancestor folder's), matching the enricher and crawl gates.

        `age_factor` is the memory's content-age decay (kb_core.aging, 1.0 unless
        matched by a policy decay rule); concepts.py multiplies it into mention_edge_weight's
        RESULT — never the confidence input, whose band semantics a multiplier would
        corrupt."""
        age_m = age_sql("m.event_at", "m.tags", "m.path", "m.source", self._policy.decay)
        params_age = age_params(self._policy.decay, self.age_decay_enabled())
        with self._pool.connection() as conn:
            rows = conn.execute(
                f"""SELECT el.memory_id, el.entity_id, el.weight,
                          count(*) OVER (PARTITION BY el.entity_id) AS support,
                          {age_m} AS age_factor
                   FROM entity_links el
                   JOIN entities e ON e.node_id = el.entity_id
                   JOIN nodes m ON m.id = el.memory_id
                   WHERE el.link_type = %(lt)s AND e.entity_type = %(inst)s
                     AND e.status = %(active)s
                     AND m.dead_at IS NULL
                     AND NOT EXISTS (SELECT 1 FROM nodes a
                                     WHERE a.analysis_excluded AND m.path <@ a.path)""",
                {"lt": MENTIONS, "inst": ENTITY_INSTANCE, "active": STATUS_ACTIVE,
                 **params_age},
            ).fetchall()
        return [dict(r) for r in rows]

    def memory_knn_edges(self, *, k: int = 8, min_sim: float = 0.55) -> "list[dict]":
        """Brute-force pgvector kNN among live, embedded memory nodes: for each memory its
        k nearest, kept when cosine similarity >= min_sim. No ANN index needed at personal
        scale (a batch job; an HNSW index added later speeds this up transparently).
        Pairs appear once per direction — the graph builder dedups. Exclusion is the
        inherited kind (own-flag or any ancestor folder's, like the crawl gates), and
        entity-ineligible memories — already judged content-free noise — emit no
        similarity edges either.

        `weight` is `sim` decayed by content age for memories matched by a policy decay
        rule (kb_core.aging), newer-endpoint rule: GREATEST of the two endpoints' factors,
        so old<->old boilerplate fades while an old memory keeps full-weight edges to
        fresh content. Selection (top-k, min_sim) deliberately stays on raw cosine —
        membership is stable, only influence fades."""
        rules = self._policy.decay
        age_m = age_sql("m.event_at", "m.tags", "m.path", "m.source", rules)
        age_n = age_sql("n.event_at", "n.tags", "n.path", "n.source", rules)
        params_age = age_params(rules, self.age_decay_enabled())
        with self._pool.connection() as conn:
            rows = conn.execute(
                f"""WITH mems AS (
                       SELECT id, embedding, event_at, tags, path, source FROM nodes
                       WHERE embedding IS NOT NULL AND dead_at IS NULL
                         AND entity_eligible
                         AND type NOT IN ('folder', %(ent)s)
                         AND NOT EXISTS (SELECT 1 FROM nodes a
                                         WHERE a.analysis_excluded AND nodes.path <@ a.path)
                   )
                   SELECT m.id AS src, n.id AS dst,
                          1 - (m.embedding <=> n.embedding) AS sim,
                          (1 - (m.embedding <=> n.embedding)) *
                            GREATEST({age_m}, {age_n}) AS weight
                   FROM mems m
                   CROSS JOIN LATERAL (
                       SELECT x.id, x.embedding, x.event_at, x.tags, x.path, x.source
                       FROM mems x WHERE x.id <> m.id
                       ORDER BY x.embedding <=> m.embedding
                       LIMIT %(k)s
                   ) n
                   WHERE 1 - (m.embedding <=> n.embedding) >= %(s)s""",
                {"k": k, "s": min_sim, "ent": ENTITY_TYPE, **params_age},
            ).fetchall()
        return [dict(r) for r in rows]

    def entity_coherence(self) -> dict:
        """`{"baseline": float, "coherence": {entity_id: float}}` — how alike each active
        instance's own memories are, and how alike ANY two memories are, both as mean
        pairwise cosine. Their ratio is what `hub_capped_entities` gates on: an entity
        whose memories are no more alike than random pairs marks no topic and is a
        clustering stopword, however often (or rarely) it is mentioned.

        This is the embedding layer auditing the entity layer — two independently
        produced signals, so a mistake in either does not quietly pass. The memory
        population matches `memory_knn_edges` exactly (live, embedded, entity_eligible,
        not a folder/entity, no analysis-excluded ancestor) so coherence and baseline are
        measured over the same set the graph is built from.

        Entities with a single mention are ABSENT from the map rather than scored 0.0 —
        one memory yields no pair, and a 0.0 would read as "measured, uninformative" and
        get a harmless leaf cut.

        Cost is the pair count, sum over entities of C(support, 2) — about two thousand pairs on
        a few-hundred-memory corpus, one aggregate. It grows with the SQUARE of entity support,
        so if a single entity ever reaches four figures of support this wants a per-entity
        sample rather than every pair."""
        with self._pool.connection() as conn:
            rows = conn.execute(
                """WITH mems AS (
                       SELECT id, embedding FROM nodes
                       WHERE embedding IS NOT NULL AND dead_at IS NULL
                         AND entity_eligible
                         AND type NOT IN ('folder', %(ent)s)
                         AND NOT EXISTS (SELECT 1 FROM nodes a
                                         WHERE a.analysis_excluded AND nodes.path <@ a.path)
                   ), links AS (
                       SELECT el.memory_id, el.entity_id
                       FROM entity_links el
                       JOIN entities e ON e.node_id = el.entity_id
                       JOIN mems m ON m.id = el.memory_id
                       WHERE el.link_type = %(lt)s AND e.entity_type = %(inst)s
                         AND e.status = %(active)s
                   )
                   SELECT l1.entity_id,
                          avg(1 - (a.embedding <=> b.embedding)) AS coherence
                   FROM links l1
                   JOIN links l2 ON l2.entity_id = l1.entity_id
                                AND l1.memory_id < l2.memory_id
                   JOIN mems a ON a.id = l1.memory_id
                   JOIN mems b ON b.id = l2.memory_id
                   GROUP BY l1.entity_id""",
                {"lt": MENTIONS, "inst": ENTITY_INSTANCE, "active": STATUS_ACTIVE,
                 "ent": ENTITY_TYPE},
            ).fetchall()
            base = conn.execute(
                """WITH mems AS (
                       SELECT id, embedding FROM nodes
                       WHERE embedding IS NOT NULL AND dead_at IS NULL
                         AND entity_eligible
                         AND type NOT IN ('folder', %(ent)s)
                         AND NOT EXISTS (SELECT 1 FROM nodes a
                                         WHERE a.analysis_excluded AND nodes.path <@ a.path)
                   )
                   SELECT avg(1 - (x.embedding <=> y.embedding)) AS baseline
                   FROM mems x JOIN mems y ON x.id < y.id""",
                {"ent": ENTITY_TYPE},
            ).fetchone()
        baseline = base["baseline"] if base and base["baseline"] is not None else None
        return {"baseline": float(baseline) if baseline is not None else None,
                "coherence": {str(r["entity_id"]): float(r["coherence"]) for r in rows}}

    def concept_member_view(self, memory_ids: "list", entity_ids: "list", *,
                            max_entities: int = 12, max_memories: int = 8) -> dict:
        """Label-prompt inputs for one community: entity names (each with one class, when
        typed) and memory (title, snippet, folder-breadcrumb) triples — the folder a memory
        is filed under is deliberate signal for the labeler (shared folders often name the
        cluster's project or area). Caller order (strongest-first) is preserved; ids arrive
        as strings from the graph, hence the ::uuid[] casts."""
        ents = [str(i) for i in entity_ids][:max_entities]
        mems = [str(i) for i in memory_ids][:max_memories]
        with self._pool.connection() as conn:
            erows = conn.execute(
                """SELECT e.node_id, e.canonical_name AS name,
                          (SELECT c.canonical_name FROM entity_class_links ecl
                           JOIN entities c ON c.node_id = ecl.class_id
                           WHERE ecl.instance_id = e.node_id
                           ORDER BY ecl.weight DESC NULLS LAST LIMIT 1) AS class_name
                   FROM entities e WHERE e.node_id = ANY(%(ids)s::uuid[])""",
                {"ids": ents}).fetchall()
            mrows = conn.execute(
                """SELECT id, title,
                          coalesce(nullif(summary, ''), left(content, 200)) AS snippet,
                          path::text AS path
                   FROM nodes WHERE id = ANY(%(ids)s::uuid[])""",
                {"ids": mems}).fetchall()
        by_ent = {str(r["node_id"]): (r["name"], r["class_name"]) for r in erows}
        by_mem = {str(r["id"]): (r["title"] or "", r["snippet"] or "",
                                 humanize_path(parent_of(r["path"] or "")))
                  for r in mrows}
        return {"entities": [by_ent[i] for i in ents if i in by_ent],
                "memories": [by_mem[i] for i in mems if i in by_mem]}

    @contextlib.contextmanager
    def consolidation_lock(self):
        """Exclude concurrent consolidation passes. The daemon is normally the only
        writer, so this exists for daemon-vs-`kb-consolidate --apply`.

        A session-level advisory lock must be released on the SAME session that took it,
        so this pins one pooled connection for the whole block rather than taking and
        releasing through the pool. The daemon's pool is 4 connections used sequentially,
        so pinning one for a multi-minute pass is free."""
        with self._pool.connection() as conn:
            got = conn.execute(
                "SELECT pg_try_advisory_lock(hashtext('kb_consolidate')) AS ok"
            ).fetchone()["ok"]
            if not got:
                raise ConsolidationBusy("another consolidation pass is running")
            # Close the transaction the lock's own SELECT opened: `open_pool` does not set
            # autocommit, so without this the session would sit idle-in-transaction for the
            # whole multi-minute pass -- pinning the vacuum horizon, and, if
            # idle_in_transaction_session_timeout is ever set, getting killed mid-pass with
            # the advisory lock silently released underneath it. Session-level advisory
            # locks survive a commit (they are tied to the session, not the transaction), so
            # this is safe.
            conn.commit()
            try:
                yield
            finally:
                conn.execute("SELECT pg_advisory_unlock(hashtext('kb_consolidate'))")

    def sweep_orphaned_concept_runs(self) -> int:
        """Mark rows left 'running' by a killed process as failed; returns how many.
        Safe because consolidation is single-writer: at daemon startup any 'running' row
        is by definition dead. Guarded by the advisory lock — if it is held, a legitimate
        CLI pass is in flight and this must not touch its row, so it skips entirely and
        returns 0."""
        try:
            with self.consolidation_lock():
                with self._pool.connection() as conn:
                    return conn.execute(
                        "UPDATE concept_runs SET status = 'failed', finished_at = now() "
                        "WHERE status = 'running'").rowcount
        except ConsolidationBusy:
            return 0

    def request_consolidation(self, source: str = "dashboard") -> dict:
        """Ask kb-mcp-consolidated to rebuild the concept layer NOW, out of band of its
        churn trigger. Mirrors request_reanalysis — durable row plus a fire-and-forget
        NOTIFY that wakes the daemon in about a second if one is listening.

        Idempotent on the queue, NOT on the run: a second request is suppressed only while
        one is still unclaimed, so an impatient double-click cannot queue two passes. A
        click made while a pass is already RUNNING *is* queued — that pass already read its
        inputs and cannot reflect whatever prompted the click, so it is served by the next
        pass instead. Dropping it here would silently discard the click."""
        with self._pool.connection() as conn:
            waiting = conn.execute(
                "SELECT count(*) AS n FROM concept_run_requests "
                "WHERE claimed_at IS NULL").fetchone()["n"]
        if waiting:
            return self.consolidation_run_status()
        with self._pool.connection() as conn:
            conn.execute("INSERT INTO concept_run_requests (source) VALUES (%s)",
                         (source,))
            conn.execute("SELECT pg_notify('kb_consolidate', %s)", (source,))
        return self.consolidation_run_status()

    def claim_consolidation_requests(self) -> "list[int]":
        """Claim every unclaimed request in one statement, so N impatient clicks
        coalesce into ONE pass. Returns the claimed ids."""
        with self._pool.connection() as conn:
            rows = conn.execute(
                "UPDATE concept_run_requests SET claimed_at = now() "
                "WHERE claimed_at IS NULL RETURNING id").fetchall()
        return [r["id"] for r in rows]

    def release_consolidation_requests(self, ids: "list[int]") -> None:
        """Undo a claim so the next poll retries it — used when the advisory lock is
        held by a CLI pass. The click stays alive instead of dying on someone else's
        run."""
        if not ids:
            return
        with self._pool.connection() as conn:
            conn.execute("UPDATE concept_run_requests SET claimed_at = NULL "
                         "WHERE id = ANY(%s)", (ids,))

    def finish_consolidation_requests(self, ids: "list[int]", run_id: "int | None",
                                      error: "str | None") -> None:
        """Mark claimed requests served by `run_id` (or failed with `error`)."""
        if not ids:
            return
        with self._pool.connection() as conn:
            conn.execute(
                "UPDATE concept_run_requests "
                "SET finished_at = now(), run_id = %(r)s, error = %(e)s "
                "WHERE id = ANY(%(ids)s)",
                {"r": run_id, "e": error, "ids": ids})

    def consolidation_run_status(self, *, run_timeout_s: float = 1800.0) -> dict:
        """One probe answering "is the concept layer rebuilding, and what did the last
        rebuild do?" — the read behind the dashboard's rebuild button.

        States: `running` (a fresh 'running' row) | `queued` (an unclaimed request) |
        `stalled` (an unclaimed request with NOTHING running, or a 'running' row past
        `run_timeout_s`) | `idle`.

        The stalled rule is the subtle one: the daemon cannot claim while it is mid-pass,
        so an old unclaimed request is only evidence of a dead daemon when no run is in
        flight. All ages are measured on the DB clock; the host clock is never consulted.
        """
        with self._pool.connection() as conn:
            row = conn.execute(
                """SELECT
                       (SELECT to_jsonb(r) FROM (
                            SELECT id, requested_at, claimed_at, finished_at, error
                            FROM concept_run_requests
                            WHERE finished_at IS NULL
                            ORDER BY requested_at LIMIT 1) r) AS request,
                       (SELECT count(*) FROM concept_run_requests
                        WHERE claimed_at IS NULL) AS unclaimed,
                       (SELECT extract(epoch FROM now() - min(requested_at))
                        FROM concept_run_requests WHERE claimed_at IS NULL)
                            AS oldest_unclaimed_s,
                       (SELECT to_jsonb(r) FROM (
                            SELECT id AS run_id, started_at, phase, trigger_reason,
                                   extract(epoch FROM now() - started_at) AS age_s
                            FROM concept_runs WHERE status = 'running'
                            ORDER BY started_at DESC LIMIT 1) r) AS running,
                       (SELECT to_jsonb(r) FROM (
                            SELECT id AS run_id, finished_at, status, trigger_reason,
                                   stats
                            FROM concept_runs WHERE status <> 'running'
                            ORDER BY finished_at DESC LIMIT 1) r) AS last,
                       (SELECT count(*) FROM nodes
                        WHERE type = 'note'
                          AND enrichment_status IN ('pending', 'processing')) AS backlog
                """).fetchone()

        running = row["running"]
        if running is not None and running.pop("age_s") > run_timeout_s:
            running = None                       # a dead pass, not a live one
            state = "stalled"
        elif running is not None:
            state = "running"
        elif row["unclaimed"]:
            state = ("stalled" if (row["oldest_unclaimed_s"] or 0) > STALE_REQUEST_S
                     else "queued")
        else:
            state = "idle"

        last = row["last"]
        if last is not None:
            # Flatten the audit row's stats jsonb into the fields the UI renders.
            stats = last.pop("stats") or {}
            last.update({k: stats.get(k) for k in
                         ("communities", "kept", "created", "updated", "retired",
                          "discarded", "label_failures", "embed_failures",
                          "label_error", "embed_error", "error")})
        return {"state": state, "request": row["request"], "running": running,
                "last": last, "backlog": row["backlog"]}

    def begin_concept_run(self, params: dict, *, trigger_reason: str = "churn") -> int:
        """Open a consolidation-run audit row; returns its id. Opened BEFORE the graph,
        labeling and embedding work so an in-flight pass is observable (the dashboard's
        rebuild button reads it) and so `started_at` covers the whole pass. It used to be
        opened last, which is why rows written by older versions record a 0s duration."""
        with self._pool.connection() as conn:
            row = conn.execute(
                "INSERT INTO concept_runs (params, trigger_reason) "
                "VALUES (%(p)s, %(t)s) RETURNING id",
                {"p": Jsonb(params), "t": trigger_reason}).fetchone()
        return row["id"]

    def set_concept_run_phase(self, run_id: int, phase: str) -> None:
        """Coarse in-flight progress: 'graph' | 'labeling' | 'applying'. Deliberately not
        per-community — the UI shows which stage, not a counter."""
        with self._pool.connection() as conn:
            conn.execute("UPDATE concept_runs SET phase = %(ph)s WHERE id = %(id)s",
                         {"ph": phase, "id": run_id})

    def finish_concept_run(self, run_id: int, *, status: str, stats: dict) -> None:
        with self._pool.connection() as conn:
            conn.execute(
                "UPDATE concept_runs SET status = %(s)s, stats = %(st)s, "
                "finished_at = now() WHERE id = %(id)s",
                {"s": status, "st": Jsonb(stats), "id": run_id})

    def active_concepts(self) -> "list[dict]":
        """Every active concept with its memory-member ids — the run-to-run matching
        baseline and the CLI --list read."""
        with self._pool.connection() as conn:
            rows = conn.execute(
                """SELECT c.id, c.name, c.kind, c.level, c.description,
                          coalesce(array_agg(cm.node_id)
                                   FILTER (WHERE cm.member_kind = 'memory'),
                                   '{}') AS memory_ids,
                          count(cm.node_id)::int AS member_count
                   FROM concepts c
                   LEFT JOIN concept_members cm ON cm.concept_id = c.id
                   WHERE c.status = 'active'
                   GROUP BY c.id
                   ORDER BY member_count DESC, c.name""").fetchall()
        return [dict(r) for r in rows]

    def apply_concept_run(self, run_id: int, upserts: "list[dict]",
                          retire_ids: "list") -> dict:
        """Replace the derived concept layer in ONE transaction.

        Matched communities (prev_id set) keep their concept id and first_seen_at with
        label/members refreshed; new ones insert; unmatched previous concepts are RETIRED
        (reversible — never deleted). Members are fully replaced; parent links resolve in
        a second pass once every key has an id. A vector is written as a text literal
        cast ::vector (same pattern as set_embedding). "retired" in the returned stats is
        the number of rows actually flipped to 'retired' (the UPDATE is idempotent via
        AND status = 'active', so already-retired or nonexistent ids are not counted)."""
        created = updated = 0
        with self._pool.connection() as conn:
            ids: "dict[str, object]" = {}
            for u in upserts:
                emb = None
                if u.get("embedding") is not None:
                    emb = "[" + ",".join(repr(float(x)) for x in u["embedding"]) + "]"
                args = {"n": u["name"], "k": u["kind"], "d": u["description"],
                        "l": u["level"], "e": emb, "mc": len(u["members"]), "r": run_id}
                if u.get("prev_id") is not None:
                    row = conn.execute(
                        """UPDATE concepts
                           SET name = CASE WHEN label_locked THEN name ELSE %(n)s END,
                               kind = CASE WHEN label_locked THEN kind ELSE %(k)s END,
                               description = CASE WHEN label_locked THEN description
                                                  ELSE %(d)s END,
                               level = %(l)s, status = 'active', retired_at = NULL,
                               embedding = coalesce(%(e)s::vector, embedding),
                               member_count = %(mc)s, last_run_id = %(r)s,
                               updated_at = now()
                           WHERE id = %(id)s RETURNING id""",
                        {**args, "id": u["prev_id"]}).fetchone()
                    updated += 1
                else:
                    row = conn.execute(
                        """INSERT INTO concepts
                               (name, kind, description, level, embedding,
                                member_count, last_run_id)
                           VALUES (%(n)s, %(k)s, %(d)s, %(l)s, %(e)s::vector,
                                   %(mc)s, %(r)s)
                           RETURNING id""", args).fetchone()
                    created += 1
                ids[u["key"]] = row["id"]
                conn.execute("DELETE FROM concept_members WHERE concept_id = %s",
                             (row["id"],))
                for node_id, kind, score in u["members"]:
                    conn.execute(
                        "INSERT INTO concept_members "
                        "(concept_id, node_id, member_kind, score) "
                        "VALUES (%s, %s::uuid, %s, %s)",
                        (row["id"], str(node_id), kind, score))
            # Parent links resolve here, once every SURVIVING key has an id. A non-empty
            # parent_key that resolves to nothing means run_once dropped that parent this
            # pass -- its label came back incoherent, or its previous concept was
            # dismissed -- while its level-1 children, separate communities, still point
            # at it. Such a child has no parent any more, so RE-LEVEL it to a root.
            # Persisting level > 0 with a NULL parent puts a concept in a dead zone the
            # dashboard cannot render: ConceptExplorer lists roots (level 0, no parent),
            # then each root's children (non-null parent), and a parentless level-1
            # concept matches neither. Such concepts were invisible that way.
            for u in upserts:
                parent_key = u.get("parent_key") or ""
                parent = ids.get(parent_key)
                orphaned = parent is None and parent_key != ""
                conn.execute(
                    "UPDATE concepts SET parent_id = %s, level = %s WHERE id = %s",
                    (parent, 0 if orphaned else u["level"], ids[u["key"]]))
            retired = 0
            if retire_ids:
                cur = conn.execute(
                    "UPDATE concepts SET status = 'retired', retired_at = now() "
                    "WHERE id = ANY(%(ids)s) AND status = 'active'",
                    {"ids": list(retire_ids)})
                retired = cur.rowcount
            # Re-apply durable member overrides (pin/remove) over this run's freshly
            # rebuilt membership, then recompute member_count so it reflects the
            # override-adjusted set — all inside this same transaction.
            persisted = list(ids.values())
            if persisted:
                conn.execute(
                    """DELETE FROM concept_members cm USING concept_member_overrides o
                       WHERE o.action = 'remove' AND o.concept_id = cm.concept_id
                         AND o.node_id = cm.node_id
                         AND cm.concept_id = ANY(%(ids)s)""", {"ids": persisted})
                conn.execute(
                    """INSERT INTO concept_members (concept_id, node_id, member_kind, score)
                       SELECT o.concept_id, o.node_id,
                              CASE WHEN e.node_id IS NOT NULL THEN 'entity'
                                   ELSE 'memory' END, 1.0
                       FROM concept_member_overrides o
                       LEFT JOIN entities e ON e.node_id = o.node_id
                       WHERE o.action = 'pin' AND o.concept_id = ANY(%(ids)s)
                       ON CONFLICT (concept_id, node_id) DO NOTHING""",
                    {"ids": persisted})
                conn.execute(
                    """UPDATE concepts SET member_count =
                           (SELECT count(*) FROM concept_members
                            WHERE concept_id = concepts.id)
                       WHERE id = ANY(%(ids)s)""", {"ids": persisted})
        return {"created": created, "updated": updated, "retired": retired}

    def concept_activity(self, days: int = 7) -> "dict[str, dict]":
        """Per-concept authorship activity over the window: how many member memories
        actually CHANGED (count + latest event), keyed by concept id string.

        Computed from ingest_log node_created/node_updated events — which fire only on
        a content-hash change — and NEVER from node timestamps: nodes.updated_at is
        touched by pipeline passes (re-analysis, embeddings) and source_updated_at has
        been flattened by bulk file operations, so both lie about authorship recency.
        """
        days = max(1, min(int(days), 365))
        with self._pool.connection() as conn:
            rows = conn.execute(
                """SELECT cm.concept_id, count(DISTINCT il.node_id) AS changed,
                          max(il.at) AS last_change
                   FROM ingest_log il
                   JOIN concept_members cm ON cm.node_id = il.node_id
                                          AND cm.member_kind = 'memory'
                   WHERE il.at > now() - make_interval(days => %(d)s)
                     AND il.event IN ('node_created', 'node_updated')
                   GROUP BY cm.concept_id""", {"d": days}).fetchall()
        return {str(r["concept_id"]): {"changed": r["changed"],
                                       "last_change": r["last_change"].isoformat()}
                for r in rows}

    def concept_priority(self, window_days: "int | None" = None) -> "dict[str, dict]":
        """Per-concept goal signals for every ACTIVE concept, keyed by concept id string.

        current  — the goal-membership test: >= 1 activity event inside the window,
                   where an activity event is a member memory's real-world event time
                   (nodes.event_at) or one of its hash-gated content changes
                   (ingest_log node_created/node_updated). Never bare node timestamps
                   (those can be flattened by bulk file operations).
        priority — importance × data-recency × retrieval-recency. Importance is the
                   member-score-weighted mean of member importance (mean, not sum, so
                   size alone never wins); the recencies are floored exponential
                   decays, so dormant projects sink but stay ranked and retrievable.
                   Retrieval recency feeds off nodes.last_accessed_at, which search
                   hits, tracked gets, and concept drill-ins all bump.
        trend    — rising | steady | fading from recent-half vs prior-half event
                   counts (None when not current).
        """
        cp = self._policy.concepts
        if window_days is None:
            window_days = cp.goal_window_days
        window_days = max(1, min(int(window_days), 365))
        wsec = window_days * 86400.0
        with self._pool.connection() as conn:
            rows = conn.execute(
                """WITH ev AS (
                       SELECT cm.concept_id, n.event_at AS at
                       FROM concept_members cm JOIN nodes n ON n.id = cm.node_id
                       WHERE cm.member_kind = 'memory' AND n.event_at IS NOT NULL
                       UNION ALL
                       SELECT cm.concept_id, il.at
                       FROM concept_members cm
                       JOIN ingest_log il ON il.node_id = cm.node_id
                       WHERE cm.member_kind = 'memory'
                         AND il.event IN ('node_created', 'node_updated')
                   ), agg AS (
                       SELECT concept_id, max(at) AS last_data_at,
                              count(*) FILTER (
                                  WHERE at > now() - make_interval(secs => %(w)s))
                                  AS in_window,
                              count(*) FILTER (
                                  WHERE at > now() - make_interval(secs => %(h)s))
                                  AS recent_half,
                              count(*) FILTER (
                                  WHERE at <= now() - make_interval(secs => %(h)s)
                                    AND at > now() - make_interval(secs => %(w)s))
                                  AS prior_half
                       FROM ev GROUP BY concept_id
                   ), imp AS (
                       SELECT cm.concept_id,
                              sum(cm.score * n.importance)
                                  / nullif(sum(cm.score), 0) AS importance,
                              max(n.last_accessed_at) AS last_retrieved_at
                       FROM concept_members cm JOIN nodes n ON n.id = cm.node_id
                       WHERE cm.member_kind = 'memory'
                       GROUP BY cm.concept_id
                   )
                   SELECT c.id, imp.importance, imp.last_retrieved_at, agg.last_data_at,
                          coalesce(agg.in_window, 0) AS in_window,
                          coalesce(agg.recent_half, 0) AS recent_half,
                          coalesce(agg.prior_half, 0) AS prior_half,
                          EXTRACT(EPOCH FROM (now() - agg.last_data_at)) AS data_age,
                          EXTRACT(EPOCH FROM (now() - imp.last_retrieved_at)) AS retr_age
                   FROM concepts c
                   LEFT JOIN agg ON agg.concept_id = c.id
                   LEFT JOIN imp ON imp.concept_id = c.id
                   WHERE c.status = 'active'""",
                {"w": wsec, "h": wsec / 2.0}).fetchall()

        def _decay(age_secs, half_life_days: float) -> float:
            if age_secs is None:                      # no signal ever -> the floor
                return cp.recency_floor
            hl = half_life_days * 86400.0
            return cp.recency_floor + (1 - cp.recency_floor) * math.exp(
                -math.log(2) * max(float(age_secs), 0.0) / hl)

        out: "dict[str, dict]" = {}
        for r in rows:
            current = r["in_window"] > 0
            importance = float(r["importance"]) if r["importance"] is not None else 1.0
            priority = (importance
                        * _decay(r["data_age"], cp.data_recency_half_life_days)
                        * _decay(r["retr_age"], cp.retrieval_recency_half_life_days))
            trend = None
            if current:
                recent, prior = r["recent_half"], r["prior_half"]
                if prior == 0:
                    trend = "rising"
                elif recent >= 1.5 * prior:
                    trend = "rising"
                elif recent <= 0.5 * prior:
                    trend = "fading"
                else:
                    trend = "steady"
            out[str(r["id"])] = {
                "current": current,
                "priority": round(priority, 6),
                "importance": round(importance, 4),
                "events_in_window": r["in_window"],
                "last_data_at": (r["last_data_at"].isoformat()
                                 if r["last_data_at"] else None),
                "last_retrieved_at": (r["last_retrieved_at"].isoformat()
                                      if r["last_retrieved_at"] else None),
                "trend": trend,
            }
        return out

    def record_concept_access(self, concept_id, *, top: int = 10) -> None:
        """A concept drill-in counts as retrieving its top member memories — this is
        what feeds concept_priority()'s retrieval-recency, so a dormant project a
        human (or agent) revisits floats back up the catalog."""
        with self._pool.connection() as conn:
            rows = conn.execute(
                """SELECT node_id FROM concept_members
                   WHERE concept_id = %(c)s::uuid AND member_kind = 'memory'
                   ORDER BY score DESC LIMIT %(k)s""",
                {"c": str(concept_id), "k": top}).fetchall()
        self._record_access([r["node_id"] for r in rows])

    def concepts_view(self, activity_days: "int | None" = None,
                      window_days: "int | None" = None) -> "dict":
        """The concept layer for the dashboard, one payload: every ACTIVE concept with
        its scored members, display label resolved (entity canonical_name, else node
        title, else slug). Read-only and sized for a personal KB (tens of concepts,
        hundreds of members), so graph shaping happens client-side. With activity_days
        set, each concept also carries an `activity` rollup from concept_activity();
        with window_days set, a `goal` block from concept_priority() — and the catalog
        is then ordered by goal priority (desc) instead of size."""
        activity = self.concept_activity(activity_days) if activity_days else None
        goal = self.concept_priority(window_days) if window_days else None
        with self._pool.connection() as conn:
            crows = conn.execute(
                """SELECT id, name, kind, level, parent_id, description, member_count,
                          label_locked, status
                   FROM concepts WHERE status = 'active'
                   ORDER BY member_count DESC, name""").fetchall()
            mrows = conn.execute(
                """SELECT cm.concept_id, cm.node_id, cm.member_kind, cm.score,
                          coalesce(e.canonical_name, n.title, n.slug) AS label,
                          n.doc_kind
                   FROM concept_members cm
                   JOIN concepts c ON c.id = cm.concept_id AND c.status = 'active'
                   JOIN nodes n ON n.id = cm.node_id
                   LEFT JOIN entities e ON e.node_id = cm.node_id
                   ORDER BY cm.score DESC, label""").fetchall()
        members: "dict[object, list[dict]]" = {}
        for r in mrows:
            members.setdefault(r["concept_id"], []).append(
                {"node_id": str(r["node_id"]), "member_kind": r["member_kind"],
                 "score": r["score"], "label": r["label"],
                 "doc_kind": r["doc_kind"]})
        concepts = [
            {"id": str(r["id"]), "name": r["name"], "kind": r["kind"],
             "level": r["level"],
             "parent_id": str(r["parent_id"]) if r["parent_id"] else None,
             "description": r["description"], "member_count": r["member_count"],
             "members": members.get(r["id"], []),
             "label_locked": r["label_locked"], "status": r["status"],
             **({"activity": activity.get(str(r["id"]),
                                          {"changed": 0, "last_change": None})}
                if activity is not None else {}),
             **({"goal": goal.get(str(r["id"]))} if goal is not None else {})}
            for r in crows]
        if goal is not None:
            concepts.sort(key=lambda c: -(c["goal"] or {}).get("priority", 0.0))
        return {"concepts": concepts}

    # ---- concept curation (durable human overrides; rules, not edits) --------
    def concept_constraints(self) -> "list[dict]":
        with self._pool.connection() as conn:
            rows = conn.execute(
                "SELECT id, kind, left_id, right_id, note FROM concept_constraints "
                "ORDER BY id").fetchall()
        return [dict(r) for r in rows]

    def add_concept_constraint(self, kind: str, left_id, right_id,
                               note: "str | None" = None) -> dict:
        """Durable must_link/cannot_link rule on an entity-anchored pair. Canonical
        order; idempotent on exact duplicate; a contradictory rule (same pair,
        opposite kind) is rejected — the human must delete the old rule first.

        Race-proof by construction: the `concept_constraints_pair` unique index (one
        rule per (left_id, right_id) pair, regardless of kind) is the actual guard.
        We INSERT ... ON CONFLICT DO NOTHING and then re-select, rather than
        SELECT-then-INSERT, so two concurrent callers can never both land a row for
        the same pair — one wins the insert, the other observes it on re-select."""
        if kind not in ("must_link", "cannot_link"):
            raise ValueError(f"unknown constraint kind: {kind}")
        lo, hi = sorted((str(left_id), str(right_id)))
        if lo == hi:
            raise ValueError("a constraint needs two different anchors")
        with self._pool.connection() as conn:
            row = conn.execute(
                "INSERT INTO concept_constraints (kind, left_id, right_id, note) "
                "VALUES (%(k)s, %(l)s, %(r)s, %(n)s) "
                "ON CONFLICT (left_id, right_id) DO NOTHING "
                "RETURNING id, kind, left_id, right_id, note",
                {"k": kind, "l": lo, "r": hi, "n": note}).fetchone()
            if row:
                return dict(row)
            existing = conn.execute(
                "SELECT id, kind, left_id, right_id, note FROM concept_constraints "
                "WHERE left_id = %(l)s AND right_id = %(r)s",
                {"l": lo, "r": hi}).fetchone()
            if existing["kind"] == kind:
                return dict(existing)                              # idempotent duplicate
            raise ValueError(
                f"contradicts existing {existing['kind']} rule #{existing['id']} "
                f"on this pair")

    def delete_concept_constraint(self, constraint_id: int) -> bool:
        with self._pool.connection() as conn:
            cur = conn.execute("DELETE FROM concept_constraints WHERE id = %s",
                               (constraint_id,))
            return cur.rowcount > 0

    # ---- concept separations (concept-pair "unrelated" rules) ----------------
    def concept_separations(self) -> "list[dict]":
        with self._pool.connection() as conn:
            rows = conn.execute(
                """SELECT s.id, s.left_id, s.right_id, s.note,
                          l.name AS left_name, r.name AS right_name
                   FROM concept_separations s
                   JOIN concepts l ON l.id = s.left_id
                   JOIN concepts r ON r.id = s.right_id
                   ORDER BY s.id""").fetchall()
        return [dict(r) for r in rows]

    def add_concept_separation(self, left_id, right_id,
                               note: "str | None" = None) -> dict:
        """Durable "these two CONCEPTS are unrelated" rule. Anchored on concept ids
        (concepts aren't nodes, so this can't live in concept_constraints); every
        consolidation run re-resolves it to node anchors from current membership
        (concepts.resolve_separations). Canonical order; idempotent on duplicate —
        INSERT ... ON CONFLICT DO NOTHING then re-select, same race-proof shape as
        add_concept_constraint."""
        lo, hi = sorted((str(left_id), str(right_id)))
        if lo == hi:
            raise ValueError("a separation needs two different concepts")
        with self._pool.connection() as conn:
            self._concept_row(conn, lo)
            self._concept_row(conn, hi)
            row = conn.execute(
                "INSERT INTO concept_separations (left_id, right_id, note) "
                "VALUES (%(l)s, %(r)s, %(n)s) "
                "ON CONFLICT (left_id, right_id) DO NOTHING "
                "RETURNING id, left_id, right_id, note",
                {"l": lo, "r": hi, "n": note}).fetchone()
            if row:
                return dict(row)
            return dict(conn.execute(
                "SELECT id, left_id, right_id, note FROM concept_separations "
                "WHERE left_id = %(l)s AND right_id = %(r)s",
                {"l": lo, "r": hi}).fetchone())

    def delete_concept_separation(self, separation_id: int) -> bool:
        with self._pool.connection() as conn:
            cur = conn.execute("DELETE FROM concept_separations WHERE id = %s",
                               (separation_id,))
            return cur.rowcount > 0

    def concept_separation_rules(self) -> "list[dict]":
        """Each separation with both sides' CURRENT memory members strongest-first —
        the input to concepts.resolve_separations at consolidation time."""
        member_sql = ("SELECT coalesce(array_agg(cm.node_id "
                      "                ORDER BY cm.score DESC, cm.node_id), '{}') "
                      "FROM concept_members cm "
                      "WHERE cm.concept_id = s.%s AND cm.member_kind = 'memory'")
        with self._pool.connection() as conn:
            rows = conn.execute(
                f"""SELECT s.id, s.note,
                           ({member_sql % 'left_id'}) AS left_members,
                           ({member_sql % 'right_id'}) AS right_members
                    FROM concept_separations s ORDER BY s.id""").fetchall()
        return [dict(r) for r in rows]

    # ---- expand: read-only one-hop neighborhood pivot ---------------------------
    # Two SQL idioms shared by the group queries below:
    #   * _EXPAND_LINKS dedupes entity_links to one weighted edge per (memory, entity)
    #     pair (mentions + belongs_to would otherwise double-count), defaulting a NULL
    #     weight to 0.5 — the same convention as concepts.mention_edge_weight.
    #   * ubiquity damping divides by log2(1 + support), again mirroring
    #     mention_edge_weight, so hub entities can't glue everything to everything.
    # Entity-row class machinery: every entity row expand()
    # returns carries its classes (person/company/tool/…), and an optional class
    # filter restricts entity groups — "who is involved in X" is
    # expand(project_id, classes=['person']). Filter matches by class name,
    # case-insensitive; %(cls)s is NULL when unfiltered.
    # entity_kind filter (below-top-k fix): pushed into each entity-bearing arm's
    # own WHERE, BEFORE that arm's ORDER BY/LIMIT %(k)s (or, for _expand_memory's
    # entities arm, before its Python-side sort+slice) — never applied only after
    # the trim. Filtering post-trim was a real bug: an arm's unfiltered top-k can
    # be entirely one structural kind (e.g. ten tools outscoring three persons),
    # so a filter that runs after LIMIT k finds nothing to keep. %(ek)s is NULL
    # when unfiltered, so this predicate is a no-op and unfiltered calls are
    # byte-identical to before.
    _EXPAND_CLASSES = """LEFT JOIN (SELECT ecl.instance_id,
                       array_agg(cc.canonical_name ORDER BY cc.canonical_name)
                           AS classes
                   FROM entity_class_links ecl
                   JOIN entities cc ON cc.node_id = ecl.class_id
                   GROUP BY ecl.instance_id) ecls
                       ON ecls.instance_id = e.node_id"""
    _EXPAND_CLASS_FILTER = """AND (%(cls)s::text[] IS NULL OR EXISTS (
                       SELECT 1 FROM entity_class_links fecl
                       JOIN entities fc ON fc.node_id = fecl.class_id
                       WHERE fecl.instance_id = e.node_id
                         AND lower(fc.canonical_name) = ANY(%(cls)s::text[])))"""
    _EXPAND_KIND_FILTER = "AND (%(ek)s::text IS NULL OR e.kind = %(ek)s)"

    _EXPAND_LINKS = ("SELECT memory_id, entity_id, max(coalesce(weight, 0.5)) AS w "
                     "FROM entity_links GROUP BY memory_id, entity_id")
    _EXPAND_DAMP = "(ln(1 + greatest(1, sup.support)) / ln(2))"

    def expand(self, any_id, k: int = 8,
               classes: "list[str] | None" = None,
               entity_kind: "str | None" = None) -> dict:
        """Typed one-hop neighborhood around a memory, entity, or concept — the
        `expand()` pivot. Returns {id, kind, label, consolidated_at, groups}; every
        neighbor row is {id, kind, label, score, via} so an agent knows WHICH signal
        produced it — entity rows also carry `classes` (open-vocab domain taxonomy)
        and `entity_kind` (the closed structural axis: person/organization/place/
        event/project/artifact/concept/system/other), plus `role` where the row
        traces to a single entity_links mention edge. Direction-aware by contract:
        entity->concepts ranks by live SHARE (P(concept|entity) recomputed from
        entity_links — no membership-threshold cliff, no consolidation staleness),
        concept->entities by ubiquity-damped INSIDE-WEIGHT (centrality) with share
        reported alongside. `classes` restricts entity groups to instances of the
        named classes (case-insensitive) — expand(project, classes=['person']) is
        the stakeholder query. `entity_kind` similarly restricts entity groups to
        one structural kind (note: this is a DIFFERENT axis from the top-level
        `kind` key on every row, which is the node-kind memory|entity|concept — a
        neighbor row's structural kind always lives under `entity_kind`). The
        entity_kind filter is applied inside each arm's own query, before that
        arm's k-trim — so expand(project_id, k=8, entity_kind='person') returns up
        to 8 persons whenever the project has that many, even if dozens of
        higher-scoring non-persons would otherwise fill the unfiltered top-8; it
        never filters an already-trimmed top-k down to nothing. Groups compute
        independently and fail open — a missing embedding or empty substrate
        yields an empty group, never an error."""
        k = max(1, min(int(k), 50))
        aid = str(UUID(str(any_id)))
        ek = entity_kind.strip().lower() if entity_kind else None
        with self._pool.connection() as conn:
            stamp = conn.execute(
                "SELECT max(finished_at) AS t FROM concept_runs "
                "WHERE status = 'done'").fetchone()["t"]
            args = {"i": aid, "k": k,
                    "cls": ([c.strip().lower() for c in classes if c.strip()]
                            or None) if classes else None,
                    "ek": ek}
            row = conn.execute("SELECT id, name FROM concepts WHERE id = %(i)s",
                               args).fetchone()
            if row:
                groups = self._annotate_entity_kinds(
                    conn, self._expand_concept(conn, args), ek)
                return self._expand_envelope("concept", row, stamp, groups)
            row = conn.execute(
                "SELECT node_id AS id, canonical_name AS name FROM entities "
                "WHERE node_id = %(i)s", args).fetchone()
            if row:
                groups = self._annotate_entity_kinds(
                    conn, self._expand_entity(conn, args), ek)
                return self._expand_envelope("entity", row, stamp, groups)
            row = conn.execute(
                "SELECT id, coalesce(title, slug) AS name FROM nodes "
                "WHERE id = %(i)s AND type = 'note'", args).fetchone()
            if row:
                groups = self._annotate_entity_kinds(
                    conn, self._expand_memory(conn, args), ek)
                return self._expand_envelope("memory", row, stamp, groups)
        raise LookupError(f"no memory, entity, or concept with id {aid}")

    @staticmethod
    def _annotate_entity_kinds(conn, groups: "dict[str, list[dict]]",
                               entity_kind: "str | None") -> "dict[str, list[dict]]":
        """Single extra query annotating every entity-kind row (across all of an
        expand() arm's groups) with `entity_kind`/`occurred_at`. Memory and concept
        rows pass through untouched.

        Below-top-k fix: the actual entity_kind FILTER now runs inside each
        entity-bearing arm's own SQL (`_EXPAND_KIND_FILTER`), BEFORE that arm's
        ORDER BY/LIMIT %(k)s (or, for _expand_memory's entities arm, before its
        Python sort+slice) — so a filtered call reaches every matching row the
        arm's underlying edge set has, not just whatever survived the unfiltered
        top-k. The `entity_kind is not None and ent_kind != entity_kind: continue`
        below is now a defensive backstop (should never trigger given the SQL
        predicate already restricts to the same kind) rather than the primary
        filter — kept because this is the one place that resolves every entity
        row's kind regardless of which arm it came from."""
        ids = [r["id"] for rows in groups.values() for r in rows
               if r["kind"] == "entity"]
        if not ids:
            return groups
        kinds = {str(r["node_id"]): (r["kind"], r["occurred_at"])
                 for r in conn.execute(
                     "SELECT node_id, kind, occurred_at FROM entities "
                     "WHERE node_id = ANY(%(ids)s::uuid[])", {"ids": ids}).fetchall()}
        for key, rows in groups.items():
            kept = []
            for r in rows:
                if r["kind"] != "entity":
                    kept.append(r)
                    continue
                ent_kind, occurred_at = kinds.get(r["id"], (None, None))
                r["entity_kind"] = ent_kind
                if occurred_at is not None:
                    r["occurred_at"] = occurred_at.isoformat()
                if entity_kind is not None and ent_kind != entity_kind:
                    continue
                kept.append(r)
            groups[key] = kept
        return groups

    @staticmethod
    def _expand_envelope(kind: str, row: dict, stamp, groups: dict) -> dict:
        return {"id": str(row["id"]), "kind": kind, "label": row["name"],
                "consolidated_at": stamp.isoformat() if stamp else None,
                "groups": groups}

    @staticmethod
    def _expand_rows(rows, kind: str, via: "str | None" = None) -> "list[dict]":
        out = []
        for r in rows:
            d = {"id": str(r["id"]), "kind": kind, "label": r["label"],
                 "score": round(float(r["score"]), 3),
                 "via": via if via is not None else r["via"]}
            if "share" in r:
                d["share"] = round(float(r["share"]), 3)
            if "classes" in r:
                d["classes"] = list(r["classes"] or [])
            if r.get("role") is not None:
                d["role"] = r["role"]
            out.append(d)
        return out

    def _expand_memory(self, conn, args: dict) -> dict:
        ents = conn.execute(
            f"""SELECT DISTINCT ON (e.node_id) e.node_id AS id,
                      e.canonical_name AS label, coalesce(el.weight, 0.5) AS score,
                      el.link_type AS via, el.role AS role,
                      coalesce(ecls.classes, '{{}}') AS classes
               FROM entity_links el
               JOIN entities e ON e.node_id = el.entity_id AND e.status = %(active)s
               {self._EXPAND_CLASSES}
               WHERE el.memory_id = %(i)s
                 {self._EXPAND_CLASS_FILTER}
                 {self._EXPAND_KIND_FILTER}
               ORDER BY e.node_id, coalesce(el.weight, 0.5) DESC""",
            {**args, "active": STATUS_ACTIVE}).fetchall()
        ents = sorted(ents, key=lambda r: -r["score"])[:args["k"]]
        concepts = conn.execute(
            """SELECT c.id, c.name AS label, cm.score
               FROM concept_members cm
               JOIN concepts c ON c.id = cm.concept_id AND c.status = 'active'
               WHERE cm.node_id = %(i)s
               ORDER BY cm.score DESC LIMIT %(k)s""", args).fetchall()
        similar = conn.execute(
            """SELECT n.id, coalesce(n.title, n.slug) AS label,
                      1 - (n.embedding <=> t.embedding) AS score
               FROM nodes n, (SELECT embedding FROM nodes WHERE id = %(i)s) t
               WHERE n.type = 'note' AND n.id != %(i)s
                 AND n.embedding IS NOT NULL AND t.embedding IS NOT NULL
               ORDER BY n.embedding <=> t.embedding LIMIT %(k)s""", args).fetchall()
        co = conn.execute(
            f"""WITH l AS ({self._EXPAND_LINKS}),
                     sup AS (SELECT entity_id, count(*) AS support FROM l GROUP BY 1)
                SELECT n.id, coalesce(n.title, n.slug) AS label,
                       sum(least(a.w, b.w) / {self._EXPAND_DAMP}) AS score
                FROM l a
                JOIN l b ON b.entity_id = a.entity_id AND b.memory_id != a.memory_id
                JOIN sup ON sup.entity_id = a.entity_id
                JOIN nodes n ON n.id = b.memory_id
                WHERE a.memory_id = %(i)s
                GROUP BY n.id, label
                ORDER BY score DESC LIMIT %(k)s""", args).fetchall()
        return {"entities": self._expand_rows(ents, "entity"),
                "concepts": self._expand_rows(concepts, "concept", "member"),
                "similar_memories": self._expand_rows(similar, "memory", "embedding"),
                "co_mentioned": self._expand_rows(co, "memory", "co-mention")}

    def _expand_entity(self, conn, args: dict) -> dict:
        concepts = conn.execute(
            """WITH l AS (SELECT memory_id, max(coalesce(weight, 0.5)) AS w
                          FROM entity_links WHERE entity_id = %(i)s
                          GROUP BY memory_id),
                    tot AS (SELECT sum(w) AS t FROM l)
               SELECT c.id, c.name AS label, sum(l.w) / max(tot.t) AS score
               FROM l
               JOIN concept_members cm ON cm.node_id = l.memory_id
                                      AND cm.member_kind = 'memory'
               JOIN concepts c ON c.id = cm.concept_id AND c.status = 'active'
               CROSS JOIN tot
               GROUP BY c.id, c.name
               ORDER BY score DESC LIMIT %(k)s""", args).fetchall()
        memories = conn.execute(
            """SELECT n.id, coalesce(n.title, n.slug) AS label,
                      max(coalesce(el.weight, 0.5)) AS score
               FROM entity_links el JOIN nodes n ON n.id = el.memory_id
               WHERE el.entity_id = %(i)s
               GROUP BY n.id, n.title, n.slug, n.updated_at
               ORDER BY score DESC, n.updated_at DESC LIMIT %(k)s""", args).fetchall()
        co = conn.execute(
            f"""WITH l AS ({self._EXPAND_LINKS}),
                     sup AS (SELECT entity_id, count(*) AS support FROM l GROUP BY 1)
                SELECT e.node_id AS id, e.canonical_name AS label,
                       sum(least(a.w, b.w) / {self._EXPAND_DAMP}) AS score,
                       coalesce(ecls.classes, '{{}}') AS classes
                FROM l a
                JOIN l b ON b.memory_id = a.memory_id AND b.entity_id != a.entity_id
                JOIN sup ON sup.entity_id = b.entity_id
                JOIN entities e ON e.node_id = b.entity_id AND e.status = %(active)s
                {self._EXPAND_CLASSES}
                WHERE a.entity_id = %(i)s
                  {self._EXPAND_CLASS_FILTER}
                  {self._EXPAND_KIND_FILTER}
                GROUP BY e.node_id, e.canonical_name, ecls.classes
                ORDER BY score DESC LIMIT %(k)s""",
            {**args, "active": STATUS_ACTIVE}).fetchall()
        return {"concepts": self._expand_rows(concepts, "concept", "share"),
                "memories": self._expand_rows(memories, "memory", "mention"),
                "co_entities": self._expand_rows(co, "entity", "co-mention")}

    def _expand_concept(self, conn, args: dict) -> dict:
        ents = conn.execute(
            f"""WITH mems AS (SELECT node_id FROM concept_members
                              WHERE concept_id = %(i)s AND member_kind = 'memory'),
                     l AS ({self._EXPAND_LINKS}),
                     sup AS (SELECT entity_id, count(*) AS support, sum(w) AS total
                             FROM l GROUP BY entity_id)
                SELECT e.node_id AS id, e.canonical_name AS label,
                       sum(l.w / {self._EXPAND_DAMP}) AS score,
                       sum(l.w) / max(sup.total) AS share,
                       coalesce(ecls.classes, '{{}}') AS classes
                FROM l JOIN mems ON mems.node_id = l.memory_id
                JOIN sup ON sup.entity_id = l.entity_id
                JOIN entities e ON e.node_id = l.entity_id AND e.status = %(active)s
                {self._EXPAND_CLASSES}
                WHERE TRUE {self._EXPAND_CLASS_FILTER}
                  {self._EXPAND_KIND_FILTER}
                GROUP BY e.node_id, e.canonical_name, ecls.classes
                ORDER BY score DESC LIMIT %(k)s""",
            {**args, "active": STATUS_ACTIVE}).fetchall()
        memories = conn.execute(
            """SELECT n.id, coalesce(n.title, n.slug) AS label, cm.score
               FROM concept_members cm JOIN nodes n ON n.id = cm.node_id
               WHERE cm.concept_id = %(i)s AND cm.member_kind = 'memory'
               ORDER BY cm.score DESC LIMIT %(k)s""", args).fetchall()
        return {"entities": self._expand_rows(ents, "entity", "inside-weight"),
                "memories": self._expand_rows(memories, "memory", "member"),
                "related_concepts": self._expand_related(conn, args)}

    def _expand_related(self, conn, args: dict) -> "list[dict]":
        """Three signals merged: parent/children (structural), entity bridges
        (sum of min member-scores over shared entities, top bridge named in `via`),
        and label-embedding cosine. A pair under an active separation rule is excluded
        from EVERY signal — curation that says "unrelated" must suppress the read path
        too, or expand() re-suggests the relation the human just severed. Dedup keeps
        the strongest signal per concept (structural > bridge > embedding)."""
        separated = {str(r["left_id"]) if str(r["right_id"]) == args["i"]
                     else str(r["right_id"])
                     for r in conn.execute(
                         "SELECT left_id, right_id FROM concept_separations "
                         "WHERE left_id = %(i)s OR right_id = %(i)s", args).fetchall()}
        family = conn.execute(
            """SELECT p.id, p.name AS label, 'parent' AS via
               FROM concepts c JOIN concepts p ON p.id = c.parent_id
               WHERE c.id = %(i)s AND p.status = 'active'
               UNION ALL
               SELECT ch.id, ch.name, 'child' FROM concepts ch
               WHERE ch.parent_id = %(i)s AND ch.status = 'active'""",
            args).fetchall()
        bridges = conn.execute(
            """SELECT c2.id, c2.name AS label, sum(least(a.score, b.score)) AS score,
                      (array_agg(e.canonical_name
                                 ORDER BY least(a.score, b.score) DESC))[1] AS bridge
               FROM concept_members a
               JOIN concept_members b ON b.node_id = a.node_id
                                     AND b.concept_id != a.concept_id
               JOIN concepts c2 ON c2.id = b.concept_id AND c2.status = 'active'
               JOIN entities e ON e.node_id = a.node_id
               WHERE a.concept_id = %(i)s AND a.member_kind = 'entity'
                 AND b.member_kind = 'entity'
               GROUP BY c2.id, c2.name
               ORDER BY score DESC LIMIT %(k)s""", args).fetchall()
        similar = conn.execute(
            """SELECT c2.id, c2.name AS label,
                      1 - (c2.embedding <=> c.embedding) AS score
               FROM concepts c, concepts c2
               WHERE c.id = %(i)s AND c2.id != c.id AND c2.status = 'active'
                 AND c.embedding IS NOT NULL AND c2.embedding IS NOT NULL
               ORDER BY c2.embedding <=> c.embedding LIMIT %(k)s""", args).fetchall()
        merged: "dict[str, dict]" = {}
        ranked = (
            [{"id": str(r["id"]), "kind": "concept", "label": r["label"],
              "score": 1.0, "via": r["via"], "_rank": 0} for r in family]
            + [{"id": str(r["id"]), "kind": "concept", "label": r["label"],
                "score": round(float(r["score"]), 3),
                "via": f"entity-bridge:{r['bridge']}", "_rank": 1} for r in bridges]
            + [{"id": str(r["id"]), "kind": "concept", "label": r["label"],
                "score": round(float(r["score"]), 3),
                "via": "embedding", "_rank": 2} for r in similar])
        for row in ranked:
            if row["id"] in separated or row["id"] in merged:
                continue
            merged[row["id"]] = row
        out = sorted(merged.values(), key=lambda r: (r["_rank"], -r["score"]))
        for r in out:
            del r["_rank"]
        return out[:args["k"]]

    def _concept_row(self, conn, concept_id) -> dict:
        row = conn.execute(
            "SELECT id, name, kind, level, description, status, label_locked "
            "FROM concepts WHERE id = %s", (concept_id,)).fetchone()
        if row is None:
            raise LookupError(f"concept not found: {concept_id}")
        return dict(row)

    def set_concept_label(self, concept_id, *, name: "str | None" = None,
                          kind: "str | None" = None,
                          description: "str | None" = None,
                          lock: bool = True) -> dict:
        from kb_core.concept_judge import CONCEPT_KINDS
        if kind is not None and kind not in CONCEPT_KINDS:
            raise ValueError(f"unknown concept kind: {kind}")
        with self._pool.connection() as conn:
            self._concept_row(conn, concept_id)
            conn.execute(
                """UPDATE concepts SET
                       name = coalesce(%(n)s, name),
                       kind = coalesce(%(k)s, kind),
                       description = coalesce(%(d)s, description),
                       label_locked = %(lock)s, updated_at = now()
                   WHERE id = %(id)s""",
                {"n": name, "k": kind, "d": description, "lock": lock,
                 "id": concept_id})
            return self._concept_row(conn, concept_id)

    def set_concept_status(self, concept_id, status: str) -> dict:
        """dismiss ('dismissed') or restore ('active'). 'retired' is pipeline-owned."""
        if status not in ("dismissed", "active"):
            raise ValueError(f"human status must be dismissed|active, got: {status}")
        with self._pool.connection() as conn:
            self._concept_row(conn, concept_id)
            conn.execute("UPDATE concepts SET status = %(s)s, updated_at = now() "
                         "WHERE id = %(id)s", {"s": status, "id": concept_id})
            return self._concept_row(conn, concept_id)

    def override_concept_member(self, concept_id, node_id, action: str,
                                note: "str | None" = None) -> dict:
        """Durable pin/remove of one node in one concept — persisted as a rule the
        apply pass re-applies every run, AND applied immediately so the UI reflects
        it without waiting for the next consolidation."""
        if action not in ("pin", "remove"):
            raise ValueError(f"unknown member action: {action}")
        with self._pool.connection() as conn:
            self._concept_row(conn, concept_id)
            conn.execute(
                """INSERT INTO concept_member_overrides (concept_id, node_id, action, note)
                   VALUES (%(c)s, %(n)s, %(a)s, %(note)s)
                   ON CONFLICT (concept_id, node_id)
                   DO UPDATE SET action = EXCLUDED.action, note = EXCLUDED.note""",
                {"c": concept_id, "n": node_id, "a": action, "note": note})
            if action == "remove":
                conn.execute("DELETE FROM concept_members WHERE concept_id = %s "
                             "AND node_id = %s", (concept_id, node_id))
            else:
                conn.execute(
                    """INSERT INTO concept_members (concept_id, node_id, member_kind, score)
                       SELECT %(c)s, %(n)s,
                              CASE WHEN e.node_id IS NOT NULL THEN 'entity'
                                   ELSE 'memory' END, 1.0
                       FROM nodes nd LEFT JOIN entities e ON e.node_id = nd.id
                       WHERE nd.id = %(n)s
                       ON CONFLICT (concept_id, node_id) DO NOTHING""",
                    {"c": concept_id, "n": node_id})
            conn.execute(
                "UPDATE concepts SET member_count = (SELECT count(*) FROM concept_members "
                "WHERE concept_id = %(id)s) WHERE id = %(id)s", {"id": concept_id})
            return {"concept_id": str(concept_id), "node_id": str(node_id),
                    "action": action}

    def clear_concept_member_override(self, concept_id, node_id) -> bool:
        with self._pool.connection() as conn:
            cur = conn.execute(
                "DELETE FROM concept_member_overrides WHERE concept_id = %s "
                "AND node_id = %s", (concept_id, node_id))
            return cur.rowcount > 0

    def matchable_concepts(self) -> "list[dict]":
        """active + dismissed concepts with memory members — the run-to-run matching
        baseline. Dismissed rows must stay matchable so their communities keep being
        skipped instead of reappearing under a fresh id."""
        with self._pool.connection() as conn:
            rows = conn.execute(
                """SELECT c.id, c.name, c.kind, c.level, c.description, c.status,
                          c.label_locked,
                          coalesce(array_agg(cm.node_id)
                                   FILTER (WHERE cm.member_kind = 'memory'),
                                   '{}') AS memory_ids,
                          count(cm.node_id)::int AS member_count
                   FROM concepts c
                   LEFT JOIN concept_members cm ON cm.concept_id = c.id
                   WHERE c.status IN ('active', 'dismissed')
                   GROUP BY c.id
                   ORDER BY member_count DESC, c.name""").fetchall()
        return [dict(r) for r in rows]

    def concept_curation_roster(self, text: str) -> dict:
        """Grounding roster for the NL curation parse: every active OR dismissed
        concept (dismissed ones tagged "(dismissed)" so `restore` has something to
        resolve against — otherwise a dismissed concept could never be un-dismissed
        by NL command), plus active entity instances whose canonical_name matches any
        word (len >= 3) of the instruction, case-insensitive substring. The LLM may
        only reference ids from this roster — anything else is unresolved by
        construction."""
        words = [w.strip(".,;:!?\"'()").lower() for w in text.split()]
        words = [w for w in words if len(w) >= 3]
        words = [w.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                 for w in words]
        ents: "list[dict]" = []
        with self._pool.connection() as conn:
            crows = conn.execute(
                "SELECT id, name, status FROM concepts WHERE status IN ('active', 'dismissed') "
                "ORDER BY member_count DESC, name").fetchall()
            if words:
                erows = conn.execute(
                    """SELECT DISTINCT e.node_id AS id, e.canonical_name AS name
                       FROM entities e
                       WHERE e.entity_type = %(inst)s AND e.status = %(active)s
                         AND lower(e.canonical_name) LIKE ANY(%(pats)s)
                       ORDER BY name LIMIT 20""",
                    {"inst": ENTITY_INSTANCE, "active": STATUS_ACTIVE,
                     "pats": [f"%{w}%" for w in words]}).fetchall()
                ents = [{"id": str(r["id"]), "name": r["name"]} for r in erows]
        concepts = [{"id": str(r["id"]),
                    "name": r["name"] if r["status"] == "active"
                    else f"{r['name']} (dismissed)"} for r in crows]
        return {"concepts": concepts, "entities": ents}

    # ---- entity-system participation gate ---------------------------------
    def set_entity_eligible(self, memory_id: UUID, eligible: bool, *,
                            reason: str | None = None) -> None:
        """Set whether a memory may participate in the entity system (vote, create/link entities).

        Set by the analysis 'entity_gate' pass: a memory whose content is essentially noise is
        made ineligible so it influences no entities, while staying a normal, searchable memory.
        This is a noise gate, not a richness one — a brief but genuine note stays eligible.

        Making a memory ineligible also RETRACTS the influence it already cast — its pending
        votes (entity_candidates) and its belongs_to/mentions links (entity_links) — so 'not
        influencing the entity system' holds retroactively, not just going forward. Still-supported
        entities survive (other memories may back them); only this memory's contribution is pulled,
        and any instance thereby left with zero support is collected (reconcile_instances).
        Re-enabling clears the reason and invalidates the extraction cursors so the next analysis
        re-stages it. The reason is stored only while ineligible.
        """
        with self._pool.connection() as conn, conn.transaction():
            conn.execute(
                """UPDATE nodes SET entity_eligible = %(e)s, entity_skip_reason = %(r)s,
                       analyzed_class_hash = CASE
                           WHEN %(e)s AND NOT entity_eligible THEN NULL
                           ELSE analyzed_class_hash END,
                       attempted_class_hash = CASE
                           WHEN %(e)s AND NOT entity_eligible THEN NULL
                           ELSE attempted_class_hash END
                   """
                "WHERE id = %(m)s",
                {"e": eligible, "r": reason if not eligible else None, "m": memory_id},
            )
            if not eligible:
                self._retract_influence(conn, [memory_id])
        if not eligible:                       # after the retraction commits, GC newly-orphaned instances
            self.reconcile_instances()

    @staticmethod
    def _retract_influence(conn: psycopg.Connection, memory_ids: "list[UUID]") -> None:
        """Pull each given memory's entity influence: its entity_links rows and its UNPROMOTED
        entity_candidates. Promoted candidates and the entity nodes themselves are never touched —
        an entity other memories support survives. Runs on the caller's connection (no new
        transaction) so it composes inside set_entity_eligible and set_analysis_excluded."""
        if not memory_ids:
            return
        # Collect affected entity_ids before deletion so we can dirty each one after.
        affected = conn.execute(
            "SELECT DISTINCT entity_id FROM entity_links WHERE memory_id = ANY(%s)",
            (memory_ids,),
        ).fetchall()
        conn.execute("DELETE FROM entity_links WHERE memory_id = ANY(%s)", (memory_ids,))
        for row in affected:
            conn.execute(
                "UPDATE entities SET embed_dirty = true WHERE node_id = %s",
                (row["entity_id"],),
            )
        conn.execute(
            "DELETE FROM entity_candidates WHERE memory_id = ANY(%s) AND promoted = false",
            (memory_ids,),
        )

    @staticmethod
    def _effective_excluded(conn: psycopg.Connection, path: str) -> bool:
        """True if `path` is under any node carrying the analysis_excluded own-flag (self or
        ancestor), resolved via the ltree path — the connection-scoped sibling of
        analysis_excluded(node)."""
        return conn.execute(
            "SELECT EXISTS (SELECT 1 FROM nodes a "
            "WHERE a.analysis_excluded AND %(p)s::ltree <@ a.path) AS ex",
            {"p": path},
        ).fetchone()["ex"]

    @staticmethod
    def _apply_exclusion_transition(conn: psycopg.Connection, *,
                                    retract_ids: "list[UUID]",
                                    restage_ids: "list[UUID]") -> bool:
        """Apply an effective-exclusion flip for a set of notes. Pull influence for the
        now-excluded (retract_ids); re-queue the now-included for a fresh enrich pass
        (restage_ids — the same requeue block_class uses). Runs on the caller's open
        transaction. Returns whether anything was retracted, so the caller GCs orphaned
        instances (reconcile_instances) after the commit."""
        if retract_ids:
            KnowledgeBase._retract_influence(conn, retract_ids)
        if restage_ids:
            conn.execute(
                "UPDATE nodes SET enrichment_status = 'pending', enriched_hash = NULL "
                "WHERE id = ANY(%s)", (restage_ids,))
        return bool(retract_ids)

    def _reconcile_exclusion_subtree(self, conn: psycopg.Connection,
                                     new_root: str, old_excluded: bool) -> bool:
        """After a subtree is re-parented under `new_root`, reconcile its notes' entity influence
        against the exclusion change. `old_excluded` is the moved root's effective exclusion BEFORE
        the move. On false->true, retract the now-excluded notes that still hold influence; on
        true->false, re-queue the now-included notes for a fresh enrich pass (a nested own-flag
        keeps its members excluded, so they are filtered out). Returns whether anything was
        retracted."""
        new_excluded = self._effective_excluded(conn, new_root)
        if new_excluded == old_excluded:
            return False
        if new_excluded:
            rows = conn.execute(
                """SELECT n.id FROM nodes n
                   WHERE n.path <@ %(r)s::ltree AND n.type = 'note'
                     AND EXISTS (SELECT 1 FROM nodes a
                                 WHERE a.analysis_excluded AND n.path <@ a.path)
                     AND (EXISTS (SELECT 1 FROM entity_links el WHERE el.memory_id = n.id)
                          OR EXISTS (SELECT 1 FROM entity_candidates ec
                                     WHERE ec.memory_id = n.id AND ec.promoted = false))""",
                {"r": new_root}).fetchall()
            return self._apply_exclusion_transition(
                conn, retract_ids=[r["id"] for r in rows], restage_ids=[])
        rows = conn.execute(
            """SELECT n.id FROM nodes n
               WHERE n.path <@ %(r)s::ltree AND n.type = 'note'
                 AND NOT EXISTS (SELECT 1 FROM nodes a
                                 WHERE a.analysis_excluded AND n.path <@ a.path)""",
            {"r": new_root}).fetchall()
        self._apply_exclusion_transition(
            conn, retract_ids=[], restage_ids=[r["id"] for r in rows])
        return False

    def set_analysis_excluded(self, id: UUID, excluded: bool, *,
                              retract: bool = False) -> Node:
        """Toggle a node's human exclusion from entity analysis (the own-flag). The flag lives only
        on the toggled node; a descendant is excluded by inheritance (resolved via the ltree path),
        so this never stamps the subtree. When excluding with `retract`, also pull the entity
        influence of every memory in the subtree (the folder's docs, or just this doc) — votes and
        links — leaving still-supported entities intact and collecting any instance thereby orphaned
        (reconcile_instances). Including (excluded=False) clears the flag and its timestamp. Returns
        the reloaded node."""
        with self._pool.connection() as conn, conn.transaction():
            row = conn.execute(
                "UPDATE nodes SET analysis_excluded = %(e)s, "
                "analysis_excluded_at = CASE WHEN %(e)s THEN now() ELSE NULL END "
                "WHERE id = %(id)s RETURNING path::text AS path",
                {"e": excluded, "id": id},
            ).fetchone()
            if row is None:
                raise LookupError(f"node not found: {id}")
            if excluded and retract:
                mem_ids = [r["id"] for r in conn.execute(
                    "SELECT id FROM nodes WHERE path <@ %(p)s::ltree AND type <> 'folder'",
                    {"p": row["path"]},
                ).fetchall()]
                self._retract_influence(conn, mem_ids)
        if excluded and retract:               # after the retraction commits, GC newly-orphaned instances
            self.reconcile_instances()
        node = self.get(id)
        if node is None:
            raise LookupError(f"node not found: {id}")
        return node

    def analysis_excluded(self, node: Node) -> bool:
        """Effective exclusion: True if this node or any ancestor folder carries the own-flag.
        Resolved via the ltree path (`node.path <@ ancestor.path`), GiST-indexed. The single source
        of truth read by the enricher gate; the crawl gates inline the same predicate in SQL."""
        with self._pool.connection() as conn:
            return conn.execute(
                "SELECT EXISTS (SELECT 1 FROM nodes a "
                "WHERE a.analysis_excluded AND %(p)s::ltree <@ a.path)",
                {"p": node.path},
            ).fetchone()["exists"]

    def analysis_exclusion_view(self, node: Node) -> dict:
        """Dashboard read model for a node's exclusion state: its own-flag, the effective result
        (self or any ancestor), the nearest ancestor folder path causing an INHERITED exclusion
        (None when self-flagged or not excluded), and the non-folder descendant count (powers
        'applies to N docs'; 1 for a document). Single connection for a consistent snapshot."""
        with self._pool.connection() as conn:
            effective = conn.execute(
                "SELECT EXISTS (SELECT 1 FROM nodes a "
                "WHERE a.analysis_excluded AND %(p)s::ltree <@ a.path)",
                {"p": node.path},
            ).fetchone()["exists"]
            excluded_by = None
            if effective and not node.analysis_excluded:
                row = conn.execute(
                    "SELECT path::text AS path FROM nodes a "
                    "WHERE a.analysis_excluded AND %(p)s::ltree <@ a.path "
                    "AND a.path <> %(p)s::ltree "
                    "ORDER BY nlevel(a.path) DESC LIMIT 1",     # nearest flagged ancestor
                    {"p": node.path},
                ).fetchone()
                excluded_by = row["path"] if row else None
            doc_count = conn.execute(
                "SELECT count(*) AS n FROM nodes WHERE path <@ %(p)s::ltree AND type <> 'folder'",
                {"p": node.path},
            ).fetchone()["n"]
        return {
            "analysis_excluded": node.analysis_excluded,
            "analysis_excluded_effective": effective,
            "excluded_by_path": excluded_by,
            "descendant_doc_count": doc_count,
        }

    def significance_view(self, node: Node) -> dict:
        """Dashboard read model for a node's significance: the per-factor breakdown (value,
        coefficient, contribution) for every registered modifier; the ancestor folders whose
        `manual` values cascade into this node's `inherited` factor (shallow→deep); the decayed
        value search uses right now; the node's age; and (for a folder) how many descendants the
        value cascades to. Single connection for a consistent snapshot."""
        coeffs = {m.name: m.coefficient for m in self._modifier_registry.modifiers()}
        mods = node.importance_modifiers or {}
        factors = [
            {"name": name, "value": float(mods.get(name, 0.0)), "coefficient": coeff,
             "contribution": coeff * float(mods.get(name, 0.0))}
            for name, coeff in coeffs.items()
        ]
        ref = node.last_accessed_at or node.ingested_at
        age_seconds = max(0.0, (datetime.now(timezone.utc) - ref).total_seconds())
        with self._pool.connection() as conn:
            inherited_from = [
                {"path": r["path"], "value": float(r["value"])}
                for r in conn.execute(
                    "SELECT path::text AS path, (importance_modifiers->>'manual')::float AS value "
                    "FROM nodes WHERE type = 'folder' AND %(p)s::ltree <@ path "
                    "AND path <> %(p)s::ltree "
                    "AND COALESCE((importance_modifiers->>'manual')::float, 0) <> 0 "
                    "ORDER BY nlevel(path)",
                    {"p": node.path},
                ).fetchall()
            ]
            descendant_count = conn.execute(
                "SELECT count(*) AS n FROM nodes "
                "WHERE path <@ %(p)s::ltree AND path <> %(p)s::ltree",
                {"p": node.path},
            ).fetchone()["n"]
            key_row = conn.execute(
                "SELECT " + project_key_sql("%(p)s::ltree", self._policy.projects)
                + "::text AS k",
                {"p": node.path, **project_params(self._policy.projects)},
            ).fetchone()
            project_key = key_row["k"]
            project_dormant_days = (self._project_dormant_days(conn, keys=[project_key])
                                    .get(project_key) if project_key else None)
        return {
            "factors": factors,
            "inherited_from": inherited_from,
            "effective_importance": effective_importance(
                node.importance, age_seconds, self._policy.importance.recency_floor,
                self._policy.importance.recency_half_life_seconds),
            "age_days": age_seconds / 86400.0,
            "descendant_count": descendant_count,
            # Whole days since the node's project last changed;
            # None outside a project subtree. Explains the project_dormancy factor.
            "project_dormant_days": project_dormant_days,
        }

    def analysis_excluded_paths(self) -> "list[str]":
        """Every node path self-flagged out of entity analysis. A node is EFFECTIVELY excluded
        when one of these is an ancestor-or-self of its path — letting a caller that already holds
        a batch of nodes (the tree, a subtree) resolve effective exclusion in memory by ltree
        prefix, without a per-node query. The flagged set is small (a few human toggles)."""
        with self._pool.connection() as conn:
            rows = conn.execute(
                "SELECT path::text AS path FROM nodes WHERE analysis_excluded"
            ).fetchall()
        return [r["path"] for r in rows]

    def refresh_entity_contribution(self, memory_id: UUID, *,
                                    classes: bool, instances: bool) -> None:
        """Clear a memory's UNPROMOTED entity contributions so an analysis pass that regenerates
        them re-stages from the current extraction rather than stacking drift across re-analysis.

        Scoped to what the running phase regenerates: `classes` (the enrich-phase discover/classify
        passes) clears its class votes + belongs_to links. Promoted candidates and mention links are
        NEVER touched — they are the durable, materialized graph, and a re-staged mention can't
        re-link past the `promoted` flag. The eligible-memory counterpart of the noise retraction
        above.

        NOTE: the `instances` parameter is retained for signature compatibility but is now a no-op.
        Instance-candidate reconciliation is owned per-memory by `_stage_entities` /
        `prune_memory_instances` (the per-memory diff path), not by this method.
        """
        if not classes:
            return
        with self._pool.connection() as conn, conn.transaction():
            if classes:
                # Collect affected entity_ids before deletion so we can dirty each one.
                affected_classes = conn.execute(
                    "SELECT DISTINCT entity_id FROM entity_links "
                    "WHERE memory_id = %s AND link_type = %s",
                    (memory_id, BELONGS_TO),
                ).fetchall()
                conn.execute(
                    "DELETE FROM entity_links WHERE memory_id = %s AND link_type = %s",
                    (memory_id, BELONGS_TO),
                )
                for row in affected_classes:
                    self._dirty_entity(conn, row["entity_id"])
                conn.execute(
                    "DELETE FROM entity_candidates "
                    "WHERE memory_id = %s AND entity_type = %s AND promoted = false",
                    (memory_id, ENTITY_CLASS),
                )

    def drain_unpromoted_instance_candidates(self) -> int:
        """One-time reconcile: remove every memory's unpromoted INSTANCE vote so the corpus stops
        carrying the drift accumulated before per-pass refresh existed. Re-analysis re-stages
        current votes under the bounded model. Class votes (re-staged only on the enrich phase) and
        the promoted/linked graph are deliberately left intact. Returns rows removed."""
        with self._pool.connection() as conn:
            return conn.execute(
                "DELETE FROM entity_candidates WHERE entity_type <> %s AND promoted = false",
                (ENTITY_CLASS,),
            ).rowcount

    def mark_dead(self, memory_id: UUID, *, reason: str | None = None) -> None:
        """Retire a memory judged no longer chronologically relevant: freeze it as-is.

        Unlike noise (set_entity_eligible), this does NOT retract influence — a dead memory's
        past votes/links are kept, because it mattered once. It just stops here: no new entity
        influence (the gate skips its entity passes) and it is excluded from re-analysis crawls
        (due_for_*), so it is never revisited until revived. COALESCE preserves the original death
        time across repeat calls; the reason is refreshed."""
        with self._pool.connection() as conn:
            conn.execute(
                "UPDATE nodes SET dead_at = COALESCE(dead_at, now()), dead_reason = %(r)s "
                "WHERE id = %(m)s",
                {"r": reason, "m": memory_id},
            )

    def revive(self, memory_id: UUID) -> None:
        """Bring a retired memory back: clear dead_at/dead_reason so it is active and crawlable
        again. Its kept influence simply resumes; the next analysis re-evaluates it from scratch."""
        with self._pool.connection() as conn:
            conn.execute(
                "UPDATE nodes SET dead_at = NULL, dead_reason = NULL WHERE id = %s", (memory_id,))

    def list_excluded(self, *, kind: str | None = None, limit: int = 200,
                      offset: int = 0) -> "list[Node]":
        """The 'shut off' memories — everything excluded from the going-forward entity system:
        noise (entity_eligible = false) and dead (dead_at set). `kind` narrows to 'noise' or
        'dead'; None returns both, most-recently-excluded first. Entity nodes are never included.
        """
        where = "source <> %(es)s AND (entity_eligible = false OR dead_at IS NOT NULL)"
        if kind == "noise":
            where = "source <> %(es)s AND entity_eligible = false AND dead_at IS NULL"
        elif kind == "dead":
            where = "source <> %(es)s AND dead_at IS NOT NULL"
        with self._pool.connection() as conn:
            rows = conn.execute(
                f"""SELECT {_COLUMNS} FROM nodes WHERE {where}
                    ORDER BY COALESCE(dead_at, updated_at) DESC
                    LIMIT %(limit)s OFFSET %(offset)s""",
                {"es": ENTITY_SOURCE, "limit": limit, "offset": offset},
            ).fetchall()
        return [_row_to_node(r) for r in rows]

    # ---- entities (concept nodes) -----------------------------------------
    def upsert_entity(self, name: str, *, categories: "list[str] | None" = None,
                      description: str | None = None,
                      aliases: "list[str] | None" = None) -> Entity:
        """Create or update a canonical INSTANCE entity (one node per real concept, identity =
        name only) and link it to each of `categories` (its class memberships). The node is
        type='entity', source='entity', entity_type='instance', placed flat under
        entities.instances; category is a many-to-many link, not part of identity or path — so
        the same concept is ONE node regardless of how many classes claim it.
        """
        aliases = aliases or []
        # Resolve (and create) class nodes BEFORE the transaction — class mkdir -p must not run
        # nested inside the instance's transaction (shared `entities` ancestor → deadlock).
        class_ids = [cid for c in (categories or [])
                     if (cid := self._ensure_class_id(c)) is not None]
        with self._pool.connection() as conn, conn.transaction():
            node = self._upsert_instance_node(conn, name, description, aliases=aliases)
            for cid in class_ids:
                self._link_instance_class(conn, node.id, cid)
        # node.title is the WRITTEN name -- _upsert_instance_node may have substituted the
        # established casing for `name` if this is a fold-equivalent variant; report the truth.
        return Entity(node=node, entity_type=ENTITY_INSTANCE, canonical_name=node.title,
                      aliases=aliases, description=description, status=STATUS_ACTIVE)

    def _upsert_instance_node(self, conn: psycopg.Connection, name: str,
                              description: str | None,
                              aliases: "list[str] | None" = None) -> Node:
        """Persist the canonical instance node + its `entities` row (entity_type='instance'),
        keyed by instance_external_id(name) so casing/punctuation variants converge. Shared by
        upsert_entity and promote_entity. Caller owns the transaction.

        Variants fold to one key (canonical_key), so on-conflict the incoming name is always
        a casing/punctuation variant of the stored one -- never a real rename (different
        letters mint a different node_id). First-established display name sticks: without
        this, promotion's tick-dependent representative (max(raw_name) at promotable_entities)
        alternated 'Vandelay'/'vandelay' through this upsert's last-write-wins, flipping
        canonical_name, node title AND content hash (re-embed churn)."""
        nid = node_id(ENTITY_SOURCE, instance_external_id(name))
        existing = conn.execute(
            "SELECT canonical_name FROM entities WHERE node_id = %s", (nid,)
        ).fetchone()
        if existing is not None:
            name = existing["canonical_name"]
        record = NodeRecord(
            source=ENTITY_SOURCE, external_id=instance_external_id(name),
            type=ENTITY_TYPE, content=description or name, slug=name,
            parent_path=instance_parent_path(), title=name,
        )
        validate(record, self._registry)
        node = self._persist_node(conn, record)
        conn.execute(
            """INSERT INTO entities
                   (node_id, entity_type, canonical_name, aliases, description, status)
               VALUES (%(id)s, %(t)s, %(n)s, %(a)s, %(d)s, 'active')
               ON CONFLICT (node_id) DO UPDATE SET
                   entity_type = EXCLUDED.entity_type,
                   canonical_name = EXCLUDED.canonical_name,
                   aliases = CASE WHEN EXCLUDED.aliases = '{}' THEN entities.aliases
                                  ELSE EXCLUDED.aliases END,
                   description = COALESCE(EXCLUDED.description, entities.description),
                   status = 'active',
                   embed_dirty = true""",
            {"id": node.id, "t": ENTITY_INSTANCE, "n": name,
             "a": aliases or [], "d": description},
        )
        return node

    def _ensure_class_id(self, category: str) -> "UUID | None":
        """The node id of the category's class, ensuring it exists (created inactive if new).
        Returns None if the category is a blocked tombstone (the caller then skips linking).
        Opens its own connection(s) — MUST be called OUTSIDE any open transaction, never nested
        inside one (the class node's mkdir -p touches the shared `entities` ancestor and would
        deadlock against an outer transaction doing the same)."""
        cls = self.get_entity_class(category)
        if cls is not None and cls.status == STATUS_BLOCKED:
            return None
        if cls is None:
            self.upsert_entity_class(category, active=False)
        return node_id(ENTITY_SOURCE, entity_external_id(ENTITY_CLASS, category))

    @staticmethod
    def _link_instance_class(conn: psycopg.Connection, instance_id: UUID, class_id: UUID,
                             *, weight: float | None = None, origin: str = "derived") -> None:
        """Insert the instance --is_a--> class link on the caller's connection. `origin` is
        'derived' (promotion, from votes) or 'human' (a manual edit). Human precedence: once a link
        is human it stays human and a manual edit upgrades a derived link to human; promotion never
        downgrades a human link. Idempotent."""
        conn.execute(
            """INSERT INTO entity_class_links (instance_id, class_id, weight, origin)
               VALUES (%(i)s, %(c)s, %(w)s, %(o)s)
               ON CONFLICT (instance_id, class_id)
               DO UPDATE SET weight = COALESCE(EXCLUDED.weight, entity_class_links.weight),
                             origin = CASE WHEN entity_class_links.origin = 'human'
                                             OR EXCLUDED.origin = 'human' THEN 'human'
                                           ELSE 'derived' END,
                             updated_at = now()""",
            {"i": instance_id, "c": class_id, "w": weight, "o": origin},
        )
        conn.execute("UPDATE entities SET embed_dirty = true WHERE node_id = %s", (instance_id,))

    def entity_class_link_pairs(self) -> "list[tuple[UUID, UUID]]":
        """Every instance→class membership as (instance_id, class_id) — the edge set the entity
        graph view draws class→instance links from (containment is no longer the tree)."""
        with self._pool.connection() as conn:
            return [(r["instance_id"], r["class_id"]) for r in conn.execute(
                "SELECT instance_id, class_id FROM entity_class_links").fetchall()]

    def classes_of(self, instance_id: UUID) -> "list[Entity]":
        """The category classes an instance belongs to (its is_a links), strongest-name order."""
        with self._pool.connection() as conn:
            rows = conn.execute(
                f"""SELECT {_COLUMNS}, {_ENTITY_COLUMNS}
                    FROM nodes JOIN entities e ON e.node_id = nodes.id
                    WHERE nodes.id IN (
                        SELECT class_id FROM entity_class_links WHERE instance_id = %s)
                    ORDER BY e.canonical_name""",
                (instance_id,),
            ).fetchall()
        return [_row_to_entity(r) for r in rows]

    def get_entity(self, id: UUID) -> "Entity | None":
        with self._pool.connection() as conn:
            row = conn.execute(
                f"SELECT {_COLUMNS}, {_ENTITY_COLUMNS} "
                "FROM nodes JOIN entities e ON e.node_id = nodes.id WHERE nodes.id = %s",
                (id,),
            ).fetchone()
        return _row_to_entity(row) if row else None

    def list_entities(self, *, entity_type: str | None = None) -> "list[Entity]":
        sql = (f"SELECT {_COLUMNS}, {_ENTITY_COLUMNS} "
               "FROM nodes JOIN entities e ON e.node_id = nodes.id")
        params: dict = {}
        if entity_type is not None:
            sql += " WHERE e.entity_type = %(t)s"
            params["t"] = entity_type
        sql += " ORDER BY nodes.path"
        with self._pool.connection() as conn:
            return [_row_to_entity(r) for r in conn.execute(sql, params).fetchall()]

    # Shared SELECT for the two entity-lookup reads below: one entity row enriched
    # with its classes and mention support (distinct memories linking it).
    _ENTITY_PROFILE = """
        SELECT e.node_id AS id, e.canonical_name AS name, e.entity_type, e.status,
               e.description, e.aliases, e.pinned, e.kind, e.occurred_at,
               coalesce(s.support, 0) AS mentions,
               coalesce(cls.classes, '{}') AS classes
        FROM entities e
        LEFT JOIN (SELECT entity_id, count(DISTINCT memory_id) AS support
                   FROM entity_links GROUP BY entity_id) s ON s.entity_id = e.node_id
        LEFT JOIN (SELECT ecl.instance_id,
                          array_agg(c.canonical_name ORDER BY c.canonical_name)
                              AS classes
                   FROM entity_class_links ecl
                   JOIN entities c ON c.node_id = ecl.class_id
                   GROUP BY ecl.instance_id) cls ON cls.instance_id = e.node_id
    """

    @staticmethod
    def _entity_profile_dict(row: dict) -> dict:
        return {"id": str(row["id"]), "name": row["name"],
                "entity_type": row["entity_type"], "status": row["status"],
                "description": row["description"], "aliases": list(row["aliases"]),
                "classes": list(row["classes"]), "mentions": row["mentions"],
                "pinned": row["pinned"], "kind": row["kind"],
                "occurred_at": row["occurred_at"].isoformat()
                    if row["occurred_at"] else None}

    def find_entities(self, query: str, *, embedding: "list[float] | None" = None,
                      limit: int = 10, conn=None) -> "list[dict]":
        """Resolve a name, misspelling, or DESCRIPTION to ACTIVE entities — a
        four-rung ladder, every hit tagged `via` + `score`:

          name         exact/prefix/substring on canonical_name/aliases, tiered,
                       mention-support tiebreak — precise, always first.
          fuzzy-name   pg_trgm similarity on canonical_name/aliases — typos.
          description  cosine over the entity's own name+description embedding
                       (resolve daemon keeps it fresh) — "the logistics
                       startup building a route planner" resolves with zero name
                       overlap.
          memory-vote  embedding-kNN over memories; entities scored by the
                       similarity-weighted entity_links weight of their appearances
                       in the top hits — resolution through evidence, robust to
                       thin or garbled descriptions.

        Rungs 3-4 need `embedding` (the caller embeds the query — same layering as
        search()). All rungs run; results dedupe by entity keeping the earliest
        (most precise) rung's tag. A fragmented referent (one person under several spellings)
        surfaces as several hits rather than one winner — intentional.

        Every rung is read-only, so a caller may pass `conn=` to run all four inside
        one pinned snapshot.
        """
        q = str(query).strip()
        if not q:
            return []
        esc = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        limit = max(1, min(int(limit), 50))
        ranked: "list[tuple[object, str, float]]" = []   # (node_id, via, score)
        # conn= lets a caller run this inside its own pinned read_snapshot(). The
        # crawl's entry step does exactly that, and without it the name-anchor rung
        # would resolve entities from a different DB state than the concept and
        # bridge frontier calls in the same query. nullcontext yields the caller's
        # connection and does NOT commit or return it to the pool on exit.
        with (nullcontext(conn) if conn is not None
              else self._pool.connection()) as conn:
            rows = conn.execute(
                """SELECT e.node_id, t.tier,
                          coalesce(s.support, 0) AS support
                   FROM entities e
                   LEFT JOIN (SELECT entity_id, count(DISTINCT memory_id) AS support
                              FROM entity_links GROUP BY entity_id) s
                          ON s.entity_id = e.node_id
                   , LATERAL (SELECT CASE
                       WHEN lower(e.canonical_name) = lower(%(q)s) THEN 0
                       WHEN EXISTS (SELECT 1 FROM unnest(e.aliases) a
                                    WHERE lower(a) = lower(%(q)s)) THEN 0
                       WHEN e.canonical_name ILIKE %(pre)s THEN 1
                       ELSE 2 END AS tier) t
                   WHERE e.status = %(active)s
                     AND (e.canonical_name ILIKE %(sub)s
                          OR EXISTS (SELECT 1 FROM unnest(e.aliases) a
                                     WHERE a ILIKE %(sub)s))
                   ORDER BY t.tier, support DESC NULLS LAST, e.canonical_name
                   LIMIT %(lim)s""",
                {"q": q, "pre": f"{esc}%", "sub": f"%{esc}%",
                 "active": STATUS_ACTIVE, "lim": limit}).fetchall()
            ranked += [(r["node_id"], "name", round(1.0 - 0.1 * r["tier"], 3))
                       for r in rows]
            rows = conn.execute(
                """SELECT e.node_id, t.sim
                   FROM entities e,
                   LATERAL (SELECT greatest(
                       similarity(e.canonical_name, %(q)s),
                       coalesce((SELECT max(similarity(a, %(q)s))
                                 FROM unnest(e.aliases) a), 0)) AS sim) t
                   WHERE e.status = %(active)s AND t.sim >= %(min)s
                   ORDER BY t.sim DESC LIMIT %(lim)s""",
                {"q": q, "active": STATUS_ACTIVE,
                 "min": FUZZY_NAME_MIN_SIM, "lim": limit}).fetchall()
            ranked += [(r["node_id"], "fuzzy-name", round(float(r["sim"]), 3))
                       for r in rows]
            if embedding is not None:
                vec = "[" + ",".join(repr(float(x)) for x in embedding) + "]"
                rows = conn.execute(
                    """SELECT e.node_id,
                              1 - (e.embedding <=> %(v)s::vector) AS sim
                       FROM entities e
                       WHERE e.status = %(active)s AND e.embedding IS NOT NULL
                         AND 1 - (e.embedding <=> %(v)s::vector) >= %(min)s
                       ORDER BY e.embedding <=> %(v)s::vector
                       LIMIT %(lim)s""",
                    {"v": vec, "active": STATUS_ACTIVE,
                     "min": ENTITY_DESC_MIN_SIM, "lim": limit}).fetchall()
                ranked += [(r["node_id"], "description", round(float(r["sim"]), 3))
                           for r in rows]
                rows = conn.execute(
                    """WITH near AS (
                           SELECT id, 1 - (embedding <=> %(v)s::vector) AS sim
                           FROM nodes WHERE embedding IS NOT NULL
                           ORDER BY embedding <=> %(v)s::vector
                           LIMIT %(pool)s)
                       SELECT el.entity_id AS node_id,
                              sum(coalesce(el.weight, 1.0) * near.sim) AS vote
                       FROM near
                       JOIN entity_links el ON el.memory_id = near.id
                       JOIN entities e ON e.node_id = el.entity_id
                                      AND e.status = %(active)s
                       GROUP BY el.entity_id
                       ORDER BY vote DESC LIMIT %(lim)s""",
                    {"v": vec, "active": STATUS_ACTIVE,
                     "pool": MEMORY_VOTE_POOL, "lim": limit}).fetchall()
                top_vote = max((float(r["vote"]) for r in rows), default=1.0) or 1.0
                ranked += [(r["node_id"], "memory-vote",
                            round(float(r["vote"]) / top_vote, 3)) for r in rows]
            seen: set = set()
            picked = []
            for nid, via, score in ranked:
                if nid in seen:
                    continue
                seen.add(nid)
                picked.append((nid, via, score))
                if len(picked) >= limit:
                    break
            if not picked:
                return []
            profiles = {r["id"]: r for r in conn.execute(
                f"{self._ENTITY_PROFILE} WHERE e.node_id = ANY(%(ids)s)",
                {"ids": [nid for nid, _, _ in picked]}).fetchall()}
        return [{**self._entity_profile_dict(profiles[nid]),
                 "via": via, "score": score}
                for nid, via, score in picked if nid in profiles]

    def entity_profile(self, node_id, *, conn=None) -> "dict | None":
        """One entity by node id in the find_entities row shape (any status — the
        status field says whether it is live). None if the id is not an entity.

        `mentions` is `count(DISTINCT memory_id)` over `entity_links` — the SAME
        quantity `bridge_frontier` returns as `entity_support` and `concept_frontier`
        returns as `support`, so a caller may mix the three sources in one
        disclosure block without them disagreeing.
        """
        with (nullcontext(conn) if conn is not None
              else self._pool.connection()) as c:
            row = c.execute(
                f"{self._ENTITY_PROFILE} WHERE e.node_id = %(i)s",
                {"i": str(UUID(str(node_id)))}).fetchone()
        return self._entity_profile_dict(row) if row else None

    # ---- entity classes (broad categories) --------------------------------
    def upsert_entity_class(self, name: str, *, active: bool = True,
                            description: str | None = None,
                            kind: str | None = None) -> Entity:
        """Create or update a broad CATEGORY entity (entity_type='class') at the top level,
        entities.<slug>. Identity dedups by normalized name.

        `active` gates whether the category counts as established: the LLM discover pass
        creates classes with active=False (they must recur past the threshold before they're
        offered as a category), while explicit/manual creation and instance promotion pass
        active=True. Two monotonicity rules hold via the status precedence below:
          - once active, a later discover (active=False) never demotes it back to inactive;
          - once BLOCKED (human-removed), nothing here can revive it — the tombstone wins.

        `kind` is the structural axis: a name the policy's taxonomy knows
        (`Policy.taxonomy.kind_for`: a category's kind, else a kind seed) always wins over the
        caller/model's `kind` — the policy's vocabulary is curated, the model's guess is not.
        Effective kind is dropped to None when it is not one of
        `KINDS`, so a model's junk value can never trip the column's CHECK constraint. First
        write wins on conflict (COALESCE below): once a class has a kind, later upserts never
        overwrite it with a different (possibly wrong) guess.
        """
        # The description here is SLM-drafted (the discover pass); drop a degenerate draft so a
        # repetition spiral never reaches the column. None leaves the existing description
        # untouched via the CONFLICT clause below. Human-set descriptions take the locked
        # `set_class_description` path instead and bypass this entirely.
        description = clean_class_description(description)
        effective_kind = self._policy.taxonomy.kind_for(name) or kind
        if effective_kind not in KINDS:
            effective_kind = None
        record = NodeRecord(
            source=ENTITY_SOURCE, external_id=entity_external_id(ENTITY_CLASS, name),
            type=ENTITY_TYPE, content=description or name, slug=name,
            parent_path=ENTITY_ROOT, title=name,
        )
        validate(record, self._registry)
        want = STATUS_ACTIVE if active else STATUS_INACTIVE
        with self._pool.connection() as conn, conn.transaction():
            node = self._persist_node(conn, record)
            row = conn.execute(
                """INSERT INTO entities
                       (node_id, entity_type, canonical_name, aliases, description, status, kind)
                   VALUES (%(id)s, %(t)s, %(n)s, '{}', %(d)s, %(want)s, %(k)s)
                   ON CONFLICT (node_id) DO UPDATE SET
                       entity_type = EXCLUDED.entity_type,
                       canonical_name = EXCLUDED.canonical_name,
                       description = CASE
                           WHEN entities.description_locked THEN entities.description
                           WHEN EXCLUDED.description IS NULL
                                OR EXCLUDED.description = '' THEN entities.description
                           ELSE EXCLUDED.description END,
                       status = CASE
                           WHEN entities.status = 'blocked' THEN 'blocked'
                           WHEN entities.status = 'active'
                                OR EXCLUDED.status = 'active' THEN 'active'
                           ELSE EXCLUDED.status END,
                       kind = COALESCE(entities.kind, EXCLUDED.kind),
                       embed_dirty = true
                   RETURNING status, description""",
                {"id": node.id, "t": ENTITY_CLASS, "n": name, "d": description,
                 "want": want, "k": effective_kind},
            ).fetchone()
        return Entity(node=node, entity_type=ENTITY_CLASS, canonical_name=name,
                      aliases=[], description=row["description"], status=row["status"])

    def resolve_class_name(self, name: str) -> str:
        """Fold a would-be-new class name onto an existing class when it is a spelling
        variant ('conpany' -> 'company'), else return it unchanged. The deterministic
        typo gate: misspellings become votes for the real category instead
        of minting a fragment category. Blocked classes are included on purpose — a
        typo of a tombstoned name must keep resolving to the tombstone so the vote
        dies there rather than re-minting a live variant."""
        from kb_core.entities import class_name_typo_of
        with self._pool.connection() as conn:
            existing = [r["canonical_name"] for r in conn.execute(
                "SELECT canonical_name FROM entities WHERE entity_type = 'class'").fetchall()]
        return class_name_typo_of(name, existing) or name

    def find_merge_candidates(
        self,
        *,
        entity_ids: "list | None" = None,
        max_distance: float = 0.20,
        top_k: int = 5,
    ) -> "list[dict]":
        """Same-kind nearest-neighbour merge candidate scan.

        For each seed entity (or ALL entities with a non-null embedding when `entity_ids` is
        None), run a pgvector KNN among entities of the SAME entity_type with a non-null
        embedding; keep only pairs whose cosine distance is <= max_distance.

        Two generator arms, deduplicated across arms (embedding wins the `via` tag):

        embedding — the KNN scan above; catches semantic near-duplicates whose
            names may differ entirely.
        name — lexical scan over ACTIVE same-kind pairs: trigram similarity across
            canonical names AND aliases (>= NAME_MERGE_MIN_SIM), or word-boundary
            containment ('Avery' ⊂ 'Avery Quinn'). Needs no embeddings — this
            is what catches duplicates whose DESCRIPTIONS diverge (a person's two
            personas sit far apart in embedding space precisely because they are
            described differently), which the embedding arm is structurally blind
            to.

        Each pair is returned canonical-ordered (min, max by str(uuid)) and deduplicated.
        Pairs already present in entity_merge_candidates (any status) or entity_merge_blocks
        are excluded.

        Returns a list of dicts: {"left_id", "right_id", "kind", "distance", "via"}.
        Name-arm distance is the pair's embedding distance when both are embedded,
        else 1 - name_similarity.
        """
        raw: "list[tuple]" = []          # (l, r, kind, distance, via), arm order
        with self._pool.connection() as conn:
            if entity_ids is not None:
                seeds = list(entity_ids)
            else:
                seeds = [
                    r["node_id"]
                    for r in conn.execute(
                        "SELECT node_id FROM entities WHERE embedding IS NOT NULL"
                    ).fetchall()
                ]
            for sid in seeds:
                rows = conn.execute(
                    """WITH me AS (
                           SELECT entity_type, embedding
                           FROM entities
                           WHERE node_id = %(id)s
                       )
                       SELECT e.node_id          AS nid,
                              (e.embedding <=> me.embedding) AS dist,
                              e.entity_type      AS kind
                       FROM entities e, me
                       WHERE e.node_id <> %(id)s
                         AND e.embedding IS NOT NULL
                         AND e.entity_type = me.entity_type
                         AND (e.embedding <=> me.embedding) <= %(maxd)s
                       ORDER BY e.embedding <=> me.embedding
                       LIMIT %(k)s""",
                    {"id": sid, "maxd": max_distance, "k": top_k},
                ).fetchall()
                raw += [(sid, r["nid"], r["kind"], float(r["dist"]), "embedding")
                        for r in rows]
            name_params: dict = {"active": STATUS_ACTIVE, "minsim": NAME_MERGE_MIN_SIM,
                                 "seeds": ([str(s) for s in entity_ids]
                                           if entity_ids is not None else None)}
            name_rows = conn.execute(
                """SELECT a.node_id AS l, b.node_id AS r, a.entity_type AS kind,
                          t.name_sim,
                          CASE WHEN a.embedding IS NOT NULL
                                AND b.embedding IS NOT NULL
                               THEN (a.embedding <=> b.embedding) END AS emb_dist
                   FROM entities a
                   JOIN entities b ON b.entity_type = a.entity_type
                                  AND a.node_id < b.node_id
                   CROSS JOIN LATERAL (SELECT
                       coalesce((SELECT max(similarity(x, y))
                                 FROM unnest(a.aliases
                                             || ARRAY[a.canonical_name]) x,
                                      unnest(b.aliases
                                             || ARRAY[b.canonical_name]) y), 0)
                           AS name_sim) t
                   WHERE a.status = %(active)s AND b.status = %(active)s
                     AND (%(seeds)s::uuid[] IS NULL
                          OR a.node_id = ANY(%(seeds)s::uuid[])
                          OR b.node_id = ANY(%(seeds)s::uuid[]))
                     AND (t.name_sim >= %(minsim)s
                          OR lower(b.canonical_name)
                                 LIKE lower(a.canonical_name) || ' %%'
                          OR lower(a.canonical_name)
                                 LIKE lower(b.canonical_name) || ' %%')""",
                name_params).fetchall()
            raw += [(r["l"], r["r"], r["kind"],
                     float(r["emb_dist"]) if r["emb_dist"] is not None
                     else round(1.0 - float(r["name_sim"]), 4), "name")
                    for r in name_rows]

            out: list[dict] = []
            seen: set[tuple] = set()
            for left, right, kind, dist, via in raw:
                lo, hi = (left, right) if str(left) < str(right) else (right, left)
                if (lo, hi) in seen:
                    continue
                seen.add((lo, hi))
                staged = conn.execute(
                    "SELECT 1 FROM entity_merge_candidates"
                    " WHERE left_id = %s AND right_id = %s",
                    (lo, hi),
                ).fetchone()
                blocked = conn.execute(
                    "SELECT 1 FROM entity_merge_blocks"
                    " WHERE left_id = %s AND right_id = %s",
                    (lo, hi),
                ).fetchone()
                if staged or blocked:
                    continue
                out.append({"left_id": lo, "right_id": hi, "kind": kind,
                            "distance": dist, "via": via})
        return out

    # ---- merge judge / staging -----------------------------------------------

    @staticmethod
    def _mention_lines(rows) -> "list[str]":
        """Format mention rows (title, snippet, path) as prompt context lines, each prefixed
        with the folder the memory is filed under: '[projects › kb_mcp] <title>: <snippet>'.
        The folder is deliberate semantic signal for the judges (where a note lives says what
        a name means there); a top-level note gets no prefix."""
        from kb_core.entities import mention_snippet

        out = []
        for r in rows:
            line = f"{r['title'] or ''}: {mention_snippet(r['snippet'])}".strip(": ")
            folder = humanize_path(parent_of(r["path"] or ""))
            out.append(f"[{folder}] {line}" if folder else line)
        return out

    def _entity_judge_view(self, entity_id: "UUID") -> dict:
        """Gather name, aliases, description, and a short list of mention snippets for one entity.

        Opens its own pool connection (called per-candidate, not inside a held connection).
        Mirrors the query shape of _entity_embed_inputs but returns a lightweight dict
        suitable for build_judge_prompt: mentions is list[str] of
        "[<folder breadcrumb>] <title>: <snippet>" strings (see _mention_lines).
        """
        from kb_core.entities import ENTITY_CLASS, MENTIONS, BELONGS_TO

        with self._pool.connection() as conn:
            row = conn.execute(
                "SELECT canonical_name, aliases, description, entity_type "
                "FROM entities WHERE node_id = %s",
                (entity_id,),
            ).fetchone()
            if row is None:
                raise ValueError(f"Entity {entity_id} not found")

            kind: str = row["entity_type"]
            link_type = BELONGS_TO if kind == ENTITY_CLASS else MENTIONS

            # Rank by evidential value, not recency. The entity's own page (titled with its
            # name) defines it; a memory it is the SUBJECT of says what it IS; the newest
            # memory may only name it in a signature line. Recency stays as the tiebreak, so
            # behavior is unchanged for entities whose links carry no such signal. Both
            # boolean keys are coalesced to false: a bare `DESC NULLS LAST` sorts
            # true > false > NULL, which ranked every non-subject role (owner, participant)
            # above role-less links. `left(content, 1200)` bounds the read; mention_snippet
            # then skips the header block.
            mention_rows = conn.execute(
                "SELECT n.title, left(n.content, 1200) AS snippet, n.path::text AS path "
                "FROM nodes n "
                "JOIN entity_links el ON el.memory_id = n.id "
                "WHERE el.entity_id = %s AND el.link_type = %s "
                "ORDER BY coalesce(lower(btrim(n.title)) = lower(%s), false) DESC, "
                "         coalesce(el.role = 'subject', false) DESC, "
                "         el.weight DESC NULLS LAST, "
                "         n.updated_at DESC NULLS LAST "
                "LIMIT 5",
                (entity_id, link_type, row["canonical_name"]),
            ).fetchall()

        mentions = self._mention_lines(mention_rows)
        return dict(
            name=row["canonical_name"],
            aliases=list(row["aliases"] or []),
            description=row["description"] or "",
            mentions=mentions,
        )

    def candidate_judge_view(self, entity_type: str, normalized_name: str) -> dict:
        """Name + up to 5 mention snippets for a not-yet-promoted candidate GROUP — the
        candidate-level counterpart of _entity_judge_view (which needs an existing node).
        Feeds build_meaning_prompt. Pure read."""
        with self._pool.connection() as conn:
            name_row = conn.execute(
                "SELECT max(raw_name) AS name FROM entity_candidates "
                "WHERE entity_type = %(t)s AND normalized_name = %(n)s",
                {"t": entity_type, "n": normalized_name}).fetchone()
            mention_rows = conn.execute(
                "SELECT n.title, left(n.content, 200) AS snippet, n.path::text AS path "
                "FROM nodes n "
                "WHERE n.id IN (SELECT memory_id FROM entity_candidates "
                "  WHERE entity_type = %(t)s AND normalized_name = %(n)s AND promoted = false) "
                "ORDER BY n.updated_at DESC NULLS LAST LIMIT 5",
                {"t": entity_type, "n": normalized_name}).fetchall()
        mentions = self._mention_lines(mention_rows)
        name = (name_row and name_row["name"]) or normalized_name
        return {"name": name, "mentions": mentions}

    def _pair_judge_evidence(self, a: "UUID", b: "UUID") -> dict:
        """Graph evidence for the merge judge: how many memories mention BOTH
        entities (two people/orgs appearing together in one note are distinct
        actors — strong counter-evidence), and how many other entities their
        memory neighborhoods share (supports identity when the names are similar)."""
        with self._pool.connection() as conn:
            co = conn.execute(
                """SELECT count(DISTINCT x.memory_id) AS n
                   FROM entity_links x
                   JOIN entity_links y ON y.memory_id = x.memory_id
                   WHERE x.entity_id = %(a)s AND y.entity_id = %(b)s""",
                {"a": a, "b": b}).fetchone()["n"]
            shared = conn.execute(
                """SELECT count(*) AS n FROM (
                       SELECT DISTINCT o.entity_id
                       FROM entity_links m
                       JOIN entity_links o ON o.memory_id = m.memory_id
                       WHERE m.entity_id = %(a)s
                         AND o.entity_id NOT IN (%(a)s, %(b)s)
                       INTERSECT
                       SELECT DISTINCT o.entity_id
                       FROM entity_links m
                       JOIN entity_links o ON o.memory_id = m.memory_id
                       WHERE m.entity_id = %(b)s
                         AND o.entity_id NOT IN (%(a)s, %(b)s)) s""",
                {"a": a, "b": b}).fetchone()["n"]
        return {"co_mentions": co, "shared_co_entities": shared}

    def _suggested_target(self, a: "UUID", b: "UUID") -> "UUID":
        """Pick the more-established survivor: more mentions, then longer canonical_name.

        Counts come from entity_links (link_type='mentions'), NOT the edges table.
        """
        from kb_core.entities import MENTIONS

        def rank(eid: "UUID") -> "tuple[int, int]":
            with self._pool.connection() as conn:
                m = conn.execute(
                    "SELECT COUNT(*) AS n FROM entity_links WHERE entity_id = %s AND link_type = %s",
                    (eid, MENTIONS),
                ).fetchone()["n"]
                nm = conn.execute(
                    "SELECT canonical_name FROM entities WHERE node_id = %s", (eid,)
                ).fetchone()
            return (m, len(nm["canonical_name"]) if nm else 0)

        return a if rank(a) >= rank(b) else b

    def stage_merge_candidate(
        self,
        left_id: "UUID",
        right_id: "UUID",
        kind: str,
        distance: float,
        judge_reason: str,
        suggested_target_id: "UUID",
    ) -> int:
        """INSERT a confirmed-same pair into entity_merge_candidates (status='pending').

        ON CONFLICT (left_id, right_id) DO NOTHING — idempotent. Pairs must already be
        canonical-ordered by the caller. Returns the new row id, or 0 if already present.
        """
        with self._pool.connection() as conn:
            row = conn.execute(
                """INSERT INTO entity_merge_candidates
                       (left_id, right_id, kind, distance, judge_same, judge_reason,
                        suggested_target_id, status)
                   VALUES (%s, %s, %s, %s, true, %s, %s, 'pending')
                   ON CONFLICT (left_id, right_id) DO NOTHING RETURNING id""",
                (left_id, right_id, kind, distance, judge_reason, suggested_target_id),
            ).fetchone()
        return row["id"] if row else 0

    def block_merge(self, a: "UUID", b: "UUID", *, origin: str, reason: "str | None" = None) -> None:
        """Permanently block a pair from being re-suggested.

        Canonical-orders (a, b) via merge_pair before inserting into entity_merge_blocks.
        ON CONFLICT DO NOTHING — idempotent.
        """
        from kb_core.entities import merge_pair
        lo, hi = merge_pair(a, b)
        with self._pool.connection() as conn:
            conn.execute(
                """INSERT INTO entity_merge_blocks (left_id, right_id, origin, reason)
                   VALUES (%s, %s, %s, %s) ON CONFLICT (left_id, right_id) DO NOTHING""",
                (lo, hi, origin, reason),
            )

    def list_merge_suggestions(self, kind: "str | None" = None) -> "list[dict]":
        """Return pending merge candidates joined to both entities' display fields.

        Each dict: {id, kind, distance(float), judge_reason, suggested_target_id,
                    left: {id, name, aliases, mention_count},
                    right: {id, name, aliases, mention_count}}.
        Ordered by distance ASC. Optionally filtered to a specific kind ('instance'/'class').
        """
        with self._pool.connection() as conn:
            rows = conn.execute(
                """SELECT c.id, c.kind, c.distance, c.judge_reason, c.suggested_target_id,
                          c.left_id, c.right_id,
                          le.canonical_name AS l_name, le.aliases AS l_aliases,
                          re.canonical_name AS r_name, re.aliases AS r_aliases
                   FROM entity_merge_candidates c
                   JOIN entities le ON le.node_id = c.left_id
                   JOIN entities re ON re.node_id = c.right_id
                   WHERE c.status = 'pending' AND (%(k)s::text IS NULL OR c.kind = %(k)s::text)
                   ORDER BY c.distance ASC""",
                {"k": kind}).fetchall()

        def side(nid, name, aliases):
            with self._pool.connection() as conn:
                mc = conn.execute(
                    "SELECT COUNT(*) AS n FROM entity_links WHERE entity_id = %s AND link_type = %s",
                    (nid, MENTIONS)).fetchone()["n"]
            return {"id": nid, "name": name, "aliases": aliases, "mention_count": mc}

        return [{"id": r["id"], "kind": r["kind"], "distance": float(r["distance"]),
                 "judge_reason": r["judge_reason"], "suggested_target_id": r["suggested_target_id"],
                 "left": side(r["left_id"], r["l_name"], r["l_aliases"]),
                 "right": side(r["right_id"], r["r_name"], r["r_aliases"])}
                for r in rows]

    def approve_merge(self, candidate_id: int, target_id: "UUID | None" = None) -> "Entity | None":
        """Fold the two entities in a pending candidate via the existing merge machinery.

        Loads the pending candidate WITH `FOR UPDATE`, holding the row lock until this
        transaction claims it (status -> 'approving') below. Two concurrent approve_merge
        calls for the same candidate_id used to both read status='pending' in their own
        transaction and both proceed to merge_instances/merge_classes, which are individually
        idempotent (ON CONFLICT DO NOTHING links, a DELETE that matches zero rows the second
        time) and so BOTH returned a non-None survivor — the observable bug was two
        `entity_merges` audit rows for one merge, not a crash. `FOR UPDATE` + the claim UPDATE
        closes that: the second caller's `FOR UPDATE` blocks until the first transaction
        commits, then re-reads status as 'approving' (not 'pending'), so its own `WHERE
        status = 'pending'` matches zero rows and it returns None before ever calling a merge
        method. This was reachable only after MCP tools moved to worker threads;
        before that, FastMCP's inline event-loop execution serialised every tool call and was
        the guard.

        If target_id is provided it must be one of the candidate's two sides (returns None
        otherwise, claim never taken, row stays 'pending' — same as before this fix). Derives
        target = target_id or suggested_target_id; source = the other side. Instance kind →
        merge_instances(source, target). Class kind → merge_classes(source_name, target_name)
        — names are captured BEFORE merge_classes tombstones the source.

        On a failed merge (survivor is None), the claim is released back to 'pending' so a
        caller can retry — matching the pre-fix behaviour, where a failed merge never touched
        status at all. On success, the row is finalized to 'approved' — needed because that
        finalization is no longer a no-op for kind='class': merge_classes tombstones the
        source via `block_class`, which only sets `entities.status = 'blocked'` and never
        deletes the node, so (unlike kind='instance', where the node's ON DELETE CASCADE
        already removes this row) nothing would ever move this row off 'approving' otherwise.
        Also inserts one row into entity_merges (audit log). Returns the surviving entity, or
        None if the candidate is not found/pending, target_id is not one of the candidate's
        two sides, or the merge itself failed.
        """
        with self._pool.connection() as conn, conn.transaction():
            c = conn.execute(
                "SELECT left_id, right_id, kind, suggested_target_id, distance, judge_reason "
                "FROM entity_merge_candidates WHERE id = %s AND status = 'pending' FOR UPDATE",
                (candidate_id,)).fetchone()
            if c is None:
                return None
            # Validate target_id override: it must be one of the two candidate sides.
            if target_id is not None and target_id not in (c["left_id"], c["right_id"]):
                return None
            # Claim the row while still holding the FOR UPDATE lock, so the status change
            # is visible to a concurrent caller as soon as (and only once) this commits.
            conn.execute(
                "UPDATE entity_merge_candidates SET status = 'approving' WHERE id = %s",
                (candidate_id,))
        target = target_id or c["suggested_target_id"]
        source = c["left_id"] if target == c["right_id"] else c["right_id"]
        # Capture source name BEFORE the merge tombstones the source entity.
        src_entity = self.get_entity(source)
        source_name = src_entity.canonical_name if src_entity else None
        if c["kind"] == ENTITY_INSTANCE:
            survivor = self.merge_instances(source, target)
        else:
            # Also capture target name for merge_classes (which takes names, not ids).
            tgt_entity = self.get_entity(target)
            target_name = tgt_entity.canonical_name if tgt_entity else None
            survivor = self.merge_classes(source_name, target_name)
        with self._pool.connection() as conn:
            if survivor is None:
                # Release the claim: matches (kind='class') or is a harmless no-op on an
                # already CASCADE-deleted row (kind='instance').
                conn.execute(
                    "UPDATE entity_merge_candidates SET status = 'pending' WHERE id = %s",
                    (candidate_id,))
                return None
            # Finalize the claim. Matches zero rows for kind='instance' (CASCADE-deleted by
            # the node tombstone); for kind='class' this is what actually moves the row off
            # 'pending'/'approving' — see the docstring.
            conn.execute(
                "UPDATE entity_merge_candidates SET status = 'approved', resolved_at = now() "
                "WHERE id = %s", (candidate_id,))
            conn.execute(
                """INSERT INTO entity_merges
                       (survivor_id, source_name, kind, distance, judge_reason)
                   VALUES (%s, %s, %s, %s, %s)""",
                (survivor.node.id, source_name, c["kind"], c["distance"], c["judge_reason"]))
        return survivor

    def reject_merge(self, candidate_id: int, reason: "str | None" = None) -> bool:
        """Mark a pending candidate rejected and permanently block re-suggestion of the pair.

        If no pending candidate with that id exists, returns False. Otherwise marks
        status='rejected', resolved_at=now(), and calls block_merge with origin='human'.
        Returns True.
        """
        with self._pool.connection() as conn:
            c = conn.execute(
                "SELECT left_id, right_id FROM entity_merge_candidates "
                "WHERE id = %s AND status = 'pending'", (candidate_id,)).fetchone()
            if c is None:
                return False
            conn.execute(
                "UPDATE entity_merge_candidates SET status='rejected', resolved_at=now() "
                "WHERE id = %s", (candidate_id,))
        self.block_merge(c["left_id"], c["right_id"], origin="human", reason=reason)
        return True

    def judge_and_stage_candidates(self, client: "object", candidates: "list[dict]") -> dict:
        """Run the LLM judge over each candidate pair; stage confirmed-same or block rejected.

        For each candidate dict {left_id, right_id, kind, distance}:
          - Builds entity views via _entity_judge_view
          - Calls client.complete(system, user, schema=JUDGE_SCHEMA)
          - If same → stage_merge_candidate with _suggested_target
          - Else → block_merge(origin='judge')

        Returns {"staged": int, "blocked": int}.
        """
        from kb_core.merge_judge import build_judge_prompt, parse_judge, JUDGE_SCHEMA

        staged = blocked = 0
        for c in candidates:
            left = self._entity_judge_view(c["left_id"])
            right = self._entity_judge_view(c["right_id"])
            via = c.get("via", "embedding")
            evidence = {**self._pair_judge_evidence(c["left_id"], c["right_id"]),
                        "via": via}
            sys_msg, usr_msg = build_judge_prompt(left, right, evidence)
            same, reason = parse_judge(client.complete(sys_msg, usr_msg, schema=JUDGE_SCHEMA))
            reason = f"[{via}] {reason}"       # provenance survives into the queue
            if same:
                tgt = self._suggested_target(c["left_id"], c["right_id"])
                if self.stage_merge_candidate(
                    c["left_id"], c["right_id"], c["kind"], c["distance"], reason, tgt
                ):
                    staged += 1
            else:
                self.block_merge(c["left_id"], c["right_id"], origin="judge", reason=reason)
                blocked += 1
        return {"staged": staged, "blocked": blocked}

    # ---- entity descriptions (background describe step) -----------------------

    def entities_missing_description(self, *, limit: int) -> "list[dict]":
        """Up to `limit` active entities with a blank description, as {node_id, kind}.

        Instances qualify immediately after promotion (the going-forward path); active
        classes are the backstop for ones discover_classes left blank. Inactive class
        candidates, blocked tombstones, and human-locked rows are excluded. Because
        selection is blank-only, the describe step is fill-when-empty by construction
        and can never churn an existing description.
        """
        from kb_core.entities import STATUS_ACTIVE

        with self._pool.connection() as conn:
            rows = conn.execute(
                """SELECT e.node_id, e.entity_type AS kind
                   FROM entities e JOIN nodes n ON n.id = e.node_id
                   WHERE e.status = %(active)s
                     AND NOT e.description_locked
                     AND (e.description IS NULL OR btrim(e.description) = '')
                   ORDER BY n.updated_at DESC NULLS LAST
                   LIMIT %(limit)s""",
                {"active": STATUS_ACTIVE, "limit": limit},
            ).fetchall()
        return [{"node_id": r["node_id"], "kind": r["kind"]} for r in rows]

    def set_entity_description(self, node_id: "UUID", text: str) -> None:
        """Persist a model-drafted description and mark the entity for re-embed.

        Mirrors _upsert_instance_node's `content = description or name` so the node's
        embed text picks up the richer wording. Deliberately does NOT set
        description_locked — that flag is for human edits, which keep precedence.
        """
        with self._pool.connection() as conn:
            conn.execute(
                "UPDATE entities SET description = %(t)s, embed_dirty = true "
                "WHERE node_id = %(id)s",
                {"t": text, "id": node_id},
            )
            conn.execute(
                "UPDATE nodes SET content = %(t)s, updated_at = now() WHERE id = %(id)s",
                {"t": text, "id": node_id},
            )

    def describe_missing_entities(self, client: "object", *, limit: int = 16) -> int:
        """Draft a one-line description for each blank entity; returns how many stuck.

        One client.complete call per entity. A degenerate draft (clean_class_description
        → None) or a per-entity failure writes nothing — the entity stays blank and is
        re-selected on a later tick, so descriptions are always real model drafts.
        A ProviderUnavailable is not a per-entity failure: it propagates at once (one
        timeout per sweep, not one per entity) so the caller can pause the provider.
        """
        from kb_core.describe import DESCRIBE_SCHEMA, build_describe_prompt, parse_describe
        from kb_core.entities import clean_class_description
        from kb_core.ingest.inference import ProviderUnavailable

        described = 0
        for row in self.entities_missing_description(limit=limit):
            try:
                view = self._entity_judge_view(row["node_id"])
                sys_msg, usr_msg = build_describe_prompt(view, row["kind"])
                draft = parse_describe(client.complete(sys_msg, usr_msg, schema=DESCRIBE_SCHEMA))
                text = clean_class_description(draft)
                if text:
                    self.set_entity_description(row["node_id"], text)
                    described += 1
            except ProviderUnavailable:
                raise     # the provider is down, not this entity: stop the sweep
            except Exception:
                continue  # never let one bad entity abort the sweep
        return described

    def entity_class_names(self, *, limit: int | None = None) -> "list[str]":
        """The category names offered to the LLM as 'the categories we have' — the top-N
        ACTIVE classes ranked by how many memories belong to them (a `belongs_to` member
        count), strongest first. Three things fall out of this one query:
          - inactive (not-yet-recurring) and blocked classes are withheld;
          - the taxonomy is bounded: at most `limit` (default DEFAULT_MAX_ACTIVE_CLASSES)
            categories are ever offered, so they compete for slots instead of accreting;
          - demotion is implicit — a class that stops gaining members slips below the cap
            and silently drops out of the offered set without any status change."""
        cap = limit if limit is not None else DEFAULT_MAX_ACTIVE_CLASSES
        with self._pool.connection() as conn:
            rows = conn.execute(
                """SELECT e.canonical_name,
                          count(DISTINCT el.memory_id) AS members
                   FROM entities e
                   LEFT JOIN entity_links el
                     ON el.entity_id = e.node_id AND el.link_type = %(bt)s
                   WHERE e.entity_type = %(t)s AND e.status = 'active'
                   GROUP BY e.canonical_name
                   ORDER BY members DESC, e.canonical_name
                   LIMIT %(cap)s""",
                {"t": ENTITY_CLASS, "bt": BELONGS_TO, "cap": cap},
            ).fetchall()
        return [r["canonical_name"] for r in rows]

    def entity_classes_with_descriptions(
            self, *, limit: int | None = None) -> "list[tuple[str, str]]":
        """Like `entity_class_names`, but pairs each active category with its description (empty
        string when unset) — the grounding injected into the LLM passes. Same ranking and cap."""
        cap = limit if limit is not None else DEFAULT_MAX_ACTIVE_CLASSES
        with self._pool.connection() as conn:
            rows = conn.execute(
                """SELECT e.canonical_name, COALESCE(e.description, '') AS description,
                          count(DISTINCT el.memory_id) AS members
                   FROM entities e
                   LEFT JOIN entity_links el
                     ON el.entity_id = e.node_id AND el.link_type = %(bt)s
                   WHERE e.entity_type = %(t)s AND e.status = 'active'
                   GROUP BY e.canonical_name, e.description
                   ORDER BY members DESC, e.canonical_name
                   LIMIT %(cap)s""",
                {"t": ENTITY_CLASS, "bt": BELONGS_TO, "cap": cap},
            ).fetchall()
        return [(r["canonical_name"], r["description"]) for r in rows]

    def entity_classes_for_prompt(
            self, *, limit: int | None = None) -> "list[tuple[str, str]]":
        """The category list injected into the LLM passes as {entity_classes}.

        Unlike `entity_classes_with_descriptions`, this also surfaces the strongest *inactive*
        candidates so the model can reuse an emerging name instead of minting a near-duplicate.
        Returns (name, description) pairs, capped at `limit` (default DEFAULT_PROMPT_CLASSES):
          - established (active) classes first, strongest first, each WITH its description (the
            grounding for what belongs in it);
          - then the top not-yet-active candidates, name only (no description — they're still
            just votes, not a settled meaning).
        Strength is the distinct memories behind a class: belongs_to members (active) or staged
        discover-pass votes (inactive). Blocked tombstones are excluded.
        """
        cap = limit if limit is not None else DEFAULT_PROMPT_CLASSES
        with self._pool.connection() as conn:
            rows = conn.execute(
                r"""WITH cls AS (
                        SELECT e.canonical_name AS name, e.status,
                               CASE WHEN e.status = 'active'
                                    THEN COALESCE(e.description, '') ELSE '' END AS description,
                               count(DISTINCT el.memory_id) AS members,
                               (SELECT count(DISTINCT vc.memory_id) FROM entity_candidates vc
                                WHERE vc.entity_type = %(t)s
                                  AND vc.normalized_name =
                                      regexp_replace(lower(btrim(e.canonical_name)),
                                                     '\s+', ' ', 'g')) AS voters
                        FROM entities e
                        LEFT JOIN entity_links el
                          ON el.entity_id = e.node_id AND el.link_type = %(bt)s
                        WHERE e.entity_type = %(t)s AND e.status IN ('active', 'inactive')
                        GROUP BY e.canonical_name, e.status, e.description
                    )
                    SELECT name, description FROM cls
                    ORDER BY (status = 'active') DESC, GREATEST(members, voters) DESC, name
                    LIMIT %(cap)s""",
                {"t": ENTITY_CLASS, "bt": BELONGS_TO, "cap": cap},
            ).fetchall()
        return [(r["name"], r["description"]) for r in rows]

    def doc_kinds_for_prompt(self, *, limit: int = 20) -> "list[str]":
        """The 'kinds already in use' block for the doc_kind pass, support-ranked — the
        convergence-by-visibility device (mirrors entity_classes_for_prompt): the model
        reuses an emerging label instead of minting a near-duplicate. Live memories only
        (a retired note shouldn't keep a label alive)."""
        with self._pool.connection() as conn:
            rows = conn.execute(
                """SELECT doc_kind FROM nodes
                   WHERE doc_kind IS NOT NULL AND dead_at IS NULL
                   GROUP BY doc_kind ORDER BY count(*) DESC, doc_kind
                   LIMIT %(limit)s""", {"limit": limit}).fetchall()
        return [r["doc_kind"] for r in rows]

    def doc_kind_rollup(self, *, min_support: "int | None" = None,
                        include_all: bool = False) -> "list[dict]":
        """Aggregate facet view: [{kind, count, established}], support-ranked, live
        memories only. Established = count >= min_support (DEFAULT_DOC_KIND_SUPPORT).
        Gating shapes what surfaces SHOW, never what persists: include_all reveals the
        emergent long tail, which is stored and filterable regardless."""
        floor = DEFAULT_DOC_KIND_SUPPORT if min_support is None else min_support
        with self._pool.connection() as conn:
            rows = conn.execute(
                """SELECT doc_kind AS kind, count(*) AS count
                   FROM nodes WHERE doc_kind IS NOT NULL AND dead_at IS NULL
                   GROUP BY doc_kind ORDER BY count DESC, kind""").fetchall()
        out = [{"kind": r["kind"], "count": r["count"],
                "established": r["count"] >= floor} for r in rows]
        return out if include_all else [r for r in out if r["established"]]

    def active_class_names(self) -> "set[str]":
        """Normalized names of currently-ACTIVE classes — the only types extraction may assign an
        instance to (top-down: discovery owns the taxonomy)."""
        with self._pool.connection() as conn:
            rows = conn.execute(
                "SELECT canonical_name FROM entities "
                "WHERE entity_type = %s AND status = 'active'", (ENTITY_CLASS,)).fetchall()
        return {normalize_entity_name(r["canonical_name"]) for r in rows}

    def get_entity_class(self, name: str) -> "Entity | None":
        """Resolve a category entity by name (normalized), or None if it doesn't exist."""
        return self.get_entity(node_id(ENTITY_SOURCE, entity_external_id(ENTITY_CLASS, name)))

    def instances_of(self, category: str) -> "list[Entity]":
        """The instances that belong to a category — resolved via the instance⇄class links
        (entity_class_links), since category is a link now, not the instance's entity_type."""
        cid = node_id(ENTITY_SOURCE, entity_external_id(ENTITY_CLASS, category))
        with self._pool.connection() as conn:
            rows = conn.execute(
                f"""SELECT {_COLUMNS}, {_ENTITY_COLUMNS}
                    FROM nodes JOIN entities e ON e.node_id = nodes.id
                    WHERE nodes.id IN (
                        SELECT instance_id FROM entity_class_links WHERE class_id = %s)
                    ORDER BY nodes.path""",
                (cid,),
            ).fetchall()
        return [_row_to_entity(r) for r in rows]

    def demote_instance(self, node_id: "UUID") -> bool:
        """Set an active instance to inactive — reversible (status='active' restores it) and
        hidden from the graph/UI, which read status='active'. Logs 'node_demoted'. Returns
        True iff a row changed (no-op on classes, already-inactive, or unknown ids).

        NOTE: when the denoise backfill demotes an instance it ALSO writes an
        entity_instance_blocks row, so fully reactivating one means both flipping status back
        AND deleting its block row — otherwise the promotion sweep keeps excluding the name and
        it never re-accrues mention links."""
        with self._pool.connection() as conn:
            cur = conn.execute(
                """WITH upd AS (
                       UPDATE entities SET status = %(inactive)s
                       WHERE node_id = %(id)s AND entity_type = %(inst)s AND status = %(active)s
                       RETURNING node_id
                   )
                   INSERT INTO ingest_log (event, source, external_id, node_id, path, content_hash)
                   SELECT 'node_demoted', n.source, n.external_id, n.id, n.path::text, n.content_hash
                   FROM upd JOIN nodes n ON n.id = upd.node_id""",
                {"inactive": STATUS_INACTIVE, "id": node_id,
                 "inst": ENTITY_INSTANCE, "active": STATUS_ACTIVE})
            return cur.rowcount > 0

    def active_instances(self) -> "list[dict]":
        """Every active instance with its pinned flag and mention support (strongest first).
        The read the denoise backfill iterates."""
        with self._pool.connection() as conn:
            rows = conn.execute(
                """SELECT e.node_id, e.canonical_name, e.pinned,
                          (SELECT count(DISTINCT el.memory_id) FROM entity_links el
                           WHERE el.entity_id = e.node_id AND el.link_type = %(m)s) AS support
                   FROM entities e
                   WHERE e.entity_type = %(inst)s AND e.status = %(active)s
                   ORDER BY support DESC, e.canonical_name""",
                {"m": MENTIONS, "inst": ENTITY_INSTANCE, "active": STATUS_ACTIVE}).fetchall()
        return [dict(id=r["node_id"], name=r["canonical_name"],
                     pinned=r["pinned"], support=r["support"]) for r in rows]

    def noisy_instances(self) -> "list[dict]":
        """Existing instance entities whose NAME is junk by the deterministic gate
        (`instance_name_issue`) — filenames, paths, task-slugs, pronouns, placeholders.

        The retroactive counterpart of the staging gate: it finds the noise already promoted
        before the gate existed. Pure read; returns one dict per offender with its `reason`
        and `support` (distinct memories that mention it), strongest first, so a caller can
        preview before `prune_noisy_instances` deletes. Classes are never included."""
        out: list[dict] = []
        with self._pool.connection() as conn:
            rows = conn.execute(
                """SELECT e.node_id, e.canonical_name,
                          (SELECT count(DISTINCT el.memory_id) FROM entity_links el
                           WHERE el.entity_id = e.node_id AND el.link_type = %(m)s) AS support
                   FROM entities e WHERE e.entity_type = %(inst)s""",
                {"m": MENTIONS, "inst": ENTITY_INSTANCE},
            ).fetchall()
        for r in rows:
            reason = instance_name_issue(r["canonical_name"])
            if reason is not None:
                out.append({"id": r["node_id"], "name": r["canonical_name"],
                            "support": r["support"], "reason": reason})
        out.sort(key=lambda d: (-d["support"], d["name"]))
        return out

    def prune_noisy_instances(self, ids: "list[UUID] | None" = None) -> int:
        """Delete junk-named instances. With `ids` given, delete exactly those; otherwise
        delete every instance flagged by `noisy_instances`. Each removal goes through the
        logged `delete` path (event 'node_deleted'). The deletion sticks because the staging
        gate now rejects these names, so they can't re-promote. Returns the count removed."""
        if ids is None:
            ids = [d["id"] for d in self.noisy_instances()]
        removed = 0
        for nid in ids:
            removed += self.delete(nid)
        return removed

    # ---- entity candidates (recurrence -> promotion) ----------------------
    def prune_memory_instances(self, memory_id: UUID,
                               keep: "set[tuple[str, str]]") -> int:
        """Delete this memory's instance candidates NOT in `keep` — the removal half of the
        per-memory recrawl reconcile (the upserts in `_stage_entities` are the keep/add half).
        `keep` holds (normalize(entity_type), normalized_name) tuples for the votes the latest
        extraction returned. Empty `keep` deletes every instance candidate for the memory (the
        extractor found no named things now). Returns rows deleted.

        Implemented with two parallel text[] arrays + unnest (not a composite `<> ALL`, which
        trips psycopg's row-type inference); empty arrays make the NOT EXISTS true for every row,
        so the empty-keep case falls out naturally."""
        types = [t for t, _ in keep]
        names = [n for _, n in keep]
        with self._pool.connection() as conn:
            return conn.execute(
                r"""DELETE FROM entity_candidates ec
                    WHERE ec.memory_id = %(m)s AND ec.entity_type <> %(cls)s
                      AND NOT EXISTS (
                        SELECT 1 FROM unnest(%(types)s::text[], %(names)s::text[]) AS k(t, n)
                        WHERE k.t = regexp_replace(lower(btrim(ec.entity_type)), '\s+', ' ', 'g')
                          AND k.n = ec.normalized_name)""",
                {"m": memory_id, "cls": ENTITY_CLASS, "types": types, "names": names}).rowcount

    def memory_instance_summary(self, memory_id: UUID) -> str:
        """The instances this memory currently votes for, as a '- name — types' block for the
        recrawl extract prompt ({existing_instances}). One line per distinct instance name, its
        types comma-joined (the UNTYPED sentinel shown as 'untyped'). '(none)' when the memory
        has no instance votes yet (first-pass enrich) — so the same prompt extracts fresh.
        Aliases (absorbed merged-away names) are appended as ' (aka alias1, alias2)' so the
        model can reuse the canonical name instead of re-minting a previously merged one."""
        with self._pool.connection() as conn:
            rows = conn.execute(
                r"""SELECT max(ec.raw_name) AS name,
                           array_agg(DISTINCT ec.entity_type) AS types,
                           COALESCE(e.aliases, '{}') AS aliases
                    FROM entity_candidates ec
                    LEFT JOIN nodes n
                      ON n.source = 'entity'
                         AND n.external_id =
                             'instance:' || regexp_replace(lower(ec.normalized_name),
                                                           '[^a-z0-9]', '', 'g')
                    LEFT JOIN entities e ON e.node_id = n.id
                    WHERE ec.memory_id = %s AND ec.entity_type <> %s
                    GROUP BY ec.normalized_name, e.aliases
                    ORDER BY max(ec.raw_name)""",
                (memory_id, ENTITY_CLASS),
            ).fetchall()
        if not rows:
            return "(none)"
        def _label(t: str) -> str:
            return "untyped" if t == UNTYPED_CATEGORY else t
        lines = []
        for r in rows:
            name = r["name"]
            types_str = ", ".join(sorted(_label(t) for t in r["types"]))
            aliases = [a for a in (r["aliases"] or []) if a]
            if aliases:
                name = f"{name} (aka {', '.join(sorted(aliases))})"
            lines.append(f"- {name} — {types_str}")
        return "\n".join(lines)

    def stage_entity_candidate(self, memory_id: UUID, entity_type: str, name: str, *,
                               confidence: float | None = None,
                               occurred_at: "datetime | None" = None,
                               role: str | None = None) -> None:
        """Stage one (LLM-proposed) entity mention for later threshold-based promotion.
        Idempotent per (memory, entity_type, normalized name).

        A vote is dropped if its category is a BLOCKED class — the candidate's own name for a
        class vote (entity_type='class'), else the instance's category (entity_type). This is
        what makes human removal stick: a tombstoned category can't re-accrete votes and
        silently re-activate on the next sweep.

        An INSTANCE vote whose name is junk (a filename, path, task-slug, pronoun, placeholder)
        is also dropped here — the single choke point every extractor flows through — so noise
        never stages, never promotes, and can't reappear after a prune. Class names have their
        own guard (is_valid_class_name) upstream and are left to it.

        `occurred_at` carries the model's stated date for an event anchor; `role`
        carries the mention's relationship to the memory. `role` has no DB CHECK
        constraint, so this is the enforcement point: a value outside `ROLES` never reaches
        the column."""
        if entity_type != ENTITY_CLASS and not is_valid_instance_name(name):
            return
        if role not in ROLES:
            role = None
        nn = normalize_entity_name(name)
        blocked_key = nn if entity_type == ENTITY_CLASS else normalize_entity_name(entity_type)
        with self._pool.connection() as conn:
            conn.execute(
                """INSERT INTO entity_candidates
                       (memory_id, entity_type, normalized_name, raw_name, confidence,
                        occurred_at, role)
                   SELECT %(m)s, %(t)s, %(nn)s, %(rn)s, %(c)s, %(oa)s, %(role)s
                   WHERE NOT EXISTS (
                       SELECT 1 FROM entities e JOIN nodes n ON n.id = e.node_id
                       WHERE n.source = 'entity'
                         AND n.external_id = 'class:' || %(bk)s
                         AND e.status = 'blocked')
                   ON CONFLICT (memory_id, entity_type, normalized_name) DO NOTHING""",
                {"m": memory_id, "t": entity_type, "nn": nn, "rn": name, "c": confidence,
                 "bk": blocked_key, "oa": occurred_at, "role": role},
            )

    def promotable_entities(self, *, threshold: int = 3) -> "list[dict]":
        """Candidate groups ready to promote on this sweep — either:
          - recurred across >= threshold distinct memories (first-time creation), OR
          - whose instance entity ALREADY exists (link any new mentions immediately, no
            threshold — once a pattern is established every later mention should link).
        Each group is one (category, name) candidate set; `entity_exists` is true when EITHER
        (a) the instance's CANONICAL (name-only) external_id already has a node, OR (b) an
        ACTIVE instance owns this name as an ALIAS (without the alias leg, a single post-merge
        mention of a folded-away name waits out the full recurrence threshold instead of linking
        immediately — and once it clears threshold on its own, it promotes as a brand-new
        fragment instance instead of folding onto the merge survivor via `promote_entity`'s
        `alias_owner` lookup). Once any variant/category/alias has established the instance,
        every later mention of that name links immediately, and all of a real entity's category
        votes converge on the single node.

        Event-kind categories promote at ONE distinct memory — a specific meeting/decision
        happens once by nature, so recurrence would filter every real event out.

        Junk-named groups are filtered out (same gate as staging) so a pre-gate candidate row
        left over from before the gate existed can never resurrect a noisy instance.

        A group whose best confidence is in the rubric's bottom band (TRUSTED_CONFIDENCE_CUTOFF)
        can never CREATE an entity no matter how often it recurs — the model has flagged the name
        as implementation-only or one-off. The `entity_exists` leg is deliberately outside that
        gate: once an instance is established, every later mention links immediately (see above),
        and a single low-confidence mention should not sever a real entity from a memory. Groups
        with no confidence at all (legacy rows, class votes) coalesce to trusted, so this gate
        only ever acts on an explicit low score.

        First-time creation is age-decayed for memories matched by a policy decay rule
        (kb_core.aging): recurrence support is sum(age_factor) over the group's distinct
        memories — notes always vote 1.0, dormant matched threads barely vote, fresh
        matched mentions count ~1.0 each. TWO legs are exempt and stay on raw counts:
        entity_exists (established patterns must keep linking regardless of
        age) and event-kind's >= 1 bar (a factor < 1.0 sum could never reach it, which
        would silently kill all event promotion). The (memory_id, entity_type,
        normalized_name) UNIQUE constraint guarantees each memory appears once per group,
        so plain sum() IS the decayed distinct count."""
        age_mem = age_sql("mem.event_at", "mem.tags", "mem.path", "mem.source",
                          self._policy.decay)
        params_age = age_params(self._policy.decay, self.age_decay_enabled())
        with self._pool.connection() as conn:
            rows = conn.execute(
                rf"""SELECT ec.entity_type, ec.normalized_name,
                          max(ec.raw_name) AS raw_name,
                          count(DISTINCT ec.memory_id) AS memory_count,
                          bool_or(n.id IS NOT NULL OR EXISTS (
                              SELECT 1 FROM entities alias_e
                              WHERE alias_e.entity_type = %(inst)s AND alias_e.status = 'active'
                                AND EXISTS (
                                    SELECT 1 FROM unnest(alias_e.aliases) al
                                    WHERE regexp_replace(lower(al), '[^a-z0-9]+', '', 'g')
                                        = regexp_replace(ec.normalized_name, '[^a-z0-9]', '', 'g'))
                          )) AS entity_exists
                   FROM entity_candidates ec
                   LEFT JOIN nodes mem ON mem.id = ec.memory_id
                   LEFT JOIN nodes n
                     ON n.source = 'entity'
                        AND n.external_id =
                            'instance:' || regexp_replace(ec.normalized_name, '[^a-z0-9]', '', 'g')
                   LEFT JOIN nodes cn
                     ON cn.source = 'entity'
                        AND cn.external_id =
                            'class:' || regexp_replace(lower(btrim(ec.entity_type)), '\s+', ' ', 'g')
                   LEFT JOIN entities cls ON cls.node_id = cn.id
                   WHERE ec.promoted = false
                     AND ec.entity_type <> 'class'   -- class candidates activate, not promote
                     AND NOT EXISTS (SELECT 1 FROM entity_instance_blocks b
                                     WHERE b.normalized_name = ec.normalized_name)
                   GROUP BY ec.entity_type, ec.normalized_name, cls.kind
                   HAVING bool_or(n.id IS NOT NULL OR EXISTS (
                              SELECT 1 FROM entities alias_e
                              WHERE alias_e.entity_type = %(inst)s AND alias_e.status = 'active'
                                AND EXISTS (
                                    SELECT 1 FROM unnest(alias_e.aliases) al
                                    WHERE regexp_replace(lower(al), '[^a-z0-9]+', '', 'g')
                                        = regexp_replace(ec.normalized_name, '[^a-z0-9]', '', 'g'))
                          ))
                       OR ((CASE WHEN cls.kind = 'event'
                                 THEN count(DISTINCT ec.memory_id) >= 1
                                 ELSE sum({age_mem}) >= %(n)s END)
                           AND coalesce(max(ec.confidence), 1.0) > %(trust)s)
                   ORDER BY memory_count DESC""",
                {"n": threshold, "inst": ENTITY_INSTANCE,
                 "trust": TRUSTED_CONFIDENCE_CUTOFF, **params_age},
            ).fetchall()
        return [r for r in rows if is_valid_instance_name(r["raw_name"])]

    def alias_owner(self, name: str) -> "UUID | None":
        """The active instance whose aliases contain `name` (canonical-key comparison), if any.
        This is what keeps a merged-away name folded: post-merge, re-extraction of the old
        name must land on the survivor instead of re-minting the deleted node (e.g. 'Avery'
        merged into 'Avery Quinn').

        Two active instances both claiming the same alias key should not happen in normal
        operation, but isn't structurally prevented (two independent merges could each fold a
        same-named source onto different survivors). `ORDER BY node_id` makes the pick
        deterministic (stable across calls/replicas) rather than whatever Postgres's LIMIT 1
        happens to return first, so a pathological double-claim fails predictably instead of
        flapping which survivor new mentions land on from one call to the next."""
        ck = canonical_key(name)
        if not ck:
            return None
        with self._pool.connection() as conn:
            row = conn.execute(
                """SELECT node_id FROM entities
                   WHERE entity_type = 'instance' AND status = 'active'
                     AND EXISTS (SELECT 1 FROM unnest(aliases) al
                                 WHERE regexp_replace(lower(al), '[^a-z0-9]+', '', 'g') = %(ck)s)
                   ORDER BY node_id
                   LIMIT 1""", {"ck": ck}).fetchone()
        return row["node_id"] if row else None

    # Shared predicate for the alias-link repair: every PROMOTED candidate whose key matches an
    # ACTIVE instance's alias but NOT its own canonical_name (that path already links correctly
    # through the normal promote_entity flow) — the exact population an earlier alias-folding
    # bug could have stripped via reconcile_instance_links' pre-fix, canonical-name-only
    # mentions sweep. DISTINCT ON collapses multiple candidate rows (different entity_type, same
    # folded name, same memory) to one, preferring the highest-confidence row for the
    # weight/role carried onto the repaired link.
    _ALIAS_BACKFILL_CANDIDATES = r"""
        SELECT DISTINCT ON (c.memory_id, e.node_id)
               c.memory_id, e.node_id AS entity_id, c.confidence, c.role
        FROM entity_candidates c
        JOIN entities e
          ON e.entity_type = %(inst)s AND e.status = 'active'
             AND EXISTS (SELECT 1 FROM unnest(e.aliases) al
                         WHERE regexp_replace(lower(al), '[^a-z0-9]+', '', 'g')
                             = regexp_replace(c.normalized_name, '[^a-z0-9]', '', 'g'))
             AND regexp_replace(c.normalized_name, '[^a-z0-9]', '', 'g')
                 <> regexp_replace(lower(btrim(e.canonical_name)), '[^a-z0-9]', '', 'g')
        WHERE c.promoted = true
          AND c.entity_type <> %(cls)s
        ORDER BY c.memory_id, e.node_id, c.confidence DESC NULLS LAST"""

    def count_alias_link_backfill(self) -> int:
        """Read-only preview of `backfill_alias_links` — how many mention links WOULD be
        restored, with zero writes (the same purity contract `plan_hygiene` needs)."""
        with self._pool.connection() as conn:
            row = conn.execute(
                f"""WITH candidates AS ({self._ALIAS_BACKFILL_CANDIDATES})
                    SELECT count(*) AS n FROM candidates cand
                    WHERE NOT EXISTS (
                        SELECT 1 FROM entity_links el
                        WHERE el.memory_id = cand.memory_id AND el.entity_id = cand.entity_id
                          AND el.link_type = %(mentions)s)""",
                {"inst": ENTITY_INSTANCE, "cls": ENTITY_CLASS, "mentions": MENTIONS},
            ).fetchone()
        return row["n"]

    def backfill_alias_links(self) -> int:
        """One-time repair for entity_links mention rows already stripped by an earlier
        alias-folding bug before this fix landed — and for the related bug it shares a root
        cause with (merge_instances' re-pointed
        links, silently degraded by the live daemon's reconcile sweep). Promoted candidates are a
        durable per-memory ledger, so lost links are reconstructible from them: for every PROMOTED
        candidate whose canonical key matches an ACTIVE instance's ALIAS (not its canonical_name —
        that path never lost its link), re-insert the missing memory->instance mention row (weight
        from the candidate's confidence, role carried across). Idempotent (`ON CONFLICT DO
        NOTHING`). Returns the count inserted."""
        with self._pool.connection() as conn:
            cur = conn.execute(
                f"""WITH candidates AS ({self._ALIAS_BACKFILL_CANDIDATES})
                    INSERT INTO entity_links (memory_id, entity_id, link_type, weight, role)
                    SELECT cand.memory_id, cand.entity_id, %(mentions)s, cand.confidence, cand.role
                    FROM candidates cand
                    WHERE NOT EXISTS (
                        SELECT 1 FROM entity_links el
                        WHERE el.memory_id = cand.memory_id AND el.entity_id = cand.entity_id
                          AND el.link_type = %(mentions)s)
                    ON CONFLICT (memory_id, entity_id, link_type) DO NOTHING""",
                {"inst": ENTITY_INSTANCE, "cls": ENTITY_CLASS, "mentions": MENTIONS},
            )
            return cur.rowcount

    def promote_entity(self, entity_type: str, name: str, *,
                       description: str | None = None,
                       relation_type: str = MENTIONS) -> Entity:
        """Promote a recurring candidate (category `entity_type`, `name`) into the canonical
        entity graph.

        The instance is ONE node identified by name alone (instance_external_id), placed flat
        under entities.instances — NOT nested under the category. The category becomes an is_a
        LINK to the category's class node (entity_class_links). So promoting the same name under
        several categories converges on a single node that gains one class link per category.
        Backfills weighted memory --mentions--> instance links (with role) from this group's
        staged memories and marks them promoted; also backfills the instance's `occurred_at`
        from the group's candidates when unset. If `name` folds onto an existing instance's
        aliases (`alias_owner`), that survivor is reused instead of re-minting a node. Idempotent.
        """
        normalized = normalize_entity_name(name)
        owner = self.alias_owner(name)          # BEFORE opening the transaction
        if entity_type == UNTYPED_CATEGORY:
            # No active type fit yet: materialize the instance node + backfill mentions, but
            # create NO is_a class link. A later pass re-stages it under a real type once one
            # activates (that pass re-derives the aggregate).
            with self._pool.connection() as conn, conn.transaction():
                if owner is not None:
                    eid = owner
                    conn.execute(
                        "UPDATE entities SET embed_dirty = true WHERE node_id = %s", (owner,))
                else:
                    node = self._upsert_instance_node(conn, name, description)
                    eid = node.id
                conn.execute(
                    """INSERT INTO entity_links (memory_id, entity_id, link_type, weight, role)
                       SELECT memory_id, %(eid)s, %(lt)s, confidence, role
                       FROM entity_candidates
                       WHERE entity_type = %(t)s AND normalized_name = %(nn)s AND promoted = false
                       ON CONFLICT (memory_id, entity_id, link_type)
                       DO UPDATE SET weight = EXCLUDED.weight,
                                     role = COALESCE(EXCLUDED.role, entity_links.role),
                                     updated_at = now()""",
                    {"eid": eid, "lt": relation_type, "t": entity_type, "nn": normalized})
                conn.execute(
                    """UPDATE entities SET occurred_at = COALESCE(entities.occurred_at, sub.oa)
                       FROM (SELECT max(occurred_at) AS oa FROM entity_candidates
                             WHERE entity_type = %(t)s AND normalized_name = %(nn)s) sub
                       WHERE entities.node_id = %(eid)s AND sub.oa IS NOT NULL""",
                    {"eid": eid, "t": entity_type, "nn": normalized})
                conn.execute(
                    "UPDATE entity_candidates SET promoted = true "
                    "WHERE entity_type = %(t)s AND normalized_name = %(nn)s AND promoted = false",
                    {"t": entity_type, "nn": normalized})
            if owner is not None:
                return self.get_entity(eid)
            # node.title is the WRITTEN name -- may be the established casing, not `name`.
            return Entity(node=node, entity_type=ENTITY_INSTANCE, canonical_name=node.title,
                          aliases=[], description=description, status=STATUS_ACTIVE)
        # If a human has blocked this category, don't resurrect it via the back door: consume
        # the staged candidates (so the sweep stops retrying them) and link no class.
        # Ensure the category class exists (outside the txn — its mkdir -p shares the `entities`
        # ancestor and would deadlock against the instance's txn). None == blocked tombstone.
        class_id = self._ensure_class_id(entity_type)
        if class_id is None:
            self._consume_candidates(entity_type, normalized)
            return self.get_entity_class(entity_type)
        with self._pool.connection() as conn, conn.transaction():
            if owner is not None:
                eid = owner
                conn.execute(
                    "UPDATE entities SET embed_dirty = true WHERE node_id = %s", (owner,))
            else:
                node = self._upsert_instance_node(conn, name, description)
                eid = node.id
            self._link_instance_class(conn, eid, class_id)  # instance --is_a--> class
            # backfill memory --(relation_type)--> instance into the entity_links overlay
            conn.execute(
                """INSERT INTO entity_links (memory_id, entity_id, link_type, weight, role)
                   SELECT memory_id, %(eid)s, %(lt)s, confidence, role
                   FROM entity_candidates
                   WHERE entity_type = %(t)s AND normalized_name = %(nn)s
                     AND promoted = false
                   ON CONFLICT (memory_id, entity_id, link_type)
                   DO UPDATE SET weight = EXCLUDED.weight,
                                 role = COALESCE(EXCLUDED.role, entity_links.role),
                                 updated_at = now()""",
                {"eid": eid, "lt": relation_type, "t": entity_type, "nn": normalized},
            )
            conn.execute(
                """UPDATE entities SET occurred_at = COALESCE(entities.occurred_at, sub.oa)
                   FROM (SELECT max(occurred_at) AS oa FROM entity_candidates
                         WHERE entity_type = %(t)s AND normalized_name = %(nn)s) sub
                   WHERE entities.node_id = %(eid)s AND sub.oa IS NOT NULL""",
                {"eid": eid, "t": entity_type, "nn": normalized})
            conn.execute(
                """UPDATE entity_candidates SET promoted = true
                   WHERE entity_type = %(t)s AND normalized_name = %(nn)s
                     AND promoted = false""",
                {"t": entity_type, "nn": normalized},
            )
        if owner is not None:
            return self.get_entity(eid)
        # node.title is the WRITTEN name -- may be the established casing, not `name`.
        return Entity(node=node, entity_type=ENTITY_INSTANCE, canonical_name=node.title,
                      aliases=[], description=description, status=STATUS_ACTIVE)

    def derive_instance_kinds(self) -> int:
        """Recompute each instance's structural kind as the modal kind of its classes
        (ties broken alphabetically for determinism; instances with no kinded class keep
        NULL). Cheap full recompute — the instance population is small; runs each
        promotion sweep so kind tracks class membership as it evolves."""
        with self._pool.connection() as conn:
            return conn.execute(
                """WITH modal AS (
                       SELECT l.instance_id,
                              mode() WITHIN GROUP (ORDER BY c.kind) AS kind
                       FROM entity_class_links l
                       JOIN entities c ON c.node_id = l.class_id AND c.kind IS NOT NULL
                       GROUP BY l.instance_id)
                   UPDATE entities e SET kind = modal.kind
                   FROM modal
                   WHERE e.node_id = modal.instance_id
                     AND e.kind IS DISTINCT FROM modal.kind""").rowcount

    def _consume_candidates(self, entity_type: str, normalized_name: str) -> None:
        """Mark a candidate group promoted without materializing anything — used to clear
        votes for a blocked category so the promotion sweep stops retrying them."""
        with self._pool.connection() as conn:
            conn.execute(
                """UPDATE entity_candidates SET promoted = true
                   WHERE entity_type = %(t)s AND normalized_name = %(nn)s
                     AND promoted = false""",
                {"t": entity_type, "nn": normalized_name},
            )

    def block_instance_promotion(self, normalized_name: str, reason: str,
                                 *, origin: str = "judge") -> None:
        """Record a name the promotion sweep must never promote (a judged code symbol, or a
        human/backfill block). Idempotent; normalizes its key to match promotable_entities'
        grouping (`entity_candidates.normalized_name`).

        The key is `normalize_entity_name` (matching the promotion GROUP key), which is coarser
        than instance identity's `canonical_key` (it keeps punctuation). So a punctuation/spacing
        variant of a blocked name is a different key and isn't excluded — it is simply re-judged
        by the gate next sweep rather than silently promoted, which is the intended safety net."""
        key = normalize_entity_name(normalized_name)
        with self._pool.connection() as conn:
            conn.execute(
                """INSERT INTO entity_instance_blocks (normalized_name, reason, origin)
                   VALUES (%(n)s, %(r)s, %(o)s)
                   ON CONFLICT (normalized_name) DO NOTHING""",
                {"n": key, "r": reason, "o": origin})

    # ---- class activation (recurrence -> active) --------------------------
    def activatable_classes(self, *, threshold: int = 3) -> "list[dict]":
        """Class candidate groups ready to (re)settle on this sweep — either:
          - recurred across >= threshold distinct memories while still INACTIVE (first-time
            activation), OR
          - whose class is ALREADY active with new staged votes (link them immediately, no
            threshold — once a category is established every later vote should attach).
        Class candidates are staged with entity_type='class'; the JOIN resolves the class
        node by its deterministic external_id ('class:<normalized_name>'). `already_active`
        says which path triggered it. Blocked (human-removed) classes are excluded so a
        tombstone never reactivates. Mirrors promotable_entities for instances."""
        with self._pool.connection() as conn:
            return conn.execute(
                """SELECT ec.normalized_name,
                          max(ec.raw_name) AS raw_name,
                          count(DISTINCT ec.memory_id) AS memory_count,
                          bool_or(e.status = 'active') AS already_active
                   FROM entity_candidates ec
                   JOIN nodes n
                     ON n.source = 'entity'
                        AND n.external_id = 'class:' || ec.normalized_name
                   JOIN entities e ON e.node_id = n.id
                   WHERE ec.promoted = false AND ec.entity_type = 'class'
                     AND e.status <> 'blocked'
                   GROUP BY ec.normalized_name
                   HAVING count(DISTINCT ec.memory_id) >= %(n)s
                       OR bool_or(e.status = 'active')
                   ORDER BY memory_count DESC""",
                {"n": threshold},
            ).fetchall()

    def activate_class(self, name: str) -> "Entity | None":
        """Flip a recurring class to active and backfill memory --belongs_to--> class edges
        from every staged class candidate, then mark those candidates promoted. A blocked
        class is left untouched (the tombstone wins). Idempotent: re-running re-asserts the
        active status and the edges without creating duplicates."""
        normalized = normalize_entity_name(name)
        cid = node_id(ENTITY_SOURCE, entity_external_id(ENTITY_CLASS, name))
        with self._pool.connection() as conn, conn.transaction():
            conn.execute(
                "UPDATE entities SET status = 'active' "
                "WHERE node_id = %(id)s AND status <> 'blocked'", {"id": cid},
            )
            conn.execute(
                """INSERT INTO entity_links (memory_id, entity_id, link_type, weight)
                   SELECT memory_id, %(cid)s, %(lt)s, confidence
                   FROM entity_candidates
                   WHERE entity_type = 'class' AND normalized_name = %(nn)s
                     AND promoted = false
                   ON CONFLICT (memory_id, entity_id, link_type)
                   DO UPDATE SET weight = EXCLUDED.weight, updated_at = now()""",
                {"cid": cid, "lt": BELONGS_TO, "nn": normalized},
            )
            conn.execute(
                """UPDATE entity_candidates SET promoted = true
                   WHERE entity_type = 'class' AND normalized_name = %(nn)s
                     AND promoted = false""",
                {"nn": normalized},
            )
        return self.get_entity(cid)

    # ---- class curation (human-in-the-loop taxonomy management) -----------
    def list_entity_classes(self, *, include_blocked: bool = False) -> "list[dict]":
        """Every category with its lifecycle status and reach — what the dashboard's class
        manager renders. `members` is the distinct memories that belong_to the class (the
        same signal the active cap ranks on); `instances` is the sub-entities filed under it;
        `voters` is the distinct memories whose enrichment staged a vote for it (entity_candidates)
        — the set that would need re-enriching if the class were pruned. Blocked tombstones are
        hidden unless include_blocked. Strongest first."""
        with self._pool.connection() as conn:
            return conn.execute(
                r"""SELECT e.canonical_name AS name, e.status, e.aliases, e.kind,
                          e.description, e.description_locked, e.pinned,
                          count(DISTINCT el.memory_id) AS members,
                          count(DISTINCT ecl.instance_id) AS instances,
                          (SELECT count(DISTINCT vc.memory_id) FROM entity_candidates vc
                           WHERE vc.entity_type = %(cls)s
                             AND vc.normalized_name =
                                 regexp_replace(lower(btrim(e.canonical_name)), '\s+', ' ', 'g')
                          ) AS voters
                   FROM entities e
                   JOIN nodes cn ON cn.id = e.node_id
                   LEFT JOIN entity_links el
                     ON el.entity_id = e.node_id AND el.link_type = %(bt)s
                   LEFT JOIN entity_class_links ecl ON ecl.class_id = e.node_id
                   WHERE e.entity_type = %(cls)s
                     AND (%(inc)s OR e.status <> 'blocked')
                   GROUP BY e.canonical_name, e.status, e.aliases, e.kind, e.description,
                            e.description_locked, e.pinned
                   ORDER BY members DESC, e.canonical_name""",
                {"bt": BELONGS_TO, "cls": ENTITY_CLASS, "inc": include_blocked},
            ).fetchall()

    def class_voters(self, name: str) -> "list[Node]":
        """The memories whose enrichment produced this class — its 'voters'. Union of the
        discover-pass votes staged in entity_candidates (entity_type='class') and the memories
        classified under it (belongs_to links). This is exactly the set that must be re-enriched
        if the class is pruned, so the frontend can show 'these N memories feed this category'
        before you remove it. Strongest first."""
        normalized = normalize_entity_name(name)
        cid = node_id(ENTITY_SOURCE, entity_external_id(ENTITY_CLASS, name))
        with self._pool.connection() as conn:
            rows = conn.execute(
                f"""SELECT {_COLUMNS} FROM nodes WHERE id IN (
                        SELECT memory_id FROM entity_candidates
                        WHERE entity_type = %(cls)s AND normalized_name = %(nn)s
                        UNION
                        SELECT memory_id FROM entity_links WHERE entity_id = %(cid)s
                    ) ORDER BY importance DESC, path""",
                {"cls": ENTITY_CLASS, "nn": normalized, "cid": cid},
            ).fetchall()
        return [_row_to_node(r) for r in rows]

    def memories_for_entity(self, entity_id: UUID, entity_type: str,
                            name: str) -> "list[Node]":
        """The memories that reference an entity (class or instance) — the set to requeue when it
        changes. Fully-indexed union of (a) materialized entity_links pointing at it (the
        `entity_links_entity` index) and (b) the memory's PENDING candidates for this (type, name)
        (indexed by `entity_candidates_group` on `(entity_type, normalized_name)`). Generalizes `class_voters` to any entity."""
        normalized = normalize_entity_name(name)
        with self._pool.connection() as conn:
            rows = conn.execute(
                f"""SELECT {_COLUMNS} FROM nodes WHERE id IN (
                        SELECT memory_id FROM entity_links WHERE entity_id = %(eid)s
                        UNION
                        SELECT memory_id FROM entity_candidates
                        WHERE entity_type = %(t)s AND normalized_name = %(nn)s
                          AND promoted = false
                    ) ORDER BY importance DESC, path""",
                {"eid": entity_id, "t": entity_type, "nn": normalized},
            ).fetchall()
        return [_row_to_node(r) for r in rows]

    def set_class_description(self, name: str, description: str) -> "Entity | None":
        """Human-curate a category's description and LOCK it (description_locked=true): once set
        here, the discover pass's `upsert_entity_class` never overwrites it. No-op returning None
        if the class doesn't exist."""
        cid = node_id(ENTITY_SOURCE, entity_external_id(ENTITY_CLASS, name))
        with self._pool.connection() as conn:
            row = conn.execute(
                "UPDATE entities SET description = %(d)s, description_locked = true "
                "WHERE node_id = %(id)s RETURNING node_id",
                {"d": description, "id": cid}).fetchone()
        return self.get_entity(cid) if row else None

    def set_class_kind(self, name: str, kind: "str | None") -> "Entity | None":
        """Set a class's structural kind directly (hygiene/curation path). Unlike
        `upsert_entity_class`, this IS the curated write: it overwrites whatever kind (seeded
        or not) the class currently has. `kind=None` clears it. Raises ValueError for anything
        outside the closed `KINDS` vocabulary — a caller typo should fail loudly, not silently
        write junk past the column's CHECK constraint. No-op returning None if the class
        doesn't exist."""
        if kind is not None and kind not in KINDS:
            raise ValueError(f"unknown kind: {kind!r}")
        cid = node_id(ENTITY_SOURCE, entity_external_id(ENTITY_CLASS, name))
        with self._pool.connection() as conn:
            row = conn.execute(
                "UPDATE entities SET kind = %(k)s WHERE node_id = %(id)s RETURNING node_id",
                {"k": kind, "id": cid}).fetchone()
        return self.get_entity(cid) if row else None

    def set_class_pinned(self, name: str, pinned: bool) -> "Entity | None":
        """Pin or unpin a category. A pinned class is protected from automatic deactivation by
        reconcile_classes (manual curation outranks the vote-based sweep), mirroring
        description_locked and the blocked tombstone. No-op returning None if it doesn't exist."""
        cid = node_id(ENTITY_SOURCE, entity_external_id(ENTITY_CLASS, name))
        with self._pool.connection() as conn:
            row = conn.execute(
                "UPDATE entities SET pinned = %(p)s WHERE node_id = %(id)s RETURNING node_id",
                {"p": pinned, "id": cid}).fetchone()
        return self.get_entity(cid) if row else None

    def block_class(self, name: str, *, purge_instances: bool = True,
                    requeue_voters: bool = True) -> "Entity | None":
        """Tombstone a category so it's gone for good: status='blocked' (withheld from the
        offered list and the default dashboard view), its belongs_to links and pending
        candidates dropped, and — unless purge_instances=False — its instance MEMBERSHIPS
        (entity_class_links) removed. Instance nodes themselves are NOT deleted: an instance may
        belong to other classes or still be mentioned, so it survives (uncategorized) and the
        zero-mention GC (reconcile_instances) collects it only if nothing references it anymore.
        The tombstone is what makes removal stick: `stage_entity_candidate` refuses to re-vote
        a blocked name and `activate_class` won't revive it, so it can't silently return.

        With requeue_voters (default), the memories that fed this class (`class_voters`) are
        flagged for re-enrichment, so they re-derive a classification WITHOUT it — the blocked
        name is skipped at staging, so they land on valid categories instead. Returns the
        blocked class, or None if no such class exists. To fold it into another category
        instead of deleting, use `merge_classes`."""
        cls = self.get_entity_class(name)
        if cls is None:
            return None
        normalized = normalize_entity_name(name)
        with self._pool.connection() as conn, conn.transaction():
            if requeue_voters:
                # Read voters from candidates + belongs_to BEFORE we drop either, and queue
                # them for a fresh enrichment pass.
                conn.execute(
                    """UPDATE nodes SET enrichment_status = 'pending', enriched_hash = NULL
                       WHERE type = 'note' AND id IN (
                           SELECT memory_id FROM entity_candidates
                           WHERE entity_type = %(cls)s AND normalized_name = %(nn)s
                           UNION
                           SELECT memory_id FROM entity_links WHERE entity_id = %(id)s)""",
                    {"cls": ENTITY_CLASS, "nn": normalized, "id": cls.node.id})
            conn.execute("UPDATE entities SET status = 'blocked' WHERE node_id = %(id)s",
                         {"id": cls.node.id})
            conn.execute("DELETE FROM entity_links WHERE entity_id = %(id)s",
                         {"id": cls.node.id})
            conn.execute(
                """DELETE FROM entity_candidates
                   WHERE promoted = false
                     AND ((entity_type = %(cls)s AND normalized_name = %(nn)s)
                          OR entity_type IN (%(name)s, %(nn)s))""",
                {"cls": ENTITY_CLASS, "nn": normalized, "name": cls.canonical_name},
            )
            if purge_instances:
                conn.execute("DELETE FROM entity_class_links WHERE class_id = %(id)s",
                             {"id": cls.node.id})
        if purge_instances:
            self.reconcile_instances()      # GC instances left with no class and no mentions
        return self.get_entity(cls.node.id)

    def unblock_class(self, name: str) -> "Entity | None":
        """Lift a tombstone: a blocked class returns as INACTIVE, so it must re-earn its place
        by recurring past the threshold again rather than springing back active. No-op on a
        class that isn't blocked. Returns the class, or None if it doesn't exist."""
        cls = self.get_entity_class(name)
        if cls is None:
            return None
        with self._pool.connection() as conn:
            conn.execute(
                "UPDATE entities SET status = 'inactive' "
                "WHERE node_id = %(id)s AND status = 'blocked'", {"id": cls.node.id})
        return self.get_entity(cls.node.id)

    def deactivate_class(self, name: str) -> "Entity | None":
        """Demote an active class back to inactive (status active→inactive). PRESERVES its votes
        (entity_candidates) and belongs_to links, so it keeps its provenance and can re-activate
        later if it regains dominance. No-op on an inactive or blocked class. Returns the class,
        or None if it doesn't exist."""
        cls = self.get_entity_class(name)
        if cls is None:
            return None
        with self._pool.connection() as conn:
            conn.execute(
                "UPDATE entities SET status = 'inactive' "
                "WHERE node_id = %(id)s AND status = 'active'", {"id": cls.node.id})
        return self.get_entity(cls.node.id)

    def reconcile_instance_links(self) -> int:
        """Re-derive instance overlay links from the current candidate ledger — the RETRACTION
        counterpart to promotion's additive linking. Idempotent; two sweeps:
          - is_a (entity_class_links): drop a link whose class is blocked, or for which no
            candidate (any memory) still types this instance under that class. Deactivation alone
            does NOT retract is_a — the class must be blocked or lose candidate support.
          - mentions (entity_links): drop a memory->instance mention no candidate of that memory
            still backs.
        Instance identity matches on canonical_key (alnum-only) of EITHER the instance's
        canonical_name OR any of its aliases[] — a type matches a class on the normalized name.
        The alias leg is what keeps promote_entity's alias fold (and merge_instances' re-pointed
        links) from being stripped right back off by this same sweep: a candidate staged under a
        name that has since been folded onto a survivor (`alias_owner`) only ever keys to the OLD
        name, never the survivor's canonical_name, so without matching aliases too, every
        alias-folded mention/is_a link would be retracted the very next tick it runs. Run in the
        promotion sweep AFTER promotion and BEFORE reconcile_instances (so a mention retracted
        here lets the GC collect a now-orphaned instance). Returns links retracted."""
        with self._pool.connection() as conn, conn.transaction():
            isa = conn.execute(
                r"""DELETE FROM entity_class_links ecl
                    USING entities einst, entities ecls
                    WHERE einst.node_id = ecl.instance_id AND einst.entity_type = %(inst)s
                      AND ecls.node_id = ecl.class_id AND ecls.entity_type = %(cls)s
                      AND ecl.origin <> 'human'
                      AND (
                        ecls.status = 'blocked'
                        OR NOT EXISTS (
                          SELECT 1 FROM entity_candidates c
                          WHERE regexp_replace(c.normalized_name, '[^a-z0-9]', '', 'g') = ANY (
                                  SELECT regexp_replace(lower(nm), '[^a-z0-9]+', '', 'g')
                                  FROM unnest(einst.aliases || ARRAY[einst.canonical_name]) AS nm)
                            AND regexp_replace(lower(btrim(c.entity_type)), '\s+', ' ', 'g')
                                = regexp_replace(lower(btrim(ecls.canonical_name)), '\s+', ' ', 'g')
                        ))""",
                {"inst": ENTITY_INSTANCE, "cls": ENTITY_CLASS},
            ).rowcount
            mentions = conn.execute(
                r"""DELETE FROM entity_links el
                    USING entities einst
                    WHERE el.entity_id = einst.node_id AND einst.entity_type = %(inst)s
                      AND el.link_type = %(mentions)s
                      AND NOT EXISTS (
                        SELECT 1 FROM entity_candidates c
                        WHERE c.memory_id = el.memory_id
                          AND c.entity_type <> %(cls)s
                          AND regexp_replace(c.normalized_name, '[^a-z0-9]', '', 'g') = ANY (
                                SELECT regexp_replace(lower(nm), '[^a-z0-9]+', '', 'g')
                                FROM unnest(einst.aliases || ARRAY[einst.canonical_name]) AS nm))""",
                {"inst": ENTITY_INSTANCE, "cls": ENTITY_CLASS, "mentions": MENTIONS},
            ).rowcount
        return isa + mentions

    def reconcile_instances(self) -> int:
        """Garbage-collect orphaned instance entities — the deletion/exclusion counterpart of the
        instance-promotion sweep.

        An instance (a sub-entity: person, org, topic — every entity whose entity_type is a real
        category, i.e. NOT the reserved 'class') exists only to anchor the memories that mention it.
        Deleting notes (sync prune / `delete`) or retracting their influence (exclusion, noise gate)
        drops the instance's `entity_links` and candidates via cascade/`_retract_influence`, but the
        instance NODE was never collected — leaving a zombie entity that nothing supports yet still
        lists and ranks. This pass removes exactly those: instances with NO remaining link and NO
        unpromoted candidate that could re-link them.

        The bar is ZERO support, deliberately not the promotion threshold. A single surviving memory
        still *mentions* the entity, so demoting at the threshold would sever a live mention and
        leave that memory pointing at nothing. The invariant is simply: an instance exists iff >= 1
        memory supports it. The unpromoted-candidate guard keeps this safe to run in any order — an
        instance with a freshly staged mention awaiting the next promotion sweep is not an orphan.
        The guard matches on canonical_key of EITHER the instance's canonical_name OR any of its
        aliases[] (same alias-folding fix as `reconcile_instance_links`) — a pending mention of
        a folded-away old name must spare the survivor just as one under its current name would.
        Blocked instances are left untouched (mirrors reconcile_classes — a tombstone is not GC).

        Each removal cascades its `entities`/`entity_links` rows and is logged 'node_deleted' (a GC
        is observable, like every other deletion). Idempotent. Returns the count removed.
        """
        with self._pool.connection() as conn:
            cur = conn.execute(
                r"""WITH del AS (
                       DELETE FROM nodes n
                       USING entities e
                       WHERE e.node_id = n.id
                         AND e.entity_type = %(inst)s             -- instances only, never classes
                         AND e.status <> 'blocked'                -- a tombstone is not garbage
                         AND NOT EXISTS (
                             SELECT 1 FROM entity_links el WHERE el.entity_id = n.id)
                         AND NOT EXISTS (
                             SELECT 1 FROM entity_candidates ec
                             WHERE ec.promoted = false
                               AND regexp_replace(ec.normalized_name, '[^a-z0-9]', '', 'g') = ANY (
                                     SELECT regexp_replace(lower(nm), '[^a-z0-9]+', '', 'g')
                                     FROM unnest(e.aliases || ARRAY[e.canonical_name]) AS nm))
                       RETURNING n.id, n.source, n.external_id,
                                 n.path::text AS path, n.content_hash
                   )
                   INSERT INTO ingest_log
                       (event, source, external_id, node_id, path, content_hash)
                   SELECT 'node_deleted', source, external_id, id, path, content_hash FROM del""",
                {"inst": ENTITY_INSTANCE},
            )
            return cur.rowcount

    def reconcile_classes(self, *, floor: int = DEFAULT_CLASS_VOTE_FLOOR,
                          cap: int = DEFAULT_MAX_ACTIVE_CLASSES,
                          dominance_hi: float = DEFAULT_DOMINANCE_HI,
                          dominance_lo: float = DEFAULT_DOMINANCE_LO,
                          dominance_span: int = DEFAULT_DOMINANCE_SPAN,
                          margin: float = DEFAULT_DOMINANCE_MARGIN) -> dict:
        """Recompute the active set from votes and apply activate/deactivate in one sweep.

        A class's `votes` = distinct memories that staged it (entity_candidates, all rows — the
        durable per-memory ledger that survives activation). The active set is the **top `cap`
        concepts by votes that clear `floor`** — dominance only competes for slots once there are
        MORE than `cap` floor-clearing concepts. So while the concept taxonomy fits under the cap, every
        concept above the floor is active (a runaway leader can't lock out smaller legitimate
        concepts); only when concepts compete for the last slots does the dominance gate
        (votes ≥ p × leader_votes, p from `dominance_threshold`) prune the marginal ones.
        Deactivation is hysteretic: an already-active concept is demoted only once it drops below
        floor, out of the cap, or — when competing — below (p − margin) × leader, so it doesn't
        flap near the boundary. Blocked classes are never touched. Returns
        {activated, deactivated, p, leader_votes, n_classes, competing}.
        """
        with self._pool.connection() as conn:
            rows = conn.execute(
                r"""SELECT e.canonical_name AS name, e.status, e.pinned,
                           count(DISTINCT ec.memory_id) AS votes
                    FROM entities e
                    JOIN nodes n ON n.id = e.node_id
                    LEFT JOIN entity_candidates ec
                      ON ec.entity_type = %(cls)s
                     AND ec.normalized_name =
                         regexp_replace(lower(btrim(e.canonical_name)), '\s+', ' ', 'g')
                    WHERE e.entity_type = %(cls)s AND e.status <> 'blocked'
                    GROUP BY e.canonical_name, e.status, e.pinned""",
                {"cls": ENTITY_CLASS},
            ).fetchall()

        n = len(rows)
        p = dominance_threshold(n, hi=dominance_hi, lo=dominance_lo, span=dominance_span)
        p_deact = max(0.0, p - margin)
        qualifiers = [r for r in rows if r["votes"] >= floor]
        leader = max((r["votes"] for r in qualifiers), default=0)
        ranked = sorted(qualifiers, key=lambda r: (-r["votes"], r["name"]))
        top = {r["name"] for r in ranked[:cap]}
        # Dominance only bites when more classes clear the floor than there are slots; until
        # then there's room for every floor-clearing class, so the leader ratio is ignored.
        competing = len(qualifiers) > cap

        activated, deactivated = [], []
        for r in rows:
            name, votes = r["name"], r["votes"]
            ratio = votes / leader if leader else 0.0
            in_top = name in top
            if r["status"] != STATUS_ACTIVE:
                if votes >= floor and in_top and (not competing or ratio >= p):
                    self.activate_class(name)
                    activated.append(name)
            elif not r["pinned"] and (
                    votes < floor or not in_top or (competing and ratio < p_deact)):
                self.deactivate_class(name)
                deactivated.append(name)
        return {"activated": activated, "deactivated": deactivated, "p": p,
                "leader_votes": leader, "n_classes": n, "competing": competing}

    def merge_classes(self, source: str, target: str, *,
                      requeue_voters: bool = False) -> "Entity | None":
        """Fold category `source` into `target` (also how you RENAME: merge into the new name).
        `target` is ensured active; every memory that belonged to `source` is re-linked to
        `target`; `source`'s name is recorded as an alias of `target`; then `source` is
        tombstoned and its now-redundant instances/candidates purged. With requeue_voters, the
        memories that fed `source` are re-queued for enrichment so its instances rebuild under
        `target` (the re-run is authoritative). Returns the target class, or None if `source`
        doesn't exist."""
        src = self.get_entity_class(source)
        if src is None:
            return None
        # Snapshot the voters BEFORE we move links / tombstone, so we re-run exactly the
        # memories that produced the source class.
        voter_ids = [n.id for n in self.class_voters(source)] if requeue_voters else []
        tgt = self.upsert_entity_class(target, active=True)
        if tgt.node.id == src.node.id:
            return tgt
        with self._pool.connection() as conn, conn.transaction():
            # carry source's belongs_to memberships onto the target, then drop the originals
            conn.execute(
                """INSERT INTO entity_links (memory_id, entity_id, link_type, weight)
                   SELECT memory_id, %(tid)s, link_type, weight FROM entity_links
                   WHERE entity_id = %(sid)s AND link_type = %(bt)s
                   ON CONFLICT (memory_id, entity_id, link_type) DO NOTHING""",
                {"tid": tgt.node.id, "sid": src.node.id, "bt": BELONGS_TO})
            # carry source's INSTANCE memberships onto the target — every instance that was a
            # `source` becomes a `target` (deduped), so no instance is lost in the fold;
            # human-origin links win over derived on conflict
            conn.execute(
                """INSERT INTO entity_class_links (instance_id, class_id, weight, origin)
                   SELECT instance_id, %(tid)s, weight, origin FROM entity_class_links
                   WHERE class_id = %(sid)s
                   ON CONFLICT (instance_id, class_id) DO UPDATE
                       SET origin = CASE
                           WHEN entity_class_links.origin = 'human' OR EXCLUDED.origin = 'human'
                           THEN 'human' ELSE 'derived' END""",
                {"tid": tgt.node.id, "sid": src.node.id})
            # record the old name as an alias so the merged category is still discoverable;
            # mark embed_dirty so the target's embedding is refreshed
            conn.execute(
                """UPDATE entities SET aliases = ARRAY(
                       SELECT DISTINCT unnest(aliases || ARRAY[%(alias)s]::text[])),
                       embed_dirty = true
                   WHERE node_id = %(tid)s""",
                {"alias": src.canonical_name, "tid": tgt.node.id})
        # The merge re-points every source instance's is_a onto the target; the memories that
        # mention those instances must recrawl to re-fit their typing (the 2-hop the belongs_to
        # voter requeue does not reach).
        for inst in self.instances_of(source):
            for n in self.memories_linked(inst.node.id, link_type=MENTIONS):
                self.request_reanalysis(n.id)
        self.block_class(source, purge_instances=True, requeue_voters=False)
        if voter_ids:
            with self._pool.connection() as conn:
                conn.execute(
                    "UPDATE nodes SET enrichment_status = 'pending', enriched_hash = NULL "
                    "WHERE id = ANY(%(ids)s) AND type = 'note'", {"ids": voter_ids})
        return self.get_entity(tgt.node.id)

    def merge_instances(self, source_id: UUID, target_id: UUID) -> "Entity | None":
        """Fold instance `source_id` into `target_id`: every memory that --mentions--> source is
        re-linked to target (deduped, link roles kept), source's class memberships are carried onto
        target, source's canonical_name and aliases are recorded as target aliases, then source is
        tombstoned. The survivor's
        embedding is dirtied (its mention context changed). Returns the surviving target, or None if
        either id is not an instance entity."""
        src = self.get_entity(source_id)
        tgt = self.get_entity(target_id)
        if src is None or tgt is None:
            return None
        if src.entity_type != ENTITY_INSTANCE or tgt.entity_type != ENTITY_INSTANCE:
            return None
        if source_id == target_id:
            return tgt
        with self._pool.connection() as conn, conn.transaction():
            # re-point mentions links source -> target, dedup against existing; the role
            # travels with the link (it ranks describe/judge evidence), and on a shared
            # memory the target's own role wins
            conn.execute(
                """INSERT INTO entity_links (memory_id, entity_id, link_type, weight, role)
                   SELECT memory_id, %(tid)s, link_type, weight, role FROM entity_links
                   WHERE entity_id = %(sid)s
                   ON CONFLICT (memory_id, entity_id, link_type) DO UPDATE
                       SET role = COALESCE(entity_links.role, EXCLUDED.role)""",
                {"tid": target_id, "sid": source_id})
            conn.execute(
                "DELETE FROM entity_links WHERE entity_id = %(sid)s",
                {"sid": source_id})
            # carry class memberships; human-origin links win over derived on conflict
            conn.execute(
                """INSERT INTO entity_class_links (instance_id, class_id, weight, origin)
                   SELECT %(tid)s, class_id, weight, origin FROM entity_class_links
                   WHERE instance_id = %(sid)s
                   ON CONFLICT (instance_id, class_id) DO UPDATE
                       SET origin = CASE
                           WHEN entity_class_links.origin = 'human' OR EXCLUDED.origin = 'human'
                           THEN 'human' ELSE 'derived' END""",
                {"tid": target_id, "sid": source_id})
            # record the source's name AND its aliases on the target (a chain A -> B -> C must
            # keep A's name), never the target's own name, and dirty the survivor's embedding
            conn.execute(
                """UPDATE entities SET aliases = ARRAY(
                       SELECT DISTINCT a FROM unnest(aliases || %(names)s::text[]) AS a
                       WHERE lower(a) <> lower(canonical_name)),
                       embed_dirty = true
                   WHERE node_id = %(tid)s""",
                {"names": [src.canonical_name, *src.aliases], "tid": target_id})
            # tombstone the source node (cascades entities row, entity_links, entity_class_links)
            # and log it — mirrors reconcile_instances' deletion pattern
            conn.execute(
                """WITH del AS (
                       DELETE FROM nodes WHERE id = %(sid)s
                       RETURNING id, source, external_id, path::text AS path, content_hash)
                   INSERT INTO ingest_log (event, source, external_id, node_id, path, content_hash)
                   SELECT 'node_deleted', source, external_id, id, path, content_hash FROM del""",
                {"sid": source_id})
        return self.get_entity(target_id)

    def _recrawl_mentioners(self, instance_id: UUID) -> None:
        """Queue every memory that mentions this instance for recrawl, so it re-fits its typing
        after a manual instance edit."""
        for n in self.memories_linked(instance_id, link_type=MENTIONS):
            self.request_reanalysis(n.id)

    def reparent_instance(self, instance_id: UUID, new_category: str) -> "Entity | None":
        """Recategorize an instance: set its class membership to exactly `new_category`. Identity
        is name-only now, so this is a link operation — the node never moves or re-keys. Existing
        is_a links are replaced with the one to `new_category`. Returns the instance, or None if
        `instance_id` isn't an instance entity. (For multi-class membership use
        add_instance_class / remove_instance_class.)"""
        inst = self.get_entity(instance_id)
        if inst is None or inst.entity_type != ENTITY_INSTANCE:
            return None
        class_id = self._ensure_class_id(new_category)   # outside the txn (mkdir -p / deadlock)
        with self._pool.connection() as conn, conn.transaction():
            conn.execute("DELETE FROM entity_class_links WHERE instance_id = %s", (instance_id,))
            conn.execute("UPDATE entities SET embed_dirty = true WHERE node_id = %s", (instance_id,))
            if class_id is not None:
                self._link_instance_class(conn, instance_id, class_id, origin="human")
        self._recrawl_mentioners(instance_id)
        return self.get_entity(instance_id)

    def add_instance_class(self, instance_id: UUID, category: str) -> "Entity | None":
        """Add a category membership to an instance (instance --is_a--> class). Idempotent.
        Returns the instance, or None if it isn't an instance entity (or the category is blocked)."""
        inst = self.get_entity(instance_id)
        if inst is None or inst.entity_type != ENTITY_INSTANCE:
            return None
        class_id = self._ensure_class_id(category)       # outside the txn (mkdir -p / deadlock)
        if class_id is not None:
            with self._pool.connection() as conn, conn.transaction():
                self._link_instance_class(conn, instance_id, class_id, origin="human")
        self._recrawl_mentioners(instance_id)
        return self.get_entity(instance_id)

    def remove_instance_class(self, instance_id: UUID, category: str) -> "Entity | None":
        """Remove a category membership from an instance. Returns the instance, or None if it
        isn't an instance entity."""
        inst = self.get_entity(instance_id)
        if inst is None or inst.entity_type != ENTITY_INSTANCE:
            return None
        cid = node_id(ENTITY_SOURCE, entity_external_id(ENTITY_CLASS, category))
        with self._pool.connection() as conn:
            conn.execute(
                "DELETE FROM entity_class_links WHERE instance_id = %s AND class_id = %s",
                (instance_id, cid))
            conn.execute("UPDATE entities SET embed_dirty = true WHERE node_id = %s", (instance_id,))
        self._recrawl_mentioners(instance_id)
        return self.get_entity(instance_id)

    def reset_entities(self) -> dict:
        """Wipe the ENTIRE entity graph so it can be rebuilt from scratch by re-running
        enrichment: every class and instance node (each logged 'node_deleted'), plus all
        staged candidates and memory->entity links. Memories are untouched — only the derived
        entity layer is removed. Deleting the entity nodes cascades their `entities` rows and
        `entity_links`; `entity_candidates` (keyed off memories) is cleared explicitly. The
        reserved `entities` root folder is left in place. DESTRUCTIVE and irreversible — take a
        backup first. Returns the counts removed."""
        with self._pool.connection() as conn, conn.transaction():
            cur = conn.execute(
                """WITH del AS (
                       DELETE FROM nodes WHERE source = %(s)s
                       RETURNING id, source, external_id, path::text AS path, content_hash
                   )
                   INSERT INTO ingest_log
                       (event, source, external_id, node_id, path, content_hash)
                   SELECT 'node_deleted', source, external_id, id, path, content_hash FROM del""",
                {"s": ENTITY_SOURCE},
            )
            entities = cur.rowcount
            links = conn.execute("DELETE FROM entity_links").rowcount
            candidates = conn.execute("DELETE FROM entity_candidates").rowcount
        return {"entities": entities, "links": links, "candidates": candidates}

    def requeue_enrichment(self, *, node_type: str = "note",
                           source: str | None = None) -> int:
        """Force matching nodes back to 'pending' for a FULL re-enrichment, regardless of
        whether their content changed (unlike flag_stale_enrichment, which only re-flags nodes
        whose content_hash moved). Clears enriched_hash so the pass is treated as fresh.
        Defaults to memory notes — the nodes the enrichment pipeline actually processes — so
        it won't enqueue folders. Returns rows queued."""
        sql = ("UPDATE nodes SET enrichment_status = 'pending', enriched_hash = NULL "
               "WHERE type = %(t)s")
        params: dict = {"t": node_type}
        if source is not None:
            sql += " AND source = %(source)s"
            params["source"] = source
        with self._pool.connection() as conn:
            return conn.execute(sql, params).rowcount

    # ---- importance --------------------------------------------------------
    def set_importance(self, id: UUID, value: float, *, modifier: str = "manual") -> Node:
        with self._pool.connection() as conn, conn.transaction():
            row = conn.execute(
                "SELECT path::text AS path, importance_modifiers FROM nodes WHERE id = %s", (id,)
            ).fetchone()
            if row is None:
                raise LookupError(f"node not found: {id}")
            mods = dict(row["importance_modifiers"])
            mods[modifier] = value
            conn.execute(
                "UPDATE nodes SET importance_modifiers = %(m)s WHERE id = %(id)s",
                {"m": Jsonb(mods), "id": id},
            )
            # A folder's value cascades to its subtree, so recompute every descendant (for a file
            # the subtree is just itself). Same transaction as the write — keep modifiers atomic.
            self._recompute_in_conn(conn, under=row["path"])
        return self.get(id)

    def recompute_importance(self, *, source: str | None = None,
                             ids: "list[UUID] | None" = None, under: str | None = None) -> int:
        with self._pool.connection() as conn, conn.transaction():
            return self._recompute_in_conn(conn, source=source, ids=ids, under=under)

    def _project_dormant_days(self, conn: psycopg.Connection,
                              keys: "list[str] | None" = None) -> "dict[str, int | None]":
        """{project_key_text: whole days since that project's newest note activity}.
        All projects when `keys` is None. A project with no activity signal maps to None
        ("no signal, no decay"). Two callers: _recompute_in_conn (all) and
        significance_view (one)."""
        if self._policy.projects is None:
            return {}
        rows = conn.execute(project_activity_sql(self._policy.projects),
                            {**project_params(self._policy.projects), "keys": keys}).fetchall()
        return {r["project"]: r["dormant_days"] for r in rows}

    def _recompute_in_conn(self, conn: psycopg.Connection, *, source: str | None = None,
                           ids: "list[UUID] | None" = None, under: str | None = None) -> int:
        """Recompute stored importance for the scoped nodes using an open connection.

        Each node's `inherited` factor is derived fresh here — the summed `manual` values of its
        strict ancestor folders — so a directory's value cascades (stacking) to its whole subtree.
        Likewise `project_dormant_days`: whole days since the node's
        project last changed, from one per-project aggregate, so a quiet repo's docs shed
        their inherited boost with the policy's dormancy half-life (6 months in the
        shipped example; no `[projects]` section ⇒ no decay)."""
        sql = ("SELECT n.id, n.access_count, n.last_accessed_at, n.ingested_at, "
               "n.importance_modifiers, COALESCE(("
               "  SELECT SUM((a.importance_modifiers->>'manual')::float) FROM nodes a "
               "  WHERE a.type = 'folder' AND n.path <@ a.path AND a.path <> n.path"
               "), 0.0) AS inherited, "
               + project_key_sql("n.path", self._policy.projects) + "::text AS project "
               "FROM nodes n")
        conds, params = [], dict(project_params(self._policy.projects))
        if source is not None:
            conds.append("n.source = %(source)s"); params["source"] = source
        if ids is not None:
            conds.append("n.id = ANY(%(ids)s)"); params["ids"] = list(ids)
        if under is not None:
            conds.append("n.path <@ %(under)s::ltree"); params["under"] = under
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        rows = conn.execute(sql, params).fetchall()
        # One aggregate for every project touched by this scope (None = no project docs).
        keys = sorted({r["project"] for r in rows if r["project"] is not None})
        dormant = self._project_dormant_days(conn, keys=keys) if keys else {}
        for r in rows:
            stats = NodeStats(
                access_count=r["access_count"], last_accessed_at=r["last_accessed_at"],
                ingested_at=r["ingested_at"],
                modifiers={**r["importance_modifiers"], "inherited": r["inherited"]},
                project_dormant_days=(dormant.get(r["project"])
                                      if r["project"] is not None else None),
            )
            importance, modifiers = compute_importance(stats, self._modifier_registry)
            # No-op gate: this runs after every pull tick (obsidian: period 0), so an
            # unconditional write meant a new row version per node per tick even when
            # nothing changed — churn that can corrupt the HNSW index under autovacuum.
            # Compare in the column's own types (importance is real/float4 — a Python
            # float64 equality check would almost never match the stored value).
            # project_dormant_days is a whole number, so the computed value is
            # bit-identical across ticks within a day and this gate keeps holding.
            conn.execute(
                "UPDATE nodes SET importance = %(imp)s, importance_modifiers = %(mods)s "
                "WHERE id = %(id)s "
                "  AND (importance IS DISTINCT FROM %(imp)s::real "
                "       OR importance_modifiers IS DISTINCT FROM %(mods)s::jsonb)",
                {"imp": importance, "mods": Jsonb(modifiers), "id": r["id"]},
            )
        return len(rows)
