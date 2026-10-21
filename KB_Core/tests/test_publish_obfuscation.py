"""Obfuscated identifiers are deny-scanned in their decoded readings.

The denylist matched only the literal UTF-8 text, so base64, HTML entities, %XX escapes, zero-width
characters, homoglyphs, split string literals and UTF-7 all passed once a path
was approved — and passed with NO signal when appended to a file approved
long ago (the lockfile only gates NEW paths). Each file is now also
deny-scanned as: NFKC + homoglyph-folded + invisible-stripped text, entity-
and %-unescaped text, joined string-literal fragments, UTF-7, and every
base64 run decoded. Invisible and bidi control characters are refused
outright (none occur in the export). Every identifier below is assembled at
runtime from escapes, so this file never trips the scan it tests.
"""
import base64
import gzip
import re

import pytest

from kb_core.publish import structural

PATS = [re.compile("secretname", re.IGNORECASE), re.compile("quincy halloway", re.IGNORECASE)]
NAME = "secret" + "name"


def _scan(tmp_path, text, rel="src/app.py"):
    p = tmp_path / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return structural.scan_files(tmp_path, [rel], PATS)


def _hit(rep):
    return [h.pattern for h in rep.hits]


def _homoglyph(word):
    # Cyrillic look-alikes: e -> U+0435, a -> U+0430
    return word.replace("e", "\u0435").replace("a", "\u0430")


CASES = {
    "base64": f'BLOB = "{base64.b64encode(NAME.encode()).decode()}"\n',
    "base64-urlsafe": f"t = '{base64.urlsafe_b64encode((NAME + '??>>').encode()).decode()}'\n",
    "base64-gzip": f'Z = "{base64.b64encode(gzip.compress(NAME.encode() * 3)).decode()}"\n',
    "html-entities": "<p>" + "".join(f"&#{ord(c)};" for c in NAME) + "</p>\n",
    "html-hex-entities": "<p>" + "".join(f"&#x{ord(c):x};" for c in NAME) + "</p>\n",
    "percent": "u = '" + "".join(f"%{ord(c):02X}" for c in NAME) + "'\n",
    "homoglyph": f"who = '{_homoglyph(NAME)}'\n",
    "fullwidth": "who = '" + "".join(chr(ord(c) + 0xFEE0) for c in NAME) + "'\n",
    "split-plus": f'who = "{NAME[:4]}" + "{NAME[4:]}"\n',
    "split-implicit": f'who = ("{NAME[:3]}"\n       "{NAME[3:]}")\n',
    "split-with-space": 'who = "Quincy " + "Halloway"\n',
    "utf7": "x = '+AHMAZQBjAHIAZQB0AG4AYQBtAGU-'\n",      # every char shifted
}


@pytest.mark.parametrize("case", sorted(CASES))
def test_obfuscated_identifier_is_caught(tmp_path, case):
    rep = _scan(tmp_path, CASES[case])
    assert rep.hits, case


@pytest.mark.parametrize("ch", ["\u200b", "\u200d", "\u2060", "\ufeff", "\u202e", "\u2066",
                                "\u00ad"])
def test_invisible_and_bidi_characters_refused(tmp_path, ch):
    rep = _scan(tmp_path, f"x = 'ab{ch}cd'\n")
    assert any("invisible" in p for p in _hit(rep)), _hit(rep)


def test_zero_width_split_name_is_caught_as_a_name_too(tmp_path):
    rep = _scan(tmp_path, f"who = '{NAME[:3]}\u200b{NAME[3:]}'\n")
    assert any(p.startswith("secretname") for p in _hit(rep)), _hit(rep)


def test_appended_to_an_approved_file_is_caught(tmp_path):
    # Path-only lockfile entries never re-review content: the scan must.
    body = "def f():\n    return 1\n" * 50
    rep = _scan(tmp_path, body + CASES["base64"])
    assert rep.hits


@pytest.mark.parametrize("text", [
    "import base64\nTOKEN_RE = r'[A-Za-z0-9+/]{16,}'\nlong_identifier_without_meaning = 1\n",
    "a = b + c\nq = '%20'\n<p>&amp; &lt;</p>\n",
    "delta = '\u0394L'\nlabel = 'naïve café'\n",
    "x = 'ab' + 'cd'\ny = 'abcdefghijklmnopqrstuvwx'\n",
])
def test_ordinary_text_is_clean(tmp_path, text):
    assert _scan(tmp_path, text).hits == []


def test_decoded_hit_is_labelled_with_its_reading(tmp_path):
    rep = _scan(tmp_path, CASES["base64"])
    assert any("base64" in p for p in _hit(rep)), _hit(rep)


@pytest.mark.parametrize("glue,tail", [("x", ""), ("xy", ""), ("xyz", ""), ("x", "Q"),
                                       ("Ab", "Qz"), ("", "Q")])
def test_base64_with_glued_characters_is_still_decoded(tmp_path, glue, tail):
    # One stray alphabet character before the run shifts every 4-char group;
    # one after it makes the length invalid. Both used to decode as nothing.
    blob = base64.b64encode(("id:" + NAME).encode()).decode()
    rep = _scan(tmp_path, f'BLOB = "{glue}{blob}{tail}"\n')
    assert any("base64 reading" in p for p in _hit(rep)), (glue, tail, _hit(rep))
