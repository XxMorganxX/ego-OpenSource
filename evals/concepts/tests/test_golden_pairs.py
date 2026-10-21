import json
from pathlib import Path

from evals.concepts.golden_pairs import authored_pairs, usable_pairs


def _write(tmp_path: Path, items: list[dict]) -> Path:
    p = tmp_path / "golden.jsonl"
    p.write_text("\n".join(json.dumps(i) for i in items) + "\n")
    return p


def _item(iid: str, must: list[str], may: list[str] = ()) -> dict:
    mems = [{"id": m, "must": True} for m in must]
    mems += [{"id": m, "must": False} for m in may]
    return {"id": iid, "expected": {"memories": mems}}


def test_must_set_becomes_all_its_pairs(tmp_path):
    """A must-set of 3 asserts 3 pairs; ids are ordered within the pair."""
    p = _write(tmp_path, [_item("g1", ["c", "a", "b"])])
    assert authored_pairs(p) == {("a", "b"), ("a", "c"), ("b", "c")}


def test_pairs_are_deduplicated_across_items(tmp_path):
    """The same pair asserted by two items counts once."""
    p = _write(tmp_path, [_item("g1", ["a", "b"]), _item("g2", ["a", "b"])])
    assert authored_pairs(p) == {("a", "b")}


def test_may_memories_are_not_pairs(tmp_path):
    """Only `must` is a belongs-together assertion; `may` is not."""
    p = _write(tmp_path, [_item("g1", ["a"], may=["b", "c"])])
    assert authored_pairs(p) == set()


def test_single_must_memory_yields_no_pair(tmp_path):
    p = _write(tmp_path, [_item("g1", ["a"])])
    assert authored_pairs(p) == set()


def test_blank_lines_are_skipped(tmp_path):
    p = tmp_path / "golden.jsonl"
    p.write_text(json.dumps(_item("g1", ["a", "b"])) + "\n\n")
    assert authored_pairs(p) == {("a", "b")}


def test_usable_pairs_drops_pairs_touching_a_dead_memory(tmp_path):
    """Golden-set rot: referenced ids (must-set union negatives) can be dead in a
    snapshot."""
    pairs = {("a", "b"), ("b", "c")}
    assert usable_pairs(pairs, {"a", "b"}) == {("a", "b")}
