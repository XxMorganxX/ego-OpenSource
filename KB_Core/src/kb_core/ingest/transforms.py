"""Default transforms: cheap inline normalization + post-ingest flag/recompute.

`default_pipeline()` is the baseline every source gets; a structured source builds its
own `Pipeline` (or appends stages to these) to make its ingestion transformation
explicit and self-contained.
"""

from kb_core.domain import NodeRecord
from kb_core.ingest.pipeline import IngestContext, Pipeline


class NormalizeContent:
    """Per-record: normalize newlines + strip; drop a record that's left empty."""
    name = "normalize_content"

    def apply(self, record: NodeRecord, ctx: IngestContext) -> NodeRecord | None:
        content = record.content.replace("\r\n", "\n").replace("\r", "\n").strip()
        if not content:
            return None
        record.content = content
        return record


class FlagEnrichment:
    """Post: mark this source's changed/new nodes 'pending' for the enrichment worker."""
    name = "flag_enrichment"

    def apply(self, ctx: IngestContext, seen_ids: set[str]) -> None:
        ctx.kb.flag_stale_enrichment(source=ctx.source)


class RecomputeImportance:
    """Post: recompute stored importance for this source (was sync's hard-coded call)."""
    name = "recompute_importance"

    def apply(self, ctx: IngestContext, seen_ids: set[str]) -> None:
        ctx.kb.recompute_importance(source=ctx.source)


def default_pipeline(*, enrichable: bool = True) -> Pipeline:
    """The baseline pipeline every source gets.

    When `enrichable` is False (a source's `is_enrichable` toggle), FlagEnrichment is
    omitted, so that source's nodes are never marked 'pending' and the enrichment
    worker never runs them through the (LLM) enrichment pipeline.
    """
    post = ([FlagEnrichment(), RecomputeImportance()] if enrichable
            else [RecomputeImportance()])
    return Pipeline(record=[NormalizeContent()], post=post)
