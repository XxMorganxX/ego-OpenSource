"""Path names get the same obfuscation defences as file content.

A file NAMED after a concealed identifier is refused whatever its body. The
plain name scan alone missed a homoglyph, zero-width or fullwidth spelling of
that name, and a path carrying an invisible or bidi character was allowed: the
name a reviewer reads in the lockfile diff is then not the name that ships.
"""
import re

import pytest

from kb_core.publish import structural

PATTERNS = [re.compile("zorblax", re.IGNORECASE)]


def _scan(rel, *, deny_only=False):
    rep = structural.Report()
    structural.scan_blob(rep, rel, b"x = 1\n", PATTERNS, deny_only=deny_only)
    return [h.pattern for h in rep.hits]


def test_plain_name_hit():
    assert _scan("KB_Core/docs/zorblax-notes.md") == ["path name: zorblax"]


@pytest.mark.parametrize("rel", [
    "KB_Core/docs/zorbl\u0430x-notes.md",        # Cyrillic a
    "KB_Core/docs/\uff5aorblax.md",               # fullwidth z
])
def test_folded_name_hit(rel):
    assert "path name: zorblax [folded reading]" in _scan(rel)
    assert "path name: zorblax [folded reading]" in _scan(rel, deny_only=True)


@pytest.mark.parametrize("ch", ["\u200b", "\u202e", "\u2066", "\ufeff", "\u00ad", "\t",
                                "\n", "\x7f", "\x85"])
def test_invisible_bidi_or_control_in_a_path_is_refused(ch):
    hits = _scan(f"src/ok{ch}name.py")
    assert "structural: invisible/bidi/control character in path name" in hits


def test_zero_width_inside_a_denylisted_name_hits_both_ways():
    hits = _scan("src/zorb\u200blax.md")
    assert "path name: zorblax [folded reading]" in hits
    assert "structural: invisible/bidi/control character in path name" in hits


def test_ordinary_paths_are_clean():
    for rel in ("KB_Core/src/kb_core/publish/structural.py", "docs/caf\u00e9.md",
                ".github/workflows/ci.yml"):
        assert _scan(rel) == []


def test_history_scan_reports_names_not_structure():
    """deny_only (accept over public history): identifiers only."""
    assert _scan("src/ok\u200bname.py", deny_only=True) == []
