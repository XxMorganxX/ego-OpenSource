"""kb_core.prompts — overlay shadowing, includes, and the taxonomy marker. Pure (no DB)."""
import pytest

from kb_core.policy import Category, Policy, PolicyError, Prompts, Taxonomy
from kb_core.prompts import PromptResolver, prompts_base_dir


@pytest.fixture
def dirs(tmp_path):
    base, ov = tmp_path / "prompts", tmp_path / "ov"
    base.mkdir(), ov.mkdir()
    (base / "a.system.md").write_text("HEAD\n{{include:a.examples.md}}\nTAIL\n")
    (base / "a.examples.md").write_text("shipped examples")
    (base / "b.system.md").write_text("only shipped")
    (base / "t.system.md").write_text("before\n\n{{taxonomy}}\n\nafter\n")
    return base, ov


def _res(dirs, overlay=True, taxonomy=None):
    base, ov = dirs
    pol = Policy(prompts=Prompts(str(ov) if overlay else None),
                 taxonomy=taxonomy if taxonomy is not None else Taxonomy())
    return PromptResolver(pol, base)


def test_shipped_file_with_shipped_partial(dirs):
    assert _res(dirs, overlay=False).load("a.system.md") == "HEAD\nshipped examples\nTAIL"


def test_overlay_shadows_a_partial_but_not_the_parent(dirs):
    base, ov = dirs
    (ov / "a.examples.md").write_text("MY examples")
    r = _res(dirs)
    assert r.load("a.system.md") == "HEAD\nMY examples\nTAIL"
    assert r.path_for("a.examples.md") == ov / "a.examples.md"
    assert r.path_for("a.system.md") == base / "a.system.md"
    assert r.overlays() == ["a.examples.md"]


def test_overlay_shadows_a_whole_file(dirs):
    base, ov = dirs
    (ov / "b.system.md").write_text("MINE")
    assert _res(dirs).load("b.system.md") == "MINE"
    assert _res(dirs, overlay=False).load("b.system.md") == "only shipped"


def test_nested_include_and_depth_cap(dirs):
    base, _ = dirs
    (base / "n0.md").write_text("{{include:n1.md}}")
    (base / "n1.md").write_text("x{{include:n2.md}}")
    (base / "n2.md").write_text("y")
    assert _res(dirs).load("n0.md") == "xy"
    (base / "loop.md").write_text("{{include:loop.md}}")
    with pytest.raises(PolicyError, match="depth"):
        _res(dirs).load("loop.md")


def test_missing_prompt_or_partial_is_a_policy_error(dirs):
    with pytest.raises(PolicyError, match="nope.md"):
        _res(dirs).load("nope.md")
    base, _ = dirs
    (base / "c.md").write_text("{{include:gone.md}}")
    with pytest.raises(PolicyError, match="gone.md"):
        _res(dirs).load("c.md")


def test_names_cannot_escape_the_prompt_dirs(dirs):
    for bad in ("../policy.toml", ".", ".."):
        with pytest.raises(PolicyError, match="name"):
            _res(dirs).load(bad)


def test_taxonomy_marker_renders_or_collapses(dirs):
    tax = Taxonomy(categories=(Category("person", "person", "an individual human"),))
    text = _res(dirs, taxonomy=tax).load("t.system.md")
    assert text == ("before\n\nYou may also use one of the fixed broad categories below.\n\n"
                    "Fixed broad categories:\n\n  person — an individual human\n\nafter")
    assert _res(dirs).load("t.system.md") == "before\n\nafter"    # empty taxonomy, no gap


def test_missing_overlay_dir_is_fine(tmp_path, dirs):
    base, _ = dirs
    r = PromptResolver(Policy(prompts=Prompts(str(tmp_path / "absent"))), base)
    assert r.load("b.system.md") == "only shipped" and r.overlays() == []


def test_shipped_prompt_reads_the_repo_prompts_without_overlay():
    from kb_core.prompts import shipped_prompt
    text = shipped_prompt("entity_gate.system.md")
    assert "noise" in text.lower() and "{{" not in text


def test_prompts_base_dir_from_enrichment_config_env(tmp_path):
    cfg = tmp_path / "cfg" / "enrichment.toml"
    assert (prompts_base_dir(env={"KB_ENRICHMENT_CONFIG": str(cfg)})
            == (tmp_path / "cfg" / "prompts").resolve())


def test_prompts_base_dir_defaults_to_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert prompts_base_dir(env={}) == (tmp_path / "prompts").resolve()
