"""`[[allow]]` in publish.deny.toml: a narrow, exact-literal exception.

The public README must carry its own clone URL and a maintainer credit, and
both contain the owner's handle — a denied term. The exception is the
narrowest one that does that job:

- exact, case-sensitive literals (never a regex from config), at most
  MAX_ALLOWS of them, each long enough and with enough non-denied context
  that it can never be widened into "allow the handle";
- boundary-checked (a longer repo name or a glued-on word is not the literal);
- scoped to path globs, per entry; path NAMES never honour it;
- applied by dropping only a deny match that lies ENTIRELY inside an exact
  occurrence of the literal, located in the same text or decoded reading the
  match was found in. A match that straddles the literal's edge (a denied
  term continuing into a glued string literal, entity or %-escape) still
  hits; so does a case/homoglyph/zero-width variant, and every other
  occurrence of the handle on the same line or in the same file.

Every identifier here is invented ("zorb" stands in for a denied handle).
"""
import re
import subprocess
from pathlib import Path

import pytest

from kb_core.publish import check, cli, deny, import_pr, structural

URL = "github.com/ZorbOwner/Ego-OpenSource"
CREDIT = "Maintained by @ZorbOwner"
HANDLE = "ZorbOwner"
ALLOWS_TOML = f"""
[deny]
patterns = ["zorb", "quincy", "sourcery", "kelpmaint"]

[[allow]]
text = "{URL}"
paths = ["README.md", "CONTRIBUTING.md", "docs/*.md", "*/pyproject.toml", "KB_*/README.md"]
reason = "the public repo's own URL"

[[allow]]
text = "{CREDIT}"
paths = ["README.md"]
reason = "maintainer credit"
"""


@pytest.fixture
def deny_file(tmp_path):
    p = tmp_path / "publish.deny.toml"
    p.write_text(ALLOWS_TOML)
    return p


@pytest.fixture
def loaded(deny_file):
    pats = deny.load_patterns(deny_file)
    return pats, deny.load_allows(deny_file, pats)


def _scan(tmp_path, loaded, text, rel="README.md", deny_only=False):
    pats, allows = loaded
    rep = structural.Report()
    structural.scan_blob(rep, rel, text.encode(), pats, allows=allows, deny_only=deny_only)
    return rep.hits


# --- what passes ------------------------------------------------------------------

@pytest.mark.parametrize("line", [
    f"git clone https://{URL}.git ego-os && cd ego-os",
    f"[![CI](https://{URL}/actions/workflows/ci.yml/badge.svg)](https://{URL}/actions)",
    f"Issues: <https://{URL}/issues>.",
    f'uv tool install "kb-agent @ git+https://{URL}.git#subdirectory=KB_Agent"',
    f"see `{URL}`",
    f"{URL}",
    f"{CREDIT}",
    f"{CREDIT}.",
    f"*{CREDIT}*",
    f"- {CREDIT} · [Contributing](CONTRIBUTING.md)",
])
def test_exact_literals_pass_in_readme(tmp_path, loaded, line):
    assert _scan(tmp_path, loaded, line + "\n") == []


@pytest.mark.parametrize("rel", ["CONTRIBUTING.md", "docs/setup/README.md", "docs/x.md",
                                 "KB_Core/pyproject.toml", "KB_Agent/README.md"])
def test_url_passes_in_every_scoped_path(tmp_path, loaded, rel):
    assert _scan(tmp_path, loaded, f'Repository = "https://{URL}"\n', rel=rel) == []


def test_history_mode_honours_the_allow_too(tmp_path, loaded):
    # `kb-publish accept` re-reads every blob in public history deny-only.
    assert _scan(tmp_path, loaded, f"https://{URL}\n", deny_only=True) == []


# --- what still hits --------------------------------------------------------------

def _zw(s):
    i = s.index(HANDLE) + 3
    return s[:i] + "\u200b" + s[i:]


@pytest.mark.parametrize("line", [
    URL.lower(),                                    # case-folded: not the literal
    URL.upper(),
    URL.replace("ZorbOwner", "zorbowner"),
    URL + "-private",                               # a longer repo name
    URL + "X",
    URL + "_2",
    URL + ".private",                               # dotted repo name
    URL + ".gitx",
    "x" + URL,                                      # glued on the left
    "www." + URL,
    "evil/" + URL,                                  # '/' only as part of https://
    "http://" + URL,                                # only https:// and git+https://
    _zw(URL),                                       # zero-width inside the handle
    URL.replace("o", "\u043e", 1),                  # Cyrillic homoglyph
    "".join(chr(ord(c) + 0xFEE0) if c.isalnum() else c for c in URL),  # fullwidth
    URL.replace("Z", "&#90;"),                      # HTML entity
    URL.replace("Z", "%5A"),                        # percent-escaped
    "\u200b" + URL,                                 # invisible neighbour on the left
    URL + "\u200b-private",                         # invisible neighbour on the right
    CREDIT + "x",
    CREDIT.lower(),
    "Maintained by  @ZorbOwner",                    # whitespace variant
    _zw(CREDIT),
])
def test_variants_still_hit(tmp_path, loaded, line):
    assert _scan(tmp_path, loaded, line + "\n"), line


def test_bare_handle_on_the_same_line_as_the_url_hits(tmp_path, loaded):
    hits = _scan(tmp_path, loaded, f"clone https://{URL} -- ping @{HANDLE}\n")
    assert [h.pattern for h in hits] == ["zorb"] and hits[0].line_no == 1


def test_handle_elsewhere_in_readme_hits(tmp_path, loaded):
    text = f"git clone https://{URL}.git\n\nWritten by {HANDLE} on a Sunday.\n{CREDIT}\n"
    hits = _scan(tmp_path, loaded, text)
    assert [(h.pattern, h.line_no) for h in hits] == [("zorb", 3)]


def test_another_denied_term_next_to_the_url_hits(tmp_path, loaded):
    hits = _scan(tmp_path, loaded, f"https://{URL}/blob/main/quincy.md\n")
    assert [h.pattern for h in hits] == ["quincy"]


def test_url_outside_its_scope_hits(tmp_path, loaded):
    for rel in ("KB_Core/src/x.py", "docs.md", "src/README.md.py", "KB_Core/docs/x.md",
                "README.md.bak", "examples/README.md"):
        assert _scan(tmp_path, loaded, f"u = 'https://{URL}'\n", rel=rel), rel


def test_credit_outside_the_root_readme_hits(tmp_path, loaded):
    for rel in ("docs/x.md", "CONTRIBUTING.md", "KB_Core/README.md"):
        assert _scan(tmp_path, loaded, f"{CREDIT}\n", rel=rel), rel


def test_path_names_never_get_the_allow(tmp_path, loaded):
    rel = f"docs/{URL.replace('/', '_')}.md"
    hits = _scan(tmp_path, loaded, "clean\n", rel=rel)
    assert any(h.pattern.startswith("path name:") for h in hits)


def test_base64_payload_glued_to_the_url_is_still_decoded(tmp_path, loaded):
    import base64
    blob = base64.b64encode(b"zorbowner-secret").decode()
    hits = _scan(tmp_path, loaded, f'see "{blob}" https://{URL}\n')
    assert any("base64 reading" in h.pattern for h in hits)


def test_structural_rules_still_see_the_original_line(tmp_path, loaded):
    # Masking is for the deny scan only: an email on the same line still refuses.
    addr = "ann" + "@" + "realmail.co"
    hits = _scan(tmp_path, loaded, f"https://{URL} or mail {addr}\n")
    assert any(h.pattern == "structural: email domain" for h in hits)


# --- config validation: the allow can't be widened --------------------------------

def _load(tmp_path, body, patterns='["zorb"]'):
    p = tmp_path / "d.toml"
    p.write_text(f"[deny]\npatterns = {patterns}\n{body}")
    return deny.load_allows(p, deny.load_patterns(p))


def _entry(text, paths='["README.md"]', reason='"r"', extra=""):
    return f'[[allow]]\ntext = "{text}"\npaths = {paths}\nreason = {reason}\n{extra}'


def test_no_allow_section_is_empty(tmp_path):
    assert _load(tmp_path, "") == ()


@pytest.mark.parametrize("text,why", [
    (HANDLE, "short"),                                   # a deny term alone
    ("@" + HANDLE, "short"),
    ("zorbzorbzorbzorbzorbzorb", "context"),             # all of it is the deny term
    ("Hi @" + HANDLE + " zorbzorb!!", "context"),
    ("an example phrase, no handle", "excuses nothing"),
    ("github.com/ZorbOwner/\u00c9go", "ASCII"),
    ("github.com/ZorbOwner/\u200bEgo-OpenSource", "ASCII"),
    (" github.com/ZorbOwner/Ego-OpenSource", "whitespace"),
    ("github.com/ZorbOwner/Ego-OpenSource ", "whitespace"),
])
def test_unsafe_allow_text_refused(tmp_path, text, why):
    with pytest.raises(deny.DenyConfigError, match=why):
        _load(tmp_path, _entry(text))


@pytest.mark.parametrize("paths", ['["*.md"]', '["**/*.md"]', '["*"]', '["**"]',
                                   '["*README.md"]', '[]', '["/README.md"]',
                                   '["./README.md"]', '["docs/../README.md"]',
                                   '["docs/[a-z]*.md"]', '"README.md"', '[1]',
                                   '["README.md", ""]'])
def test_unsafe_allow_paths_refused(tmp_path, paths):
    with pytest.raises(deny.DenyConfigError, match="paths"):
        _load(tmp_path, _entry(URL, paths=paths))


def test_allow_entries_are_hard_capped(tmp_path):
    body = "".join(_entry(f"{URL}/n{i}") for i in range(deny.MAX_ALLOWS + 1))
    with pytest.raises(deny.DenyConfigError, match="at most"):
        _load(tmp_path, body)


@pytest.mark.parametrize("body,why", [
    (_entry(URL, reason='""'), "reason"),
    (_entry(URL, extra='regex = true\n'), "unknown"),
    ('[[allow]]\npaths = ["README.md"]\nreason = "r"\n', "text"),
    (_entry(URL) + _entry(URL), "duplicate"),
])
def test_malformed_allow_refused(tmp_path, body, why):
    with pytest.raises(deny.DenyConfigError, match=why):
        _load(tmp_path, body)


def test_allow_that_is_not_an_array_of_tables_is_refused(tmp_path):
    p = tmp_path / "d.toml"
    p.write_text('allow = "x"\n[deny]\npatterns = ["zorb"]\n')
    with pytest.raises(deny.DenyConfigError, match="array"):
        deny.load_allows(p, deny.load_patterns(p))


def test_the_two_shipped_shapes_load(loaded):
    _, allows = loaded
    assert [a.text for a in allows] == [URL, CREDIT]


# --- canaries: an allow can never excuse one --------------------------------------

def test_canary_inside_an_allow_literal_is_unmatched(loaded):
    pats, allows = loaded
    assert check.canaries_matched(pats, [HANDLE, "quincy"], allows) == 2
    # A canary equal to an allowed literal is masked away: it counts as unmatched,
    # so strict CI refuses rather than letting the allow hide a canary.
    assert check.canaries_matched(pats, [URL, CREDIT], allows) == 0
    assert check.canaries_matched(pats, [URL, CREDIT], ()) == 2


# --- every content gate honours it ------------------------------------------------

MANIFEST = """
[publish]
remote = "https://github.com/example/pub.git"
[export]
include = ["src/**", "README.md", "docs/**"]
"""


def _git_repo(tmp_path, files):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / "publish.manifest.toml").write_text(MANIFEST)
    (tmp_path / "publish.deny.toml").write_text(ALLOWS_TOML)
    for rel, text in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    return tmp_path


def test_check_publishable_honours_the_allow(tmp_path):
    root = _git_repo(tmp_path, {"README.md": f"git clone https://{URL}.git\n\n{CREDIT}\n",
                                "src/a.py": "a = 1\n"})
    assert check.run_check(root) == []


def test_check_publishable_still_hits_out_of_scope(tmp_path):
    root = _git_repo(tmp_path, {"README.md": f"https://{URL}\n",
                                "docs/x.md": f"{CREDIT}\n",
                                "src/a.py": f"u = 'https://{URL}'\n"})
    assert sorted(h.path for h in check.run_check(root)) == ["docs/x.md", "src/a.py"]


def test_check_publishable_refuses_a_bad_allow(tmp_path, monkeypatch, capsys):
    root = _git_repo(tmp_path, {"src/a.py": "a = 1\n"})
    (root / "publish.deny.toml").write_text(
        '[deny]\npatterns = ["zorb"]\n' + _entry(HANDLE))
    monkeypatch.chdir(root)
    assert check.main([]) == 1
    assert "REFUSED" in capsys.readouterr().out


def test_import_review_honours_the_allow_by_destination_path(loaded):
    pats, allows = loaded

    def change(path, line):
        return import_pr.FileChange(kind="modify", old_path=path, new_path=path,
                                    old_mode="100644", new_mode="100644", binary=False,
                                    meta=(f"diff --git a/{path} b/{path}",), added=(line,))
    m = _manifest_obj()
    ok = import_pr.review_changes([change("README.md", f"see https://{URL}")], m, pats, allows)
    assert ok.hits == []
    bad = import_pr.review_changes([change("src/a.py", f"see https://{URL}")], m, pats, allows)
    assert bad.hits
    mixed = import_pr.review_changes([change("README.md", f"https://{URL} by {HANDLE}")],
                                     m, pats, allows)
    assert mixed.hits


def _manifest_obj():
    from kb_core.publish import manifest
    return manifest.Manifest(include=["README.md", "src/**"], exclude=[], remote="x")


def test_kb_publish_export_honours_the_allow(tmp_path, capsys, monkeypatch):
    from kb_core.publish import lock
    root = tmp_path / "repo"
    root.mkdir()

    def git(*a):
        return subprocess.run(["git", *a], cwd=root, check=True, capture_output=True,
                              text=True).stdout.strip()
    git("init", "-q", "--initial-branch=main")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "T")
    files = {"publish.manifest.toml": MANIFEST.replace(', "docs/**"', ', "publish.manifest.toml"'),
             "publish.deny.toml": ALLOWS_TOML,
             "README.md": f"git clone https://{URL}.git\n{CREDIT}\n",
             "src/a.py": "a = 1\n"}
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)
    git("add", "-A")
    git("commit", "-qm", "init")
    assert cli.approve(root, ref="HEAD", confirm=lambda _p: True) == 0
    git("add", lock.LOCK_REL)
    git("commit", "-qm", "approve")
    monkeypatch.delenv("KB_PUBLISH_REMOTE", raising=False)
    assert cli.export_tree(root, ref="HEAD", out=tmp_path / "out") == 0, capsys.readouterr().out
    assert (tmp_path / "out" / "README.md").read_text().startswith("git clone")

    (root / "README.md").write_text(f"git clone https://{URL}.git\nby {HANDLE}\n")
    git("add", "-A")
    git("commit", "-qm", "leak")
    assert cli.export_tree(root, ref="HEAD", out=tmp_path / "out2") == 1
    assert "denylist/structural" in capsys.readouterr().out


# --- the real config (private checkout only) --------------------------------------

REAL = Path(__file__).resolve().parents[2] / "publish.deny.toml"


def _in_scope_path(a: deny.Allow) -> str:
    return next(g for g in a.paths if not re.search(r"[*?\[]", g))


@pytest.mark.skipif(not REAL.exists(), reason="no publish.deny.toml (public checkout)")
def test_real_allow_entries_stay_narrow():
    """The owner's real [[allow]] entries, mutated at runtime (this file never
    names the real handle): only the exact literal, in scope, passes."""
    pats = deny.load_patterns(REAL)
    allows = deny.load_allows(REAL, pats)
    assert len(allows) <= deny.MAX_ALLOWS
    for a in allows:
        rel = _in_scope_path(a)

        def hits(text, path=rel):
            rep = structural.Report()
            structural.scan_blob(rep, path, (text + "\n").encode(), pats, allows=allows)
            return [h for h in rep.hits if h.path == path]

        assert hits(a.text) == [], a.reason
        assert hits(a.text.lower()), a.reason
        assert hits(a.text + "x"), a.reason
        assert hits(a.text[:5] + "\u200b" + a.text[5:]), a.reason
        assert hits(a.text, path="KB_Core/src/kb_core/x.py"), a.reason
        # Every denied term inside the literal still hits on its own, even
        # right next to the allowed literal on the same line.
        for p in pats:
            for m in p.finditer(a.text):
                assert hits(m.group()), a.reason
                assert hits(f"{a.text} {m.group()}"), a.reason


def test_accept_honours_the_allow_at_head_and_in_history(tmp_path, monkeypatch, capsys):
    from tests.publish_fakes import gated_push, git, scratch_private
    root, remote, gh = scratch_private(tmp_path, monkeypatch)
    assert gated_push(root, gh) == 0
    work = tmp_path / "w"
    git(tmp_path, "clone", "-q", str(remote), str(work))

    def push(text):
        (work / "README.md").write_text(text)
        git(work, "add", "-A")
        git(work, "-c", "user.name=x", "-c", "user.email=x@example.com", "commit", "-qm",
            "sync: readme")
        git(work, "push", "-q", "origin", "main")

    def accept():
        capsys.readouterr()
        rc = cli.accept(root, remote=str(remote))
        return rc, capsys.readouterr().out

    push(f"git clone https://{URL}.git\n")
    push(f"git clone https://{URL}.git ego-os\n\n{CREDIT}\n")   # first one is now history
    (root / "publish.deny.toml").write_text(ALLOWS_TOML)
    rc, out = accept()
    assert rc == 0, out

    push(f"git clone https://{URL}.git\n\nby {HANDLE}\n")
    rc, out = accept()
    assert rc == 1 and "README.md:3" in out


def test_gate_reports_allow_changes_without_the_literal(tmp_path):
    from kb_core.publish import gate
    root = tmp_path

    def commit(deny_text):
        (root / "publish.deny.toml").write_text(deny_text)
        subprocess.run(["git", "add", "-A"], cwd=root, check=True)
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com",
                        "commit", "-qm", "c"], cwd=root, check=True)
        return subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, check=True,
                              capture_output=True, text=True).stdout.strip()
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    base = commit('[deny]\npatterns = ["zorb", "quincy"]\n')
    now = commit(ALLOWS_TOML)
    added = ["added literal", "added literal"]
    assert gate.allow_changes(root, base, now) == added
    assert gate.allow_changes(root, None, now) == added
    assert gate.allow_changes(root, now, now) == []
    lines = cli._gate_lines(gate.Base(sha=base, source="x"), now, [("M", "publish.deny.toml")],
                            [], added)
    shown = "\n".join(lines)
    assert "2 [[allow]] change(s)" in shown and "2 added literal(s)" in shown
    assert HANDLE not in shown

    # Same literals, one entry's scope widened: still reported, still no literal.
    wider = commit(ALLOWS_TOML.replace('paths = ["README.md"]',
                                       'paths = ["README.md", "docs/*.md"]'))
    assert gate.allow_changes(root, now, wider) == ["widened scope"]
    # Narrowing (or rewording the reason) is not a widening.
    assert gate.allow_changes(root, wider, now) == []
    lines = cli._gate_lines(gate.Base(sha=now, source="x"), wider,
                            [("M", "publish.deny.toml")], [], ["widened scope"])
    shown = "\n".join(lines)
    assert "1 [[allow]] change(s)" in shown and "1 widened scope" in shown
    assert HANDLE not in shown


# --- straddling a literal's edge: only a match ENTIRELY inside one is excused ---------
# "sourcery" starts inside the URL literal (its last letters) and "kelpmaint" ends
# inside the credit (its first letters): complete in a decoded reading, broken by
# any approach that blanks the literal before deriving the readings.

@pytest.mark.parametrize("line", [
    f'x = "https://{URL}" "ry"',                    # joined string literals
    f'x = "https://{URL}" + "ry"',
    f"https://{URL}&#114;y",                        # HTML entity
    f"https://{URL}&#x72;y",
    f"https://{URL}%72y",                           # percent-escape
    f'"kelp" "{CREDIT}"',                           # joined on the left
    f'"kelp" + "{CREDIT}."',
])
def test_denied_term_straddling_the_literal_edge_hits(tmp_path, loaded, line):
    pats, _ = loaded
    assert _scan(tmp_path, (pats, ()), line + "\n"), "caught without any allow"
    assert _scan(tmp_path, loaded, line + "\n"), "and still caught with it"


@pytest.mark.parametrize("deny_only", [False, True])
def test_straddle_hits_in_history_mode_too(tmp_path, loaded, deny_only):
    hits = _scan(tmp_path, loaded, f"https://{URL}%72y\n", deny_only=deny_only)
    assert any(h.pattern.startswith("sourcery") for h in hits)


def test_plain_match_straddling_the_edge_hits():
    # Boundary-valid occurrence (a space follows it) and a pattern that runs
    # from inside the literal across that space: not contained, so not excused.
    pats = [re.compile(p, re.IGNORECASE) for p in ("zorb", "opensource zz")]
    allows = (deny.Allow(URL, ("README.md",), "r"),)
    line = f"https://{URL} zz top"
    assert [h.pattern for h in deny.scan_text("README.md", line, pats, allows=allows)] == \
        ["opensource zz"]
    assert deny.scan_text("README.md", f"https://{URL} top", pats, allows=allows) == []


def test_overlapping_match_is_found_past_a_contained_one():
    # The leftmost "zorb" match sits inside the literal; a later, overlapping
    # candidate that spills out must still be found.
    pats = [re.compile(r"zorb\w*", re.IGNORECASE)]
    allows = (deny.Allow("Maintained by @Zorb", ("README.md",), "r"),)
    assert deny.scan_text("README.md", "Maintained by @Zorb zorbish", pats,
                          allows=allows)
    assert deny.scan_text("README.md", "Maintained by @Zorb.", pats, allows=allows) == []


def test_alternation_short_form_inside_cannot_hide_long_form_past_edge():
    # Leftmost-first alternation picks "zorb" (inside the literal) at the same
    # start where "zorbowner" runs past its edge. Unbounded = a decoded reading,
    # where the literal is located without boundary rules.
    pats = [re.compile(r"zorb|zorbowner", re.IGNORECASE)]
    allows = (deny.Allow("Maintained by @Zorb", ("README.md",), "r"),)
    assert deny.scan_text("README.md", "Maintained by @Zorbowner", pats,
                          allows=allows, bounded=False)
    assert deny.scan_text("README.md", "Maintained by @Zorb", pats,
                          allows=allows, bounded=False) == []


# --- URL literal: no dot segments after it ------------------------------------------

@pytest.mark.parametrize("line", [
    f"https://{URL}/../other",                      # another repo under the handle
    f"https://{URL}/..",
    f"https://{URL}/../../someone/x",
    f"https://{URL}/./README.md",
    f"https://{URL}/issues/../../other",            # traversal further along
    f"https://{URL}/%2e%2e/other",                  # percent-encoded dots
    f"https://{URL}/%2E%2E%2Fother",
    f"https://{URL}/%252e%252e/other",              # double-encoded
    f"https://{URL}\\..\\other",                    # backslash separators
    f"https://{URL}.git/../other",
    f"[x](https://{URL}/..)",
    f"https://{URL}..evil",                         # a dotted repo name
    f"https://{URL}./other",
])
def test_url_followed_by_a_dot_segment_hits(tmp_path, loaded, line):
    assert _scan(tmp_path, loaded, line + "\n"), line


@pytest.mark.parametrize("line", [
    f"https://{URL}/issues",
    f"https://{URL}/blob/main/docs/setup/README.md",
    f"https://{URL}/actions/workflows/ci.yml/badge.svg",
    f"git clone https://{URL}.git",
    f"https://{URL}#readme",
    f"https://{URL}/issues?q=is%3Aopen",
    f"See https://{URL}.",
    f"(https://{URL}).",
    f"<https://{URL}/issues>.",
    f"https://{URL}/releases/v0.1.0",
])
def test_url_with_ordinary_paths_passes(tmp_path, loaded, line):
    assert _scan(tmp_path, loaded, line + "\n") == [], line


# --- credit literal: only prose may follow it ----------------------------------------

@pytest.mark.parametrize("tail", ["/x", "@x", "-x", "_x", "9", "x", ".com", ".x", "'s",
                                  "#x", ",x", "!x", ").x", "+x", "%40x"])
def test_credit_followed_by_more_name_hits(tmp_path, loaded, tail):
    assert _scan(tmp_path, loaded, CREDIT + tail + "\n"), tail


@pytest.mark.parametrize("line", [CREDIT, CREDIT + ".", CREDIT + ". Thanks!",
                                  CREDIT + ", since 2026", CREDIT + ";", CREDIT + ":",
                                  CREDIT + "!", f"({CREDIT})", f"({CREDIT}).",
                                  f"*{CREDIT}*", f"{CREDIT}\tand friends"])
def test_credit_followed_by_prose_passes(tmp_path, loaded, line):
    assert _scan(tmp_path, loaded, line + "\n") == [], line


# --- accept: a history blob gets the allow only where EVERY path it had is in scope ----

def _history_repo(tmp_path, commits):
    repo = tmp_path / "pub"
    repo.mkdir()

    def git(*a):
        return subprocess.run(["git", *a], cwd=repo, check=True, capture_output=True,
                              text=True).stdout.strip()
    git("init", "-q", "--initial-branch=main")
    for files in commits:
        for rel, text in files.items():
            p = repo / rel
            if text is None:
                p.unlink()
                continue
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text)
        git("add", "-A")
        git("-c", "user.name=x", "-c", "user.email=x@example.com", "commit", "-qm", "c")
    return repo


def _accept_hits(tmp_path, loaded, commits):
    from kb_core.publish import accept as accept_mod
    pats, allows = loaded
    repo = _history_repo(tmp_path, commits)
    rep, _ = accept_mod.scan(tmp_path, repo, pats, allows=allows)
    return [h for h in rep.hits if not h.path.startswith(("commit", "tag"))]


def test_accept_history_blob_also_at_an_out_of_scope_path_hits(tmp_path, loaded):
    same = f"see https://{URL}\n"
    hits = _accept_hits(tmp_path, loaded, [
        {"README.md": same, "src/leak.py": same},       # one blob, two paths
        {"README.md": "moved on\n", "src/leak.py": None},
    ])
    assert any(h.path.startswith("src/leak.py (history") for h in hits), hits


def test_accept_head_blob_that_was_out_of_scope_in_history_hits(tmp_path, loaded):
    same = f"see https://{URL}\n"
    hits = _accept_hits(tmp_path, loaded, [
        {"src/leak.py": same},
        {"README.md": same, "src/leak.py": None},       # same blob, now only in scope
    ])
    assert any(h.path.startswith("src/leak.py (history") for h in hits), hits


def test_accept_in_scope_history_still_passes(tmp_path, loaded):
    hits = _accept_hits(tmp_path, loaded, [
        {"README.md": f"git clone https://{URL}.git\n"},
        {"README.md": f"git clone https://{URL}.git ego-os\n", "docs/x.md": f"https://{URL}\n"},
    ])
    assert hits == []
