"""kb_core.policy — loader, validation, defaults. Pure (no DB)."""
import pytest

from kb_core.policy import (DEFAULT_POLICY, Category, Match, PolicyError,
                            Projects, Taxonomy, load_policy)


def _load(tmp_path, text, **env):
    p = tmp_path / "policy.toml"
    p.write_text(text, encoding="utf-8")
    return load_policy(env={"KB_POLICY_CONFIG": str(p), **env})


def test_missing_default_file_yields_shipped_defaults(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)                       # no policy.toml here
    p = load_policy(env={})
    assert p is DEFAULT_POLICY
    assert p.is_default and p.source_path is None
    assert p.decay == () and p.projects is None
    assert p.prompts.overlay_dir is None              # no file -> no overlay
    assert [c.name for c in p.taxonomy.categories] == [
        "person", "organization", "place", "event", "project", "artifact", "concept", "system"]
    assert p.taxonomy.kind_for("person") == "person"
    assert p.taxonomy.kind_for("made-up thing") is None


def test_missing_explicit_path_is_an_error(tmp_path):
    with pytest.raises(PolicyError, match="not found"):
        load_policy(env={"KB_POLICY_CONFIG": str(tmp_path / "nope.toml")})
    with pytest.raises(PolicyError, match="not found"):
        load_policy(env={}, path=str(tmp_path / "nope.toml"))


def test_loaded_file_records_source_and_defaults_overlay(tmp_path):
    p = _load(tmp_path, "[importance]\nrecency_floor = 0.5\n")
    assert p.source_path.endswith("policy.toml") and not p.is_default
    assert p.importance.recency_floor == 0.5
    assert p.importance.recency_half_life_days == 8.0          # untouched default
    assert p.prompts.overlay_dir == "prompts.local"            # file loaded -> default overlay


def test_full_file_round_trips(tmp_path):
    p = _load(tmp_path, """
[[decay]]
name = "email"
match = { tags = ["email"] }
half_life_days = 17
floor = 0.2

[[decay]]
name = "journal"
match = { path_prefix = "Obsidian.Journal", source = "obsidian" }
half_life_days = 30
floor = 0.5

[projects]
folder_label = "Projects"
max_depth = 1
dormancy_half_life_days = 182.5

[importance]
recency_half_life_days = 8
recency_floor = 0.65
access_saturation = 10

[concepts]
goal_window_days = 14
data_recency_half_life_days = 14
retrieval_recency_half_life_days = 7
recency_floor = 0.2

[taxonomy]
categories = [
  { name = "person", kind = "person", description = "an individual human" },
  { name = "research paper", kind = "artifact", description = "a paper or publication" },
]
kind_seeds = { university = "organization", "AI Model" = "system" }

[prompts]
overlay_dir = "my.prompts"
""")
    assert [r.name for r in p.decay] == ["email", "journal"]
    assert p.decay[0].match == Match(tags=("email",))
    assert p.decay[1].match == Match(path_prefix="Obsidian.Journal", source="obsidian")
    assert p.decay[1].half_life_days == 30.0 and p.decay[1].floor == 0.5
    assert p.projects == Projects("Projects", 1, 182.5)
    assert p.importance.recency_half_life_seconds == 8 * 86400.0
    assert p.concepts.goal_window_days == 14
    assert p.taxonomy.kind_for("Research Paper") == "artifact"    # category, normalized
    assert p.taxonomy.kind_for("ai model") == "system"            # seed, normalized key
    assert p.taxonomy.kind_for("university") == "organization"
    assert p.prompts.overlay_dir == "my.prompts"
    d = p.to_dict()
    assert d["decay"][1]["match"] == {"path_prefix": "Obsidian.Journal", "source": "obsidian"}
    assert d["projects"]["folder_label"] == "Projects"
    assert d["taxonomy"]["kind_seeds"] == {"university": "organization", "ai model": "system"}
    assert d["prompts"] == {"overlay_dir": "my.prompts"}


def test_to_dict_of_defaults_has_every_section():
    d = DEFAULT_POLICY.to_dict()
    assert set(d) == {"decay", "projects", "importance", "concepts", "taxonomy", "prompts"}
    assert d["decay"] == [] and d["projects"] is None
    assert d["importance"] == {"recency_half_life_days": 8.0, "recency_floor": 0.65,
                               "access_saturation": 10.0}


@pytest.mark.parametrize("text, match", [
    ("[bogus]\nx = 1\n", "unknown section"),
    ("[importance]\nbogus = 1\n", r"unknown key"),
    ("[[decay]]\nname = \"a\"\nmatch = { tags = [\"t\"] }\nhalf_life_days = 1\nfloor = 0.1\nextra = 1\n", "unknown key"),
    ("[[decay]]\nname = \"Bad Name\"\nmatch = { tags = [\"t\"] }\nhalf_life_days = 1\nfloor = 0.1\n", "name"),
    ("[[decay]]\nname = \"a\"\nmatch = {}\nhalf_life_days = 1\nfloor = 0.1\n", "at least one"),
    ("[[decay]]\nname = \"a\"\nmatch = { tags = [] }\nhalf_life_days = 1\nfloor = 0.1\n", "tags"),
    ("[[decay]]\nname = \"a\"\nmatch = { path_prefix = \"a..b\" }\nhalf_life_days = 1\nfloor = 0.1\n", "path_prefix"),
    ("[[decay]]\nname = \"a\"\nmatch = { tags = [\"t\"] }\nhalf_life_days = 0\nfloor = 0.1\n", "half_life_days"),
    ("[[decay]]\nname = \"a\"\nmatch = { tags = [\"t\"] }\nhalf_life_days = 1\nfloor = 1.5\n", "floor"),
    ("[[decay]]\nname = \"a\"\nmatch = { tags = [\"t\"] }\nhalf_life_days = 1\n", "floor"),
    ("[[decay]]\nname = \"a\"\nmatch = { tags = [\"t\"] }\nhalf_life_days = 1\nfloor = 0.1\n"
     "[[decay]]\nname = \"a\"\nmatch = { tags = [\"u\"] }\nhalf_life_days = 1\nfloor = 0.1\n", "unique"),
    ("[projects]\nmax_depth = 1\n", "folder_label"),
    ("[projects]\nfolder_label = \"a.b\"\n", "folder_label"),
    ("[projects]\nfolder_label = \"Projects\"\nmax_depth = -1\n", "max_depth"),
    ("[projects]\nfolder_label = \"Projects\"\ndormancy_half_life_days = 0\n", "dormancy_half_life_days"),
    ("[importance]\nrecency_floor = 2\n", "recency_floor"),
    ("[importance]\naccess_saturation = 0\n", "access_saturation"),
    ("[concepts]\ngoal_window_days = 0\n", "goal_window_days"),
    ("[concepts]\ngoal_window_days = 400\n", "goal_window_days"),
    ("[taxonomy]\ncategories = [ { name = \"research_paper\", kind = \"artifact\", description = \"x\" } ]\n", "name"),
    ("[taxonomy]\ncategories = [ { name = \"paper\", kind = \"thing\", description = \"x\" } ]\n", "kind"),
    ("[taxonomy]\ncategories = [ { name = \"paper\", kind = \"artifact\", description = \"\" } ]\n", "description"),
    ("[taxonomy]\ncategories = [ { name = \"paper\", kind = \"artifact\", description = \"x\" },"
     " { name = \"Paper\", kind = \"artifact\", description = \"y\" } ]\n", "unique"),
    ("[taxonomy]\ncategories = [ { name = \"paper\", kind = \"artifact\", description = \"x\" } ]\n"
     "kind_seeds = { paper = \"system\" }\n", "agree"),
    ("[taxonomy]\nkind_seeds = { thing = \"gadget\" }\n", "kind_seeds"),
    ("[prompts]\noverlay_dir = 7\n", "overlay_dir"),
])
def test_validation_failures_name_the_problem(tmp_path, text, match):
    with pytest.raises(PolicyError, match=match):
        _load(tmp_path, text)


def test_overlay_dir_that_is_a_file_is_rejected(tmp_path):
    (tmp_path / "ov").write_text("not a dir")
    with pytest.raises(PolicyError, match="overlay_dir"):
        _load(tmp_path, f"[prompts]\noverlay_dir = \"{tmp_path / 'ov'}\"\n")


def test_invalid_toml_is_a_policy_error(tmp_path):
    with pytest.raises(PolicyError, match="TOML"):
        _load(tmp_path, "[[decay]\n")


def test_prompt_block_renders_categories_or_nothing():
    assert Taxonomy().prompt_block() == ""
    t = Taxonomy(categories=(Category("person", "person", "an individual human"),
                             Category("tool", "system", "a reusable technical tool")))
    block = t.prompt_block()
    assert block.startswith("You may also use one of the fixed broad categories below.\n\n"
                            "Fixed broad categories:\n\n")
    assert "  person — an individual human\n  tool — a reusable technical tool" in block


def test_match_enforces_at_least_one_condition():
    with pytest.raises(PolicyError, match="at least one"):
        Match()
    Match(tags=("x",))                                 # does not raise


def test_policy_is_frozen():
    import dataclasses
    with pytest.raises(dataclasses.FrozenInstanceError):
        DEFAULT_POLICY.decay = ()                       # type: ignore[misc]
