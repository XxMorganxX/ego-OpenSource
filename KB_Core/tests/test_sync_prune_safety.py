"""A mirror-prune must never fire on a fetch that did not observe the whole source.

If the adapter cannot read part of the source (a permissions outage, say), the notes it
could not read are missing from `seen`. Absence from the fetch must not be read as
deletion: a mirror-prune that did so would remove those notes, then the folders they
emptied, then the FK-orphaned entities.
Absence is only trustworthy when the observation was complete.
"""

import pytest

from kb_core.adapters.obsidian.adapter import ObsidianAdapter
from kb_core.domain import NodeRecord
from kb_core.sync import SyncService


def _vault(tmp_path, files):
    for rel, text in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return tmp_path


@pytest.fixture
def unreadable():
    """Drop a path's permissions for one test, always restoring so tmp_path can be cleaned."""
    restore = []

    def _make(path):
        restore.append((path, path.stat().st_mode))
        path.chmod(0o000)
        return path

    yield _make
    for path, mode in restore:
        try:
            path.chmod(mode)
        except OSError:
            pass


def test_unlistable_directory_skips_the_prune(tmp_path, kb, pool, unreadable):
    """A directory os.walk cannot list yields no records and must not look like a deletion."""
    vault = _vault(tmp_path, {"keep.md": "k", "Locked/secret.md": "s"})
    svc = SyncService(kb, pool)
    svc.sync(ObsidianAdapter(vault))
    assert kb.get_by_path("Locked.secret") is not None

    unreadable(vault / "Locked")
    res = svc.sync(ObsidianAdapter(vault))

    assert res.prune_skipped is not None
    assert res.deleted == 0
    assert kb.get_by_path("Locked.secret") is not None


def test_unreadable_file_skips_the_prune(tmp_path, kb, pool, unreadable):
    """One note the adapter cannot read is enough to make the fetch untrustworthy."""
    vault = _vault(tmp_path, {"keep.md": "k", "locked.md": "l"})
    svc = SyncService(kb, pool)
    svc.sync(ObsidianAdapter(vault))

    unreadable(vault / "locked.md")
    res = svc.sync(ObsidianAdapter(vault))

    assert res.prune_skipped is not None
    assert res.deleted == 0
    assert kb.get_by_path("locked") is not None


def test_clean_fetch_still_prunes_a_deleted_note(tmp_path, kb, pool):
    """The guard must not cost the mirror its real job."""
    vault = _vault(tmp_path, {"a.md": "a", "b.md": "b"})
    svc = SyncService(kb, pool)
    svc.sync(ObsidianAdapter(vault))
    (vault / "b.md").unlink()

    res = svc.sync(ObsidianAdapter(vault))

    assert res.prune_skipped is None
    assert res.deleted == 1
    assert kb.get_by_path("b") is None


class _FailingIngestor:
    """Persisting one external_id always fails; every other record lands."""

    def __init__(self, eid):
        self._eid = eid

    def upsert(self, kb, record):
        if record.external_id == self._eid:
            raise RuntimeError("boom")
        return kb.upsert(record)


class _StubAdapter:
    source = "stub"

    def __init__(self, records, ingestor=None):
        self._records = records
        self.errors = []
        if ingestor is not None:
            self.ingestor = ingestor

    def fetch(self):
        yield from self._records


def _rec(eid, slug):
    return NodeRecord(source="stub", external_id=eid, type="note", content="x", slug=slug)


def test_upsert_failure_does_not_block_the_prune(kb, pool):
    """A record that was seen but failed to persist leaves the mirror whole.

    Gating on per-record failures instead of observation would let one permanently
    broken note freeze pruning for the whole source forever.
    """
    svc = SyncService(kb, pool)
    svc.sync(_StubAdapter([_rec("1", "one"), _rec("2", "two")]))
    assert kb.get_by_path("two") is not None

    res = svc.sync(_StubAdapter([_rec("1", "one")], ingestor=_FailingIngestor("1")))

    assert res.errors                    # the upsert really did fail
    assert res.prune_skipped is None     # but the fetch observed everything
    assert res.deleted == 1
    assert kb.get_by_path("two") is None
