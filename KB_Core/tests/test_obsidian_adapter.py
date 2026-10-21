import pytest

from kb_core.adapters.obsidian.adapter import ObsidianAdapter


def _write(vault, rel, text):
    p = vault / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def test_fetch_uses_path_as_id_and_never_writes(tmp_path):
    p = _write(tmp_path, "Projects/KB MCP/design.md", "# Design\nbody text")
    before = p.read_text()
    records = list(ObsidianAdapter(tmp_path).fetch())
    assert len(records) == 1
    rec = records[0]
    assert rec.parent_path == "Projects.KB_MCP"
    assert rec.slug == "design"
    assert rec.title == "Design"
    assert rec.content.strip() == "# Design\nbody text"  # full body; H1 not stripped (lossless)
    assert rec.type == "note" and rec.source == "obsidian"
    assert rec.external_id == "Projects/KB MCP/design.md"  # path-based id (no frontmatter id)
    assert p.read_text() == before                          # vault never mutated


def test_fetch_is_idempotent_keeps_same_id(tmp_path):
    _write(tmp_path, "note.md", "hello")
    a = ObsidianAdapter(tmp_path)
    assert list(a.fetch())[0].external_id == list(a.fetch())[0].external_id == "note.md"


def test_fetch_skips_dotfolders(tmp_path):
    _write(tmp_path, ".obsidian/cfg.md", "x")
    _write(tmp_path, "real.md", "y")
    assert sorted(r.slug for r in ObsidianAdapter(tmp_path).fetch()) == ["real"]


def test_fetch_empty_body_falls_back_to_title(tmp_path):
    _write(tmp_path, "blank.md", "---\ntitle: My Title\n---\n")
    rec = list(ObsidianAdapter(tmp_path).fetch())[0]
    assert rec.content == "My Title"


def test_fetch_malformed_frontmatter_is_reported_and_skipped(tmp_path):
    _write(tmp_path, "bad.md", "---\ntitle: [unterminated\n---\nbody")
    a = ObsidianAdapter(tmp_path)
    assert list(a.fetch()) == []
    assert any("bad.md" in e for e in a.errors)


def test_fetch_missing_vault_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        list(ObsidianAdapter(tmp_path / "nope").fetch())


def test_fetch_unsanitizable_filename_is_reported_and_skipped(tmp_path):
    _write(tmp_path, "🎉.md", "party")   # sanitizes to an empty ltree label
    _write(tmp_path, "ok.md", "fine")
    a = ObsidianAdapter(tmp_path)
    slugs = sorted(r.slug for r in a.fetch())
    assert slugs == ["ok"]               # the bad file is skipped, the sync proceeds
    assert len(a.errors) == 1


def test_root_label_prefixes_placement_but_not_identity(tmp_path):
    _write(tmp_path, "TeamHub/Foo.md", "# Foo\nbody")
    rec = list(ObsidianAdapter(tmp_path, root_label="Obsidian").fetch())[0]
    assert rec.parent_path == "Obsidian.TeamHub"   # the whole vault hangs off the layer
    assert rec.slug == "Foo"
    assert rec.external_id == "TeamHub/Foo.md"      # identity is untouched by the layer


def test_root_label_lifts_a_root_level_note_under_the_layer(tmp_path):
    _write(tmp_path, "README.md", "# Readme")
    rec = list(ObsidianAdapter(tmp_path, root_label="Obsidian").fetch())[0]
    assert rec.parent_path == "Obsidian"
    assert rec.external_id == "README.md"


def test_fetch_prefers_frontmatter_id_for_move_survival(tmp_path):
    p = _write(tmp_path, "kept.md", "---\nid: fixed-id-123\n---\nbody")
    before = p.stat().st_mtime_ns
    rec = list(ObsidianAdapter(tmp_path).fetch())[0]
    assert rec.external_id == "fixed-id-123"   # committed frontmatter id wins (survives moves)
    assert p.stat().st_mtime_ns == before      # file untouched


def test_fetch_derives_event_at_from_frontmatter_and_filename(tmp_path):
    from datetime import datetime, timezone
    _write(tmp_path, "Meetings/2025-11-04 - Planning.md", "---\ndate: 2025-11-04\n---\nbody")
    _write(tmp_path, "Meetings/2026_03_25_Budget_Review.md", "no frontmatter body")
    _write(tmp_path, "plain.md", "just a note")
    by_slug = {r.slug: r for r in ObsidianAdapter(tmp_path).fetch()}
    assert by_slug["2025_11_04_Planning"].event_at == datetime(
        2025, 11, 4, tzinfo=timezone.utc)          # frontmatter date
    assert by_slug["2026_03_25_Budget_Review"].event_at == datetime(
        2026, 3, 25, tzinfo=timezone.utc)          # filename fallback
    assert by_slug["plain"].event_at is None       # no signal — never a file timestamp


def test_sync_persists_and_backfills_event_at(tmp_path, kb, pool):
    """upsert stores event_at, and an unchanged-content re-sync still recomputes it —
    the mechanism that backfills rows ingested before the column existed."""
    from datetime import datetime, timezone
    from kb_core.sync import SyncService
    p = _write(tmp_path, "Meetings/2025-11-04 - Planning.md", "meeting body")
    svc = SyncService(kb, pool)
    svc.sync(ObsidianAdapter(tmp_path))
    def fetch_event_at():
        with pool.connection() as conn:
            return conn.execute(
                "SELECT event_at FROM nodes WHERE type = 'note'").fetchone()["event_at"]
    assert fetch_event_at() == datetime(2025, 11, 4, tzinfo=timezone.utc)
    # Simulate a pre-event_at row (NULL), then re-sync with content byte-identical:
    # the hash gate skips source_* but event_at must still be recomputed.
    with pool.connection() as conn:
        conn.execute("UPDATE nodes SET event_at = NULL WHERE type = 'note'")
    svc.sync(ObsidianAdapter(tmp_path))
    assert fetch_event_at() == datetime(2025, 11, 4, tzinfo=timezone.utc)


def test_fetch_with_stamp_on_resolves_a_stripped_id_to_the_stamped_identity(tmp_path):
    # A rewrite that drops the `id:` line (the vault mirror hook regenerates its copy from the
    # repo file; an external sync can revert a stamp) can land between prepare() and fetch().
    # With stamping on, the unstamped read must yield the SAME external_id the stamper writes —
    # otherwise the note collides with its own node and gets pruned (a past delete/recreate churn).
    from kb_core.adapters.obsidian.stamp import mint_id
    stamped = mint_id("Projects/plan.md")
    p = _write(tmp_path, "Projects/plan.md", f"---\nid: {stamped}\n---\n# Plan\nbody")
    a = ObsidianAdapter(tmp_path, stamp=True)
    assert list(a.fetch())[0].external_id == stamped
    p.write_text("# Plan\nbody", encoding="utf-8")            # the stamp is gone
    assert list(a.fetch())[0].external_id == stamped          # identity is not
    assert p.read_text() == "# Plan\nbody"                     # fetch still never writes


def test_fetch_with_stamp_on_still_honours_an_app_assigned_id(tmp_path):
    # Notes that carry their own id (the todo app's `todo_<ts>_<hash>`) keep it: only the
    # no-id fallback changes under stamping, never a committed frontmatter id.
    _write(tmp_path, "Todos/t.md", "---\nid: todo_20250102_030405_c0ffee42\n---\nbody")
    rec = list(ObsidianAdapter(tmp_path, stamp=True).fetch())[0]
    assert rec.external_id == "todo_20250102_030405_c0ffee42"


def test_fetch_never_follows_a_symlinked_note(tmp_path):
    """A linked note can live outside the vault; stamping would write an id into it."""
    vault, outside = tmp_path / "vault", tmp_path / "outside"
    _write(vault, "real.md", "inside")
    target = _write(outside, "elsewhere.md", "outside the vault")
    (vault / "linked.md").symlink_to(target)
    assert sorted(r.slug for r in ObsidianAdapter(vault, stamp=True).fetch()) == ["real"]
    assert target.read_text(encoding="utf-8") == "outside the vault"
