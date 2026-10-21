import pytest

from kb_core.entities import (
    clean_class_description,
    entity_external_id,
    entity_parent_path,
    instance_name_issue,
    is_valid_class_name,
    is_valid_instance_name,
    normalize_entity_name,
)
from kb_core.identity import node_id
from kb_core.registry import default_registry


def test_normalize_collapses_and_lowercases():
    assert normalize_entity_name("  Avery   Quinn ") == "avery quinn"


def test_external_id_is_type_scoped_and_normalized():
    assert entity_external_id("person", "Avery") == "person:avery"
    # case/whitespace differences resolve to the same id -> dedup
    a = node_id("entity", entity_external_id("person", "Avery"))
    b = node_id("entity", entity_external_id("person", " avery "))
    assert a == b


def test_parent_path_under_entities_root():
    assert entity_parent_path("person") == "entities.person"


def test_default_registry_has_entity():
    assert default_registry().is_registered("entity")


@pytest.mark.parametrize("name", [
    "people", "projects", "databases", "Research Papers", "co-workers", "people's homes",
])
def test_valid_class_names_accepted(name):
    assert is_valid_class_name(name) is True


@pytest.mark.parametrize("name", [
    "NEW_CATEGORY_NAME",           # the canonical placeholder leak
    "category", "Categories", "example", "new", "name", "n/a", "unknown",  # meta words
    "types", "confidence",         # extract-schema field names the model parrots as types
    "snake_case_thing",            # underscores never appear in real category names
    "", "   ", "123", "***",       # empty / no letters
    "a fairly long category phrase that is really a sentence",  # > 4 words
])
def test_placeholder_class_names_rejected(name):
    assert is_valid_class_name(name) is False


@pytest.mark.parametrize("name", [
    "entity resolution", "knowledge graph design",        # 2-3 word concepts
    "local large language inference",                      # exactly 4 words: now allowed
])
def test_four_word_concept_names_accepted(name):
    assert is_valid_class_name(name) is True


@pytest.mark.parametrize("name", [
    "a concept phrase with five words",                    # 5 words: still rejected
    "a fairly long category phrase that is really a sentence",  # 9 words
])
def test_class_names_over_four_words_rejected(name):
    assert is_valid_class_name(name) is False


@pytest.mark.parametrize("name", [
    # Verbatim degenerate types a local-model repetition spiral once coined. Hyphens used
    # to count as word-internal, so each hyphen-glued run-on passed the 4-word cap as ONE word.
    "type-of-a-feature-that-is-not-a-project-or-of-the-feature-feature-in-the-pull-request",
    " Underscore-based-on-implementation-details-in-the-prompt-engineering-prompt-for-Zelda"
    "-is-a-leakage-direction-leakage-direction-leak",
    "type-of-a-the-" + "a-" * 56 + " Underscore-based-" + "a-" * 9
    + "capability-capability-a-" + "implementation-detail-" * 15 + "implementation-a-a-implementation-",
    "feature feature",                # adjacent repeated word: degeneration signature
    "implementation-detail-implementation-detail-implementation",  # 5 words once hyphens split
    "a" * 59,                         # single-word runaway under the total length cap
    "customer relationship management systems overview",  # 49 chars but 5 words
])
def test_degenerate_runaway_class_names_rejected(name):
    assert is_valid_class_name(name) is False


@pytest.mark.parametrize("name", [
    "co-workers",                       # hyphenated compound: 2 words, fine
    "e-commerce platform",              # hyphen inside a real compound
    "peer to peer network",             # non-adjacent repeat is legitimate
    "customer relationship management system",  # 4 words, just under the length cap
])
def test_real_hyphen_and_repeat_class_names_still_pass(name):
    assert is_valid_class_name(name) is True


@pytest.mark.parametrize("name", ["D", "x", " R "])
def test_single_letter_class_names_rejected(name):
    # A lone letter is never a category ('D' was once coined as a class) — but
    # single-letter INSTANCES stay legal: the C or R language is an instance of a class.
    assert is_valid_class_name(name) is False
    assert is_valid_instance_name(name.strip()) is True


@pytest.mark.parametrize("name", [
    "Avery Quinn", "Zelda", "uv", "Vandelay",
    "Acme Robotics Club",     # >3 words: fine for instances (unlike classes)
    "claude-sonnet-4-6", "atlas_dockV2",  # digits/hyphens/underscores: real tech names
])
def test_valid_instance_names_accepted(name):
    assert is_valid_instance_name(name) is True
    assert instance_name_issue(name) is None


@pytest.mark.parametrize("name", [
    "README.md", "CLAUDE.md", "BUILD-NOTES.md", "run_case.py",         # filenames
    "scripts/run_case.py", "a\\b",                                     # paths
    "quarterly-review-ask-sam-about-the-draft-budget-figures",          # 55-char task slug
    "her", "team", "they", "someone", "everything",                    # pronoun/generic
    "category", "placeholder", "unknown",                               # placeholder/meta
    "", "   ", "***",                                                   # empty / no alnum
])
def test_junk_instance_names_rejected(name):
    assert is_valid_instance_name(name) is False
    assert instance_name_issue(name) is not None


def test_legacy_and_shipped_category_names_pass_validation():
    # Every category a policy offers to the extract prompt must survive is_valid_class_name,
    # or the model's reuse of it would be silently dropped as an artifact. The loader
    # enforces this for files; the in-code fixtures are checked here.
    from kb_core.policy import Policy
    from tests.legacy_policy import LEGACY_POLICY
    for pol in (LEGACY_POLICY, Policy()):
        for c in pol.taxonomy.categories:
            assert is_valid_class_name(c.name) is True, c.name


@pytest.mark.parametrize("name", ["product/tool", "dataset/benchmark", "research_paper",
                                  "system_component"])
def test_slash_and_underscore_labels_are_rejected(name):
    # Documents why the curated labels are space-separated (not the slash/underscore originals):
    # the artifact guard rejects '/' and '_'.
    assert is_valid_class_name(name) is False


def test_clean_description_passes_normal_text():
    desc = "correspondence and messages received or sent"
    assert clean_class_description(desc) == desc


def test_clean_description_trims_surrounding_whitespace():
    assert clean_class_description("  documents and notes  ") == "documents and notes"


def test_clean_description_keeps_legitimate_longer_text():
    # the longest real description in the taxonomy — must survive the length cap
    desc = "work items, work streams, or specific initiatives-related to a single subject matter"
    assert clean_class_description(desc) == desc


@pytest.mark.parametrize("desc", [None, "", "   "])
def test_clean_description_empty_is_none(desc):
    assert clean_class_description(desc) is None


def test_clean_description_rejects_overlong_runaway():
    # the SLM degeneration that motivated this guard: a ~1000-char repetition spiral
    garbage = "individuals or entities that are involved in a S_L_I_N_K_Y " + "r_e_" * 300
    assert clean_class_description(garbage) is None


def test_clean_description_rejects_low_diversity_collapse():
    # a repetition collapse that stays UNDER the length cap — caught by char diversity
    assert clean_class_description("r_e_" * 15) is None   # 60 chars, 3 distinct chars


@pytest.mark.parametrize("name", [
    "_conftest",                       # leading underscore
    "SKU_FIELD_MAP", "PALLET_LOCK_STATES", "ANTHROPIC_API_KEY",  # SCREAMING_SNAKE
    "chrome.runtime.sendMessage",      # dotted path, 2+ dots
    "chat.getPermalink",               # dotted path, camelCase segment
    "C01A2B3C4D5",                     # opaque id (caps + digits, no lowercase run)
])
def test_code_symbol_instance_names_rejected(name):
    from kb_core.entities import code_symbol_issue
    assert code_symbol_issue(name) is not None
    assert is_valid_instance_name(name) is False


@pytest.mark.parametrize("name", [
    "lantern_sync", "rubric grader", "keystone-env", "Vandelay",
    "claude-sonnet-4-6", "atlas_dockV2", "TDD", "uv", "GPT4",
    # brand+number and version-suffixed real names must reach the judge, not be hard-rejected
    "SHA256", "RTX4090", "ISO8601", "COVID_19", "GPT_4", "US_EAST_1",
])
def test_real_names_survive_code_symbol_filter(name):
    from kb_core.entities import code_symbol_issue
    assert code_symbol_issue(name) is None


def test_named_plan_is_a_valid_instance_name():
    # Named plans are legitimate anchors — the
    # deterministic guard must admit short noun-phrase plan names…
    from kb_core.entities import instance_name_issue
    assert instance_name_issue("billing schema redesign plan") is None
    assert instance_name_issue("Postgres migration plan") is None


def test_todo_sentence_still_rejected_by_length_guard():
    # …while a sentence-shaped to-do stays out (length guard, unchanged).
    from kb_core.entities import instance_name_issue
    issue = instance_name_issue(
        "migrate the billing store to Postgres before Friday and update the docs")
    assert issue is not None and "too long" in issue
