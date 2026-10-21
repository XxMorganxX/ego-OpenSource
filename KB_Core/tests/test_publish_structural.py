"""Structural detectors: what a denylist can't express.

The denylist only knows identifiers someone already thought to list. These
rules refuse the SHAPES of private data — a real email domain, a home
directory, a routable IP — and the file forms the text scan can't read at all.
"""
import re

import pytest

from kb_core.publish import manifest, structural

PATS = [re.compile("secretname", re.IGNORECASE)]


def _e(local, domain):
    # Assembled at runtime: this file is exported and scanned too, so a literal
    # real-looking address here would trip the very detector it tests.
    return local + "@" + domain


def _scan(tmp_path, files: dict[str, bytes | str], **kw):
    for rel, data in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(data, str):
            data = data.encode()
        p.write_bytes(data)
    return structural.scan_files(tmp_path, sorted(files), PATS, **kw)


def _rules(report):
    return sorted({(h.path, h.pattern) for h in report.hits})


# --- path names -----------------------------------------------------------

def test_path_name_is_deny_scanned(tmp_path):
    rep = _scan(tmp_path, {"src/secretname_notes.md": "nothing here\n"})
    assert [(h.path, h.line_no) for h in rep.hits] == [("src/secretname_notes.md", 0)]
    assert "path name" in rep.hits[0].pattern


# --- encoding ------------------------------------------------------------

@pytest.mark.parametrize("data,rule", [
    ("secret\n".encode("utf-16"), "BOM"),
    (b"\xef\xbb\xbfplain\n", "BOM"),
    (b"a\x00b\n", "NUL"),
    (b"caf\xe9\n", "UTF-8"),
])
def test_unreadable_text_refused(tmp_path, data, rule):
    rep = _scan(tmp_path, {"src/x.txt": data})
    assert len(rep.hits) >= 1
    assert any(rule in h.pattern for h in rep.hits)


def test_utf16_identifier_still_refused(tmp_path):
    # errors='replace' line scanning missed UTF-16 entirely: the identifier is
    # interleaved with NULs. Refusing the encoding is what closes that hole.
    rep = _scan(tmp_path, {"src/x.txt": "secretname\n".encode("utf-16-le")})
    assert rep.hits


def test_pinned_hook_exempts_encoding_only(tmp_path):
    seen = []

    def pinned(rel, data):
        seen.append(rel)
        return True

    rep = _scan(tmp_path, {"src/fixture.bin.txt": b"\x00\x01\x02"}, is_pinned=pinned,
                allow_binary=("src/fixture.bin.txt",))
    assert rep.hits == []
    assert seen == ["src/fixture.bin.txt"]


def test_valid_utf8_passes(tmp_path):
    rep = _scan(tmp_path, {"src/ok.md": "naïve café — ok\n"})
    assert rep.hits == []


# --- suffixes ------------------------------------------------------------

@pytest.mark.parametrize("name", [
    "a.zip", "a.tar.gz", "a.tgz", "a.gz", "a.7z", "a.jar", "a.whl", "a.bz2", "a.xz",
    "nb.ipynb", "doc.docx", "sheet.xlsx", "deck.pptx", "old.doc", "a.odt", "a.rtf",
    "kb.sqlite", "kb.sqlite3", "kb.db", "a.pkl", "a.parquet", "A.ZIP",
])
def test_forbidden_suffixes(name):
    assert manifest.forbidden_suffix(f"src/{name}")


def test_forbidden_suffix_refused_even_if_allow_media(tmp_path):
    rep = _scan(tmp_path, {"src/bundle.zip": b"PK\x03\x04"})
    assert any("archive" in h.pattern for h in rep.hits)


def test_media_is_not_encoding_checked(tmp_path):
    # Media is gated by allow_media, not by the UTF-8 rule (a PNG is binary).
    rep = _scan(tmp_path, {"src/logo.png": b"\x89PNG\r\n\x1a\n\x00"})
    assert rep.hits == []


# --- emails ---------------------------------------------------------------

@pytest.mark.parametrize("line", [
    "alice@example.com", "bob@Example.org", "x@git.example.com", "me@work.example",
    "a@host.invalid", "a@x.test", "1234+bot@users.noreply.github.com",
    "git@github.com:owner/repo.git",              # ssh remote, not an email
    "https://tok3n@github.com/owner/repo",        # url userinfo, not an email
    "deploy:tok3n@github.com:Org/repo.git",       # scp-style remote, not an email
    "postgresql://kb:kb@localhost:5433/db",
    "npm i react@18.3.1",
])
def test_placeholder_emails_allowed(tmp_path, line):
    assert _scan(tmp_path, {"src/a.md": line + "\n"}).hits == []


@pytest.mark.parametrize("line", [
    "contact " + _e("jane.doe", "gmail.com"), _e("me", "corp.edu"), _e("a", "x.com"),
    "mailto:" + _e("ops", "realco.io"), _e("alice", "example.com.evil.net"),
])
def test_real_looking_emails_refused(tmp_path, line):
    rep = _scan(tmp_path, {"src/a.md": line + "\n"})
    assert [h.pattern for h in rep.hits] == ["structural: email domain"]


# --- home paths -----------------------------------------------------------

@pytest.mark.parametrize("line", [
    "/Users/you/vault", "/Users/me/x", "/Users/example/x", "/Users/<name>/notes",
    "/Users/$USER/x", "/Users/a/x", "see ~/Desktop or ~/Documents (TCC)",
    "/Users/Shared/x",
])
def test_placeholder_home_paths_allowed(tmp_path, line):
    assert _scan(tmp_path, {"src/a.md": line + "\n"}).hits == []


@pytest.mark.parametrize("line", [
    "/Users/" + "janedoe/notes", "cd ~/Desk" + "top/Proj/x", "$HOME/Desk" + "top/stuff",
])
def test_real_home_paths_refused(tmp_path, line):
    rep = _scan(tmp_path, {"src/a.md": line + "\n"})
    assert [h.pattern for h in rep.hits] == ["structural: home path"]


# --- IPv4 -----------------------------------------------------------------

@pytest.mark.parametrize("line", [
    "127.0.0.1", "0.0.0.0", "192.0.2.10", "198.51.100.7", "203.0.113.255",
    "10.1.2.3", "172.17.0.1", "192.168.1.20", "version 1.2.3", "v1.2.3.4.5",
    "255.255.255.0",
])
def test_allowed_ips(tmp_path, line):
    assert _scan(tmp_path, {"src/a.md": line + "\n"}).hits == []


@pytest.mark.parametrize("line", ["host 8.8." + "8.8", "ssh 93.184." + "216.34",
                                  "169.254." + "1.1"])
def test_routable_ips_refused(tmp_path, line):
    rep = _scan(tmp_path, {"src/a.md": line + "\n"})
    assert [h.pattern for h in rep.hits] == ["structural: IPv4 address"]


# --- phone numbers --------------------------------------------------------

def test_phone_refused_and_fictional_allowed(tmp_path):
    assert _scan(tmp_path, {"src/a.md": "call 212-555-0123\n"}).hits == []
    rep = _scan(tmp_path, {"src/b.md": "call (555) 234-" + "5678\n"})
    assert [h.pattern for h in rep.hits] == ["structural: phone number"]


# --- TEMP/revert markers in exported TOML ---------------------------------

def test_toml_temp_marker_refused(tmp_path):
    rep = _scan(tmp_path, {"KB_Core/inference.toml":
                           '[x]\nmodel = "haiku"  # TEMP: backlog drain\n'})
    assert [h.pattern for h in rep.hits] == ["structural: TEMP/revert marker in TOML"]
    rep = _scan(tmp_path, {"a/b.toml": "# Revert to local when pending≈0\n"})
    assert rep.hits


def test_temp_marker_outside_toml_is_fine(tmp_path):
    assert _scan(tmp_path, {"src/a.py": "# TEMP: fine in code\n"}).hits == []


def test_toml_temperature_is_not_a_marker(tmp_path):
    assert _scan(tmp_path, {"a.toml": "temperature = 0.2\ntemp_dir = 'x'\n"}).hits == []


# --- warn-level lint ------------------------------------------------------

def test_lint_warns_but_never_blocks(tmp_path):
    rep = _scan(tmp_path, {"src/a.py": "tz = 'Americ" + "a/Chicago'\n# measured " + "2026 "
                           "on the live" + " corpus\n"})
    assert rep.hits == []
    assert len(rep.warnings) == 3
    assert {w.pattern for w in rep.warnings} == {
        "lint: tz-name", "lint: live-corpus", "lint: measured-year"}


# --- the deny content scan still runs ------------------------------------

def test_deny_content_hit(tmp_path):
    rep = _scan(tmp_path, {"src/a.py": "x = 1\n# secretname\n"})
    assert [(h.path, h.line_no, h.pattern) for h in rep.hits] == [("src/a.py", 2, "secretname")]


@pytest.mark.parametrize("line", ["# temp override, drop later", "# Temporary routing for the drain",
                                  "x = 1  # temp: until pending=0"])
def test_lowercase_temp_markers_in_toml_refused(tmp_path, line):
    # Only upper-case TEMP used to be caught.
    rep = _scan(tmp_path, {"KB_Core/inference.toml": f"[a]\n{line}\nb = 2\n"})
    assert any("TEMP/revert" in h.pattern for h in rep.hits)


def test_ordinary_toml_words_are_not_markers(tmp_path):
    rep = _scan(tmp_path, {"KB_Core/x.toml": "temperature = 0.2\n# route it temporarily if needed\n"})
    assert not any("TEMP/revert" in h.pattern for h in rep.hits)
