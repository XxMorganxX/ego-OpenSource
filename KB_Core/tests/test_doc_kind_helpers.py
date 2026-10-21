"""Pure form-guard tests for the doc_kind facet — no DB, no network."""
from kb_core.doc_kind import MAX_DOC_KIND_LEN, normalize_doc_kind


def test_normalizes_case_and_whitespace():
    assert normalize_doc_kind("  Meeting   Notes ") == "meeting notes"


def test_empty_and_non_alnum_rejected():
    assert normalize_doc_kind(None) is None
    assert normalize_doc_kind("") is None
    assert normalize_doc_kind("   ") is None
    assert normalize_doc_kind("---") is None


def test_placeholder_meta_words_rejected():
    # Mirrors the instance placeholder guard: "no signal" replies persist nothing.
    assert normalize_doc_kind("unknown") is None
    assert normalize_doc_kind("None") is None
    assert normalize_doc_kind("N/A") is None


def test_overlong_is_a_runaway_generation():
    assert normalize_doc_kind("a" * (MAX_DOC_KIND_LEN + 1)) is None
    assert normalize_doc_kind("meeting notes") == "meeting notes"


def test_repetition_spiral_rejected():
    # >= 20 chars with <= 5 distinct characters — clean_class_description's signature.
    assert normalize_doc_kind("ababababababababababab") is None


def test_weird_but_genuine_kind_survives():
    # Open vocabulary: form guards must never reject a real coinage.
    assert normalize_doc_kind("Troubleshooting Log") == "troubleshooting log"
    assert normalize_doc_kind("1:1 meeting notes") == "1:1 meeting notes"
