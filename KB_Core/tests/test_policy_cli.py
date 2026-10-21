"""kb-policy show / check, and the committed example file. Pure (no DB)."""
from kb_core.policy import load_policy, main


def test_example_file_loads_and_is_not_the_owners(monkeypatch):
    # Run from KB_Core (like every test that reads ./enrichment.toml).
    p = load_policy(path="policy.toml.example")
    assert not p.is_default
    assert p.decay and all(r.name != "email" for r in p.decay)      # generic sample rules
    assert p.projects is not None
    assert p.taxonomy.categories
    for r in p.decay:                                               # no owner numbers
        assert r.half_life_days != 17.0


def test_check_reports_ok_and_failure(tmp_path, capsys):
    good = tmp_path / "good.toml"
    good.write_text("[importance]\nrecency_floor = 0.5\n")
    assert main(["check", str(good)]) == 0
    assert f"ok: {good}" in capsys.readouterr().out
    bad = tmp_path / "bad.toml"
    bad.write_text("[importance]\nrecency_floor = 5\n")
    assert main(["check", str(bad)]) == 1
    assert "recency_floor" in capsys.readouterr().err


def test_check_without_a_file_reports_shipped_defaults(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("KB_POLICY_CONFIG", raising=False)
    # No enrichment.toml here (load_passes tolerates that, returning []), but the two
    # judge system files are always checked regardless — a normal install always ships
    # them alongside enrichment.toml, so stand them in here too.
    prompts = tmp_path / "prompts"
    prompts.mkdir()
    (prompts / "meaning_judge.system.md").write_text("ok")
    (prompts / "concept_judge.system.md").write_text("ok")
    assert main(["check"]) == 0
    assert "shipped defaults" in capsys.readouterr().out


def test_show_prints_source_overlays_and_json(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "policy.toml").write_text(
        "[[decay]]\nname = \"news\"\nmatch = { tags = [\"news\"] }\n"
        "half_life_days = 3\nfloor = 0.1\n[prompts]\noverlay_dir = \"ov\"\n")
    (tmp_path / "ov").mkdir()
    (tmp_path / "ov" / "entities.examples.md").write_text("x")
    monkeypatch.delenv("KB_POLICY_CONFIG", raising=False)
    assert main(["show"]) == 0
    out = capsys.readouterr().out
    assert "source: policy.toml" in out
    assert "overlays: entities.examples.md" in out
    assert '"name": "news"' in out


def test_show_reports_error_on_a_bad_file_instead_of_a_traceback(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "policy.toml").write_text("[importance]\nrecency_floor = 5\n")
    monkeypatch.delenv("KB_POLICY_CONFIG", raising=False)
    assert main(["show"]) == 1
    assert "recency_floor" in capsys.readouterr().err


def test_check_validates_the_prompt_surface_too(tmp_path, monkeypatch, capsys):
    # `check` must not stop at load_policy(): every prompt an install actually reads
    # (enrichment passes + the two judge system files + every overlay file present)
    # has to resolve, or a broken include would only surface at daemon start instead
    # of at `kb-policy check` time.
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("KB_POLICY_CONFIG", raising=False)
    monkeypatch.delenv("KB_ENRICHMENT_CONFIG", raising=False)
    prompts = tmp_path / "prompts"
    prompts.mkdir()
    (prompts / "x.system.md").write_text("ok")
    (prompts / "meaning_judge.system.md").write_text("ok")
    (prompts / "concept_judge.system.md").write_text("ok")
    (tmp_path / "enrichment.toml").write_text(
        "[[pass]]\nname = \"x\"\nsystem_file = \"prompts/x.system.md\"\nuser = \"u\"\n")
    (tmp_path / "policy.toml").write_text("[prompts]\noverlay_dir = \"ov\"\n")
    ov = tmp_path / "ov"
    ov.mkdir()
    (ov / "x.system.md").write_text("{{include:typo.md}}")

    assert main(["check"]) == 1
    assert "typo.md" in capsys.readouterr().err

    (ov / "x.system.md").write_text("fine")
    assert main(["check"]) == 0
