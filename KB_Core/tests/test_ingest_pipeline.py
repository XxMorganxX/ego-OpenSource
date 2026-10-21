from datetime import datetime, timezone

from kb_core.domain import NodeRecord
from kb_core.ingest.pipeline import IngestContext, Pipeline
from kb_core.ingest.transforms import NormalizeContent, default_pipeline


def _rec(content="hello"):
    return NodeRecord(source="t", external_id="1", type="note", content=content, slug="s")


def _ctx():
    return IngestContext(source="t", kb=None, now=datetime.now(timezone.utc))


def test_normalize_strips_and_normalizes_newlines():
    out = NormalizeContent().apply(_rec("  a\r\nb  "), _ctx())
    assert out.content == "a\nb"


def test_normalize_drops_empty():
    assert NormalizeContent().apply(_rec("   "), _ctx()) is None


def test_pipeline_run_record_chains_and_drops():
    drop = type("Drop", (), {"name": "d", "apply": lambda self, r, c: None})()
    assert Pipeline(record=[drop]).run_record(_rec(), _ctx()) is None
    assert Pipeline(record=[NormalizeContent()]).run_record(_rec("x"), _ctx()).content == "x"


def test_default_pipeline_shape():
    p = default_pipeline()
    assert [t.name for t in p.record] == ["normalize_content"]
    assert [t.name for t in p.post] == ["flag_enrichment", "recompute_importance"]
