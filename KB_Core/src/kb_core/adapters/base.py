"""Shared adapter contracts: the Adapter and Ingestor protocols, and SyncResult."""

import itertools
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from kb_core.domain import Node, NodeRecord

if TYPE_CHECKING:
    from kb_core.knowledge_base import KnowledgeBase


@dataclass
class SyncResult:
    source: str
    # Records that reached the store this run, whether or not anything changed: a pull
    # source re-upserts its whole corpus every tick and the no-op gate skips the unchanged
    # ones. On a dry run, the records that WOULD be upserted.
    upserted: int = 0
    # Of `upserted`, how many actually wrote a node row (created, or changed past the
    # no-op gate). 0 on an idle tick. Not computed on a dry run (stays 0).
    changed: int = 0
    deleted: int = 0
    errors: list[str] = field(default_factory=list)
    # Why a mirror-prune was suppressed this tick (see SyncService.sync). None whenever
    # the prune ran normally, and on a source that does not prune at all.
    prune_skipped: "str | None" = None


def write_counter(kb) -> "Callable[[], int]":
    """A reader of kb.node_writes, for telling a real write from a no-op upsert (read it
    before and after; a difference means a node row was written). A KnowledgeBase
    stand-in without node_writes gets a counter that moves on every read, so every
    upsert counts as a change."""
    fn = getattr(kb, "node_writes", None)
    if callable(fn):
        return fn
    tick = itertools.count()
    return lambda: next(tick)


@runtime_checkable
class Ingestor(Protocol):
    """How one source's records are persisted — its insert/upsert strategy.

    Optional per source. A source defines `adapters/<source>/ingest.py` exposing an
    Ingestor only when it needs persistence beyond a plain core upsert: e.g. writing a
    typed-extension row (the `*_ext` table for a structured `type`) in the same
    transaction as the node, source-specific conflict handling, or dedup beyond the
    core (id, content_hash) funnel. When a source defines no ingestor, SyncService
    falls back to DEFAULT_INGESTOR (a straight `kb.upsert`).

    Contract: implementations MUST be idempotent — re-ingesting the same record
    upserts in place, never duplicates — and MUST flow through `KnowledgeBase` so
    validation, `mkdir -p` placement, and deterministic identity stay uniform across
    every writer. Returns the persisted Node so SyncService can count/inspect it.
    """

    def upsert(self, kb: "KnowledgeBase", record: NodeRecord) -> Node:
        ...


class DefaultIngestor:
    """Core-only persistence: a straight `kb.upsert`.

    The fallback used when a source defines no `ingest.py` (e.g. the core-only
    Obsidian vault, whose notes are plain `type="note"` nodes with no extension).
    """

    def upsert(self, kb: "KnowledgeBase", record: NodeRecord) -> Node:
        return kb.upsert(record)


# Singleton fallback ingestor; stateless, so one instance is shared by every source
# that doesn't override persistence.
DEFAULT_INGESTOR: Ingestor = DefaultIngestor()


@runtime_checkable
class Adapter(Protocol):
    source: str

    def fetch(self) -> Iterator[NodeRecord]:
        """Yield a NodeRecord for every item currently present in the source.

        Must raise if the source is unreachable, so SyncService aborts before pruning.
        """
        ...

    # Optional attribute (intentionally not a Protocol member, so core-only adapters
    # stay valid without declaring it): an adapter may expose a custom `ingestor`
    # — typically wired from its sibling `ingest.py` — to override how its records are
    # persisted. Absent -> SyncService uses DEFAULT_INGESTOR.
    #     ingestor: Ingestor


@runtime_checkable
class PushDecoder(Protocol):
    """A push source's translator: one staged payload -> a NodeRecord.

    The push analogue of Adapter.fetch(): pure translation only — no DB access, no id
    computation, no retrieval decisions (Principle #2). The receiver stages raw payloads;
    InboxDrainService calls decode() at drain time.
    """
    source: str

    def decode(self, payload: dict) -> NodeRecord:
        ...
