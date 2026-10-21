"""publish/lock.py: the path lockfile — pure parts.

The manifest selects with coarse globs, so any new nested file exports the
next run unless something remembers what was already reviewed. These pin the
lockfile's format, what gets content-hashed, and the diff kb-publish and
kb-check-publishable --strict refuse on.
"""
import subprocess

import pytest

from kb_core.publish import lock, manifest

H1 = "a" * 40
H2 = "b" * 40


def test_blob_hash_is_git_hash_object():
    data = b"hello\nworld\n"
    want = subprocess.run(["git", "hash-object", "--stdin"], input=data,
                          capture_output=True, check=True).stdout.decode().strip()
    assert lock.blob_hash(data) == want


@pytest.mark.parametrize("rel,data,pinned", [
    ("KB_Core/src/x.py", b"x = 1\n", False),
    ("README.md", b"# hi\n", False),
    ("evals/retrieval/golden.jsonl", b"{}\n", True),          # data, anywhere
    ("KB_Core/data/rows.jsonl", b"{}\n", True),
    ("evals/concepts/pairs.json", b"{}\n", True),             # json under evals/
    ("KB_Core/tests/fixtures/doc.json", b"{}\n", True),       # json in a fixtures dir
    ("KB_Frontend/src/__fixtures__/graph.json", b"{}\n", True),
    ("KB_Frontend/package.json", b"{}\n", False),             # plain config json
    ("KB_Core/inference.toml", b"[a]\n", True),               # every exported toml
    ("publish.manifest.toml", b"[a]\n", True),
    ("docs/shot.png", b"\x89PNG\r\n\x1a\n", True),            # not strict UTF-8
    ("KB_Core/src/odd.txt", "x".encode("utf-16"), True),
    ("KB_Core/src/bom.md", b"\xef\xbb\xbfhi\n", True),
])
def test_needs_pin(rel, data, pinned):
    assert lock.needs_pin(rel, data) is pinned


def test_render_parse_roundtrip_sorted():
    entries = {"b/x.py": None, "a/y.jsonl": H1, "a/z.toml": H2}
    text = lock.render(entries)
    body = [ln for ln in text.splitlines() if ln and not ln.startswith("#")]
    assert body == [f"a/y.jsonl\t{H1}", f"a/z.toml\t{H2}", "b/x.py"]
    assert lock.parse(text) == entries


def test_render_header_says_what_it_is():
    text = lock.render({"a.py": None})
    assert text.startswith("#") and "kb-publish approve" in text


@pytest.mark.parametrize("bad", [
    "a.py\tnothex\n",               # hash isn't a 40-hex blob id
    f"a.py\t{H1}\textra\n",         # too many fields
    "a.py\na.py\n",                 # duplicate path
    " a.py\n",                      # leading space: not a path this tool writes
    "\t" + H1 + "\n",               # empty path
])
def test_parse_rejects_malformed(bad):
    with pytest.raises(lock.LockError):
        lock.parse(bad)


@pytest.mark.parametrize("path", ["a\tb.py", "a\nb.py", "a\rb.py", "#notes.md", " lead.py",
                                  "trail.py "])
def test_render_refuses_unrepresentable_path(path):
    # Such a path can never be approved, so it stays "new" and refused forever.
    with pytest.raises(lock.LockError):
        lock.render({path: None})


def test_parse_rejects_non_utf8_bytes():
    with pytest.raises(lock.LockError):
        lock.parse_bytes(b"a.py\n\xff\n")


def test_diff_new_removed_changed_unpacks_as_tuple():
    old = {"keep.py": None, "gone.py": None, "data.jsonl": H1, "same.toml": H2}
    cur = {"keep.py": None, "data.jsonl": H2, "same.toml": H2, "fresh.py": None}
    new, removed, changed = lock.diff(old, cur)
    assert new == ["fresh.py"]
    assert removed == ["gone.py"]
    assert changed == ["data.jsonl"]


def test_diff_unpinned_edit_is_not_a_change():
    # Ordinary source edits go through normal review; the lockfile only
    # tracks which paths exist (and the bytes of pinned data/config).
    d = lock.diff({"a.py": None}, {"a.py": None})
    assert not d.blocking and d == ([], [], [])


def test_diff_newly_needs_pin_is_a_change():
    # a.txt was approved as text; now it isn't strict UTF-8 → needs a pin.
    assert lock.diff({"a.txt": None}, {"a.txt": H1}).changed == ["a.txt"]


def test_diff_pin_dropped_is_a_change():
    # Fail closed: a pinned file whose current form no longer needs a pin is
    # still a content change nobody has looked at.
    assert lock.diff({"a.toml": H1}, {"a.toml": None}).changed == ["a.toml"]


def test_diff_blocking():
    assert lock.diff({}, {"a.py": None}).blocking
    assert lock.diff({"a.toml": H1}, {"a.toml": H2}).blocking
    assert not lock.diff({"a.py": None}, {}).blocking   # removals are reported, not refused


def test_snapshot_hashes_only_pinned(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_bytes(b"a = 1\n")
    (tmp_path / "src" / "b.jsonl").write_bytes(b"{}\n")
    snap = lock.snapshot(tmp_path, ["src/a.py", "src/b.jsonl"])
    assert snap == {"src/a.py": None, "src/b.jsonl": lock.blob_hash(b"{}\n")}


def test_pinned_hook_matches_only_the_recorded_bytes():
    data = "x".encode("utf-16")
    hook = lock.pinned_hook({"odd.txt": lock.blob_hash(data), "plain.py": None})
    assert hook("odd.txt", data)
    assert not hook("odd.txt", data + b"\x00")
    assert not hook("plain.py", b"anything")
    assert not hook("unknown.txt", data)


def test_pinned_hook_none_lock_pins_nothing():
    assert not lock.pinned_hook(None)("a", b"")


def test_lockfile_is_never_exported():
    assert lock.LOCK_REL == "private/publish.paths.lock"
    assert manifest.hard_deny_reason(lock.LOCK_REL) is not None


def _git(root, *args):
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


def test_load_at_ref(tmp_path):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@example.com")
    _git(tmp_path, "config", "user.name", "T")
    (tmp_path / "a.py").write_text("a\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "no lock")
    assert lock.load_at_ref(tmp_path, "HEAD") is None
    (tmp_path / "private").mkdir()
    (tmp_path / lock.LOCK_REL).write_text(lock.render({"a.py": None}))
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "lock")
    assert lock.load_at_ref(tmp_path, "HEAD") == {"a.py": None}
    # The working tree copy is not what load_at_ref reads.
    (tmp_path / lock.LOCK_REL).write_text(lock.render({"a.py": None, "b.py": None}))
    assert lock.load_at_ref(tmp_path, "HEAD") == {"a.py": None}
    assert lock.load(tmp_path) == {"a.py": None, "b.py": None}


def test_load_missing_is_none(tmp_path):
    assert lock.load(tmp_path) is None


@pytest.mark.parametrize("data,expect", [
    (b"line1\nline2\nline3\nline4\nline5\nline6\n", ["line1", "line5"]),
    (b"\x89PNG\r\n\x1a\n\x00\x00", ["binary"]),
    (b"evil \x1b[2J clear\n", ["\\x1b"]),          # terminal escapes neutralized
])
def test_describe_shows_size_and_head(data, expect):
    text = lock.describe("p", data)
    assert f"{len(data)} bytes" in text
    for e in expect:
        assert e in text
    assert "line6" not in text
    assert "\x1b" not in text


def test_snapshot_skips_what_never_exports(tmp_path):
    # The guard snapshots the CHECKOUT: a tracked symlink is stripped by
    # export.assemble and a locally deleted file isn't there to ship, so
    # neither may demand an approval.
    (tmp_path / "a.py").write_bytes(b"a\n")
    (tmp_path / "link.py").symlink_to(tmp_path / "a.py")
    assert lock.snapshot(tmp_path, ["a.py", "link.py", "gone.py"]) == {"a.py": None}


def test_report_lines():
    d = lock.LockDiff(["n.py"], ["r.py"], ["c.toml"])
    lines = lock.report_lines(d, 7)
    assert lines[0] == "lockfile: 1 new, 1 changed, 1 removed (of 7 selected)"
    assert "  + n.py" in lines and any(ln.startswith("  ~ c.toml") for ln in lines)
    assert any(ln.startswith("  - r.py") for ln in lines)
    assert lock.LOCK_REL in lock.report_lines(d, 7, have_lock=False)[0]
