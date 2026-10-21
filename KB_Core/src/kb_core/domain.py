"""Core dataclasses: the in-code contract and read models."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

# Placeholder type for Phase 1 (no typed extensions yet).
ExtensionPayload = dict[str, Any]


@dataclass
class NodeRecord:
    """What every writer (adapter or direct) produces. id/path/hash are core-computed."""
    source: str
    external_id: str
    type: str
    content: str
    slug: str
    parent_path: str = ""
    extension: ExtensionPayload | None = None
    title: str | None = None
    summary: str | None = None
    uri: str | None = None
    tags: list[str] = field(default_factory=list)
    source_created_at: datetime | None = None
    source_updated_at: datetime | None = None
    # When the content HAPPENED (meeting date, todo completion) — from source metadata
    # only, never file timestamps. None = the source carries no explicit event signal.
    event_at: datetime | None = None


@dataclass
class Node:
    """A persisted node, as read from the store."""
    id: UUID
    source: str
    external_id: str
    type: str
    parent_id: UUID | None
    slug: str
    path: str
    content: str
    content_hash: str
    title: str | None
    summary: str | None
    uri: str | None
    tags: list[str]
    source_created_at: datetime | None
    source_updated_at: datetime | None
    ingested_at: datetime
    updated_at: datetime
    importance: float
    importance_modifiers: dict
    access_count: int
    last_accessed_at: datetime | None
    enrichment_status: str = "none"
    enriched_at: datetime | None = None
    enriched_hash: str | None = None
    last_analyzed_at: datetime | None = None
    # Entity-system participation. False marks a memory whose content is essentially noise: it
    # is excluded from entity voting/creation/linking (set by the analysis 'entity_gate' pass),
    # while remaining a normal, searchable memory. entity_skip_reason records the why.
    entity_eligible: bool = True
    entity_skip_reason: str | None = None
    # Chronological retirement. A non-null dead_at marks a memory the gate judged no longer
    # time-relevant: frozen as-is — past entity influence kept, no new influence, never crawled
    # again — until a human revives it. Orthogonal to noise; dead_reason records the why.
    dead_at: datetime | None = None
    dead_reason: str | None = None
    # Human-driven exclusion from entity analysis. Orthogonal to noise/dead: an explicit switch set
    # on the toggled node (folder or memory); a node is effectively excluded if it or any ancestor
    # has the flag (resolved via the ltree path). analysis_excluded_at records when, for display.
    analysis_excluded: bool = False
    analysis_excluded_at: datetime | None = None
    # Embedding bookkeeping (Phase 3 write path). embedded_hash is the content_hash of the
    # exact text behind the current `embedding` vector; the enricher skips re-embedding when it
    # matches content_hash.
    embedded_hash: str | None = None
    # Document-kind facet ("meeting notes", "implementation plan", …): what the memory IS,
    # coined open-vocabulary by the doc_kind enrichment pass. None = not yet classified.
    doc_kind: str | None = None
    # When the content HAPPENED (frontmatter/filename event signal). None = no explicit
    # signal; read paths fall back with COALESCE(event_at, source_created_at).
    event_at: datetime | None = None

    @property
    def lifecycle(self) -> str:
        """Display participation state, dead taking precedence over noise: 'dead' (retired/frozen),
        'noise' (excluded + influence retracted), or 'active'. Drives the 'shut off' memories view."""
        if self.dead_at is not None:
            return "dead"
        if not self.entity_eligible:
            return "noise"
        return "active"


@dataclass
class NodePatch:
    """Editable fields for update(); None means 'leave unchanged'."""
    title: str | None = None
    content: str | None = None
    summary: str | None = None
    uri: str | None = None
    tags: list[str] | None = None
    doc_kind: str | None = None


@dataclass
class SearchResult:
    node: Node
    rank: float          # FTS-only: raw ts_rank; hybrid: the fused RRF score (ordering key)
    snippet: str
    score: float = 0.0   # importance/recency-decayed FTS score (0 = not an FTS hit)
    similarity: float | None = None   # hybrid: kNN-arm cosine similarity (None = FTS-only)


@dataclass
class Edge:
    """A typed, directed, optionally-weighted relationship between two nodes."""
    source_id: UUID
    target_id: UUID
    relation_type: str
    weight: float | None = None


@dataclass
class Entity:
    """A concept node (the universal contract) plus its entity-specific extension."""
    node: Node
    entity_type: str
    canonical_name: str
    aliases: list[str] = field(default_factory=list)
    description: str | None = None
    status: str = "active"  # 'inactive' (not yet recurred) | 'active' | 'blocked' (human-removed)
    pinned: bool = False     # human-pinned: protected from automatic reconcile deactivation

    @property
    def active(self) -> bool:
        """Established and eligible to be offered as a category (status == 'active')."""
        return self.status == "active"
