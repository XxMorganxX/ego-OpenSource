"""evals/extraction/capture.py never writes live-DB notes into exported paths.

capture.py freezes real memories (title, content, path) into gold/sample JSONL.
Its --out used to default to its own directory — evals/extraction/, which the
manifest exports — so one bare run put corpus text one `git add` away from the
public mirror. Now the default is $KB_EVAL_DATA_DIR/extraction, and any path
inside the repo but outside private/ is refused unless --force.
"""
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CAPTURE = ROOT / "evals" / "extraction" / "capture.py"


@pytest.fixture(scope="module")
def capture():
    spec = importlib.util.spec_from_file_location("eval_extraction_capture", CAPTURE)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def no_db(capture, monkeypatch):
    """Any attempt to open the database fails the test: refusals come first."""
    def boom(*a, **kw):
        raise AssertionError("capture.py connected to the database before refusing")
    monkeypatch.setattr(capture, "ConnectionPool", boom)


def test_default_is_the_eval_data_dir(capture, tmp_path):
    out = capture.resolve_out(None, env={"KB_EVAL_DATA_DIR": str(tmp_path)})
    assert out == (tmp_path / "extraction").resolve()


def test_no_default_without_the_eval_data_dir(capture):
    with pytest.raises(capture.OutRefused) as e:
        capture.resolve_out(None, env={})
    assert "KB_EVAL_DATA_DIR" in str(e.value)


@pytest.mark.parametrize("rel", ["evals/extraction", "evals", ".", "KB_Core/tests/fixtures"])
def test_in_repo_paths_outside_private_are_refused(capture, rel):
    with pytest.raises(capture.OutRefused) as e:
        capture.resolve_out(str(ROOT / rel), env={})
    assert "private/" in str(e.value) and "--force" in str(e.value)


def test_eval_data_dir_inside_the_exported_tree_is_refused_too(capture):
    with pytest.raises(capture.OutRefused):
        capture.resolve_out(None, env={"KB_EVAL_DATA_DIR": str(ROOT / "evals")})


def test_dotdot_cannot_escape_the_check(capture):
    with pytest.raises(capture.OutRefused):
        capture.resolve_out(str(ROOT / "private" / ".." / "evals" / "extraction"), env={})


def test_private_is_allowed(capture):
    out = capture.resolve_out(str(ROOT / "private" / "evals" / "extraction"), env={})
    assert out == (ROOT / "private" / "evals" / "extraction").resolve()


def test_outside_the_repo_is_allowed(capture, tmp_path):
    assert capture.resolve_out(str(tmp_path / "x"), env={}) == (tmp_path / "x").resolve()


def test_force_allows_an_in_repo_path(capture):
    out = capture.resolve_out(str(ROOT / "evals" / "extraction"), env={}, force=True)
    assert out == (ROOT / "evals" / "extraction").resolve()


def test_main_refuses_before_touching_the_database(capture, no_db, monkeypatch, capsys):
    monkeypatch.delenv("KB_EVAL_DATA_DIR", raising=False)
    assert capture.main([]) == 2
    assert "REFUSED" in capsys.readouterr().err


def test_main_refuses_the_old_default(capture, no_db, monkeypatch, capsys):
    monkeypatch.delenv("KB_EVAL_DATA_DIR", raising=False)
    assert capture.main(["--out", str(CAPTURE.parent)]) == 2
    assert "REFUSED" in capsys.readouterr().err
