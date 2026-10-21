from kb_core.adapters.base import (
    DEFAULT_INGESTOR, Adapter, DefaultIngestor, Ingestor, SyncResult,
)
from kb_core.domain import NodeRecord


def test_sync_result_defaults():
    r = SyncResult(source="obsidian")
    assert r.upserted == 0 and r.deleted == 0 and r.errors == []


def test_adapter_protocol_is_satisfied_by_a_simple_class():
    class Dummy:
        source = "dummy"
        def fetch(self):
            yield NodeRecord(source="dummy", external_id="1", type="note",
                             content="x", slug="x")
    assert isinstance(Dummy(), Adapter)


def test_adapter_protocol_allows_an_optional_ingestor():
    # Declaring a custom ingestor must not disqualify the adapter (the attribute is
    # intentionally not a required Protocol member).
    class WithIngest:
        source = "with_ingest"
        ingestor = DEFAULT_INGESTOR
        def fetch(self):
            yield NodeRecord(source="with_ingest", external_id="1", type="note",
                             content="x", slug="x")
    assert isinstance(WithIngest(), Adapter)


def test_default_ingestor_satisfies_the_ingestor_protocol():
    assert isinstance(DEFAULT_INGESTOR, Ingestor)
    assert isinstance(DefaultIngestor(), Ingestor)


def test_default_ingestor_delegates_to_kb_upsert():
    class FakeKB:
        def __init__(self):
            self.records = []
        def upsert(self, record):
            self.records.append(record)
            return "persisted-node"

    kb = FakeKB()
    record = NodeRecord(source="s", external_id="1", type="note", content="x", slug="x")
    assert DEFAULT_INGESTOR.upsert(kb, record) == "persisted-node"
    assert kb.records == [record]
