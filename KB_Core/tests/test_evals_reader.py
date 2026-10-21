"""evals_reader: classify result files by shape, aggregate for display."""

import json

import pytest
from fastapi.testclient import TestClient

from dashboard.api.app import create_app
from dashboard.api.evals_reader import read_evals


@pytest.fixture
def client(kb):
    return TestClient(create_app(kb))


def _item(recall, neg, archetype, latency):
    return {"id": "x", "archetype": archetype, "must_recall": recall,
            "neg_hits": neg, "calls": [{"latency_ms": latency}]}


def _cell_file(tmp, name, repeat, items):
    tmp.joinpath(name).write_text(json.dumps({
        "cell": "cell1", "repeat": repeat,
        "stamp": {"ran_at": f"20250315T12000{repeat}Z",
                  "git_head": "0123456789abcdef0123456789abcdef01234567",
                  "cell": {"set": "flat", "strategy": "hybrid",
                           "judge": None, "repeats": 2}},
        "items": items}))


def _seed(tmp):
    tmp.joinpath("20250315T120000Z.json").write_text(json.dumps({
        "ran_at": "20250315T120000Z", "k": 20, "overall_must_recall": 0.8,
        "mrr": 0.5, "items_scored": 36, "items_total": 40,
        "by_archetype": {"meetings": {"n": 10, "scored": 10,
                                      "must_recall": 0.9, "mrr": 0.7}}}))
    _cell_file(tmp, "cell1-r1.json", 1,
               [_item(1.0, 1, "meetings", 100), _item(0.5, 0, "facet", 200),
                _item(None, 0, "goals", 150)])          # unscored: excluded from recall
    _cell_file(tmp, "cell1-r2.json", 2,
               [_item(1.0, 1, "meetings", 100), _item(1.0, 0, "facet", 200),
                _item(None, 0, "goals", 150)])
    tmp.joinpath("off_corpus.json").write_text(json.dumps({
        "verdict": "FAIL", "confidence_weak": 0.5,
        "worst_mode_b_best_similarity": 0.61,
        "probes": [{"probe": "junk", "mode_a": {"level": "weak",
                                                "best_similarity": 0.55, "n": 20},
                    "mode_b": {"level": "weak", "best_similarity": 0.55, "n": 20}}]}))
    tmp.joinpath("ablations.json").write_text(json.dumps({
        "sweeps": [{"name": "flat_reserve=0", "set": "flat",
                    "kw": {"flat_reserve": 0}, "must_recall": 0.7,
                    "n_scored": 36}],
        "entry_families": {}}))


def test_reference_runs_parsed_and_sorted(tmp_path):
    _seed(tmp_path)
    out = read_evals(tmp_path)
    assert out["warnings"] == []
    runs = out["reference_runs"]
    assert len(runs) == 1
    assert runs[0]["overall_must_recall"] == 0.8
    assert runs[0]["by_archetype"]["meetings"]["must_recall"] == 0.9


def test_grid_aggregates_repeats(tmp_path):
    _seed(tmp_path)
    grids = read_evals(tmp_path)["grids"]
    assert len(grids) == 1
    g = grids[0]
    assert g["date"] == "2025-03-15"
    assert g["git_heads"] == ["0123456"]
    [cell] = g["cells"]
    assert cell["cell"] == "cell1"
    assert cell["set"] == "flat" and cell["strategy"] == "hybrid"
    assert cell["repeats"] == 2
    # repeat recalls: mean(1.0, 0.5)=0.75 and mean(1.0, 1.0)=1.0 -> 0.875
    assert cell["must_recall"] == 0.875
    assert cell["neg_hits"] == 1.0           # 1 per repeat, averaged
    # latencies pooled over calls in both repeats: 100,200,150,100,200,150
    assert cell["latency_p50_ms"] == 150
    assert cell["by_archetype"] == {"meetings": 1.0, "facet": 0.75}


def test_off_corpus_and_ablations_pass_through(tmp_path):
    _seed(tmp_path)
    out = read_evals(tmp_path)
    assert out["off_corpus"]["verdict"] == "FAIL"
    assert out["off_corpus"]["threshold"] == 0.5
    assert out["off_corpus"]["worst_best_similarity"] == 0.61
    assert len(out["off_corpus"]["probes"]) == 1
    assert out["ablations"]["sweeps"][0]["name"] == "flat_reserve=0"


def test_malformed_file_is_skipped_with_warning(tmp_path):
    _seed(tmp_path)
    tmp_path.joinpath("broken.json").write_text("{not json")
    tmp_path.joinpath("stranger.json").write_text(json.dumps({"who": "knows"}))
    out = read_evals(tmp_path)
    assert len(out["reference_runs"]) == 1          # good files still parse
    assert any("broken.json" in w for w in out["warnings"])
    assert any("stranger.json" in w for w in out["warnings"])


def test_missing_dir_warns_instead_of_raising(tmp_path):
    out = read_evals(tmp_path / "nope")
    assert out["reference_runs"] == [] and out["grids"] == []
    assert len(out["warnings"]) == 1


def test_endpoint_serves_reader_output(client, kb, tmp_path, monkeypatch):
    _seed(tmp_path)
    monkeypatch.setenv("KB_EVALS_RESULTS_DIR", str(tmp_path))
    body = client.get("/api/retrieval/evals").json()
    assert body["reference_runs"][0]["mrr"] == 0.5
    assert body["grids"][0]["cells"][0]["must_recall"] == 0.875


def test_reference_file_without_ran_at_does_not_crash_the_sort(tmp_path):
    _seed(tmp_path)
    tmp_path.joinpath("no-ran-at.json").write_text(json.dumps({
        "overall_must_recall": 0.5, "mrr": 0.3,
        "items_scored": 1, "items_total": 1, "by_archetype": {}}))
    out = read_evals(tmp_path)
    assert len(out["reference_runs"]) == 2
    assert out["reference_runs"][0]["ran_at"] is None   # sorts first via ""
    assert out["reference_runs"][1]["ran_at"] == "20250315T120000Z"


def test_cell_file_with_null_ran_at_does_not_crash(tmp_path):
    # corruption 1: stamp.ran_at is null -> TypeError in _date (len of None)
    # before the group-key was made defensive.
    _seed(tmp_path)
    tmp_path.joinpath("null-ran-at.json").write_text(json.dumps({
        "cell": "c1", "stamp": {"ran_at": None}}))
    out = read_evals(tmp_path)
    # good data untouched
    assert len(out["reference_runs"]) == 1
    good_grid = next(g for g in out["grids"] if g["date"] == "2025-03-15")
    assert good_grid["cells"][0]["cell"] == "cell1"


def test_cell_file_with_null_cell_meta_does_not_crash(tmp_path):
    # corruption 2: stamp.cell is null -> AttributeError (meta.get on None)
    # in the per-cell aggregation body; caught and warned, other cells survive.
    _seed(tmp_path)
    tmp_path.joinpath("null-cell-meta.json").write_text(json.dumps({
        "cell": "c1", "stamp": {"ran_at": "20250315T120000Z", "cell": None}}))
    out = read_evals(tmp_path)
    assert len(out["reference_runs"]) == 1
    good_grid = next(g for g in out["grids"] if g["date"] == "2025-03-15")
    assert any(c["cell"] == "cell1" for c in good_grid["cells"])
    assert any("grid 2025-03-15/c1" in w for w in out["warnings"])


def test_duplicate_day_null_cell_name_is_rejected_not_crashed(tmp_path):
    # corruption 3: a second same-day cell file has "cell": null -> used to
    # blow up sorted(by_day[day]) comparing None to str; now rejected upstream
    # by the "cell must be a str" invariant before it ever reaches _grids.
    _seed(tmp_path)
    tmp_path.joinpath("null-cell-name.json").write_text(json.dumps({
        "cell": None, "stamp": {"ran_at": "20250315T120000Z"}}))
    out = read_evals(tmp_path)
    assert len(out["reference_runs"]) == 1
    good_grid = next(g for g in out["grids"] if g["date"] == "2025-03-15")
    assert any(c["cell"] == "cell1" for c in good_grid["cells"])
    assert any("null-cell-name.json" in w and "malformed cell file" in w
               for w in out["warnings"])


def test_cell_item_with_string_must_recall_does_not_crash_mean(tmp_path):
    # corruption 4: a cell item's must_recall is a string -> TypeError in
    # mean(); caught and warned, other cells survive.
    _seed(tmp_path)
    tmp_path.joinpath("cell2-r1.json").write_text(json.dumps({
        "cell": "cell2", "repeat": 1,
        "stamp": {"ran_at": "20250315T120000Z",
                  "cell": {"set": "flat", "strategy": "hybrid",
                           "judge": None, "repeats": 1}},
        "items": [{"id": "y", "archetype": "meetings", "must_recall": "0.5",
                   "neg_hits": 0, "calls": []}]}))
    out = read_evals(tmp_path)
    assert len(out["reference_runs"]) == 1
    good_grid = next(g for g in out["grids"] if g["date"] == "2025-03-15")
    assert any(c["cell"] == "cell1" for c in good_grid["cells"])
    assert any("grid 2025-03-15/cell2" in w for w in out["warnings"])


def test_endpoint_returns_200_with_warning_for_half_shaped_cell_file(
        client, kb, tmp_path, monkeypatch):
    _seed(tmp_path)
    tmp_path.joinpath("null-cell-meta.json").write_text(json.dumps({
        "cell": "c1", "stamp": {"ran_at": "20250315T120000Z", "cell": None}}))
    monkeypatch.setenv("KB_EVALS_RESULTS_DIR", str(tmp_path))
    resp = client.get("/api/retrieval/evals")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["warnings"]) >= 1
    assert body["reference_runs"][0]["mrr"] == 0.5
