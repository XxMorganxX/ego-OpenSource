"""The ingest transform pipeline: per-record map + whole-source post stages.

This is the one place every ingested record is analyzed/transformed, uniform across
pull (SyncService) and push (InboxDrainService). A `Pipeline` is a plain dataclass of
two ordered stage lists, so a source can compose its own (or extend `default_pipeline`)
and hand it to sync()/drain() — per-adapter transformation stays explicit and separable.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from kb_core.domain import NodeRecord

if TYPE_CHECKING:
    from kb_core.knowledge_base import KnowledgeBase


@dataclass
class IngestContext:
    source: str
    kb: "KnowledgeBase"
    now: datetime


@runtime_checkable
class RecordTransform(Protocol):
    name: str

    def apply(self, record: NodeRecord, ctx: IngestContext) -> NodeRecord | None:
        """Return the (possibly rewritten) record, or None to DROP it."""
        ...


@runtime_checkable
class PostIngestTransform(Protocol):
    name: str

    def apply(self, ctx: IngestContext, seen_ids: set[str]) -> None:
        ...


@dataclass
class Pipeline:
    record: list[RecordTransform] = field(default_factory=list)
    post: list[PostIngestTransform] = field(default_factory=list)

    def run_record(self, record: NodeRecord, ctx: IngestContext) -> NodeRecord | None:
        for t in self.record:
            record = t.apply(record, ctx)
            if record is None:
                return None
        return record

    def run_post(self, ctx: IngestContext, seen_ids: set[str]) -> None:
        for t in self.post:
            t.apply(ctx, seen_ids)
