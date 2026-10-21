"""Pure helpers: policy kind seeds and class-name typo detection (no DB)."""
from kb_core.entities import KINDS, ROLES, class_name_typo_of
from kb_core.policy import Category, Policy, Taxonomy
from tests.legacy_policy import LEGACY_POLICY


def test_kinds_and_roles_frozen():
    assert KINDS == ("person", "organization", "place", "event", "project",
                     "artifact", "concept", "system", "other")
    assert ROLES == ("stakeholder", "owner", "participant", "subject", "tool-used")


def test_legacy_taxonomy_seeds_known_names():
    kind_for = LEGACY_POLICY.taxonomy.kind_for
    assert kind_for("person") == "person"
    assert kind_for("Company") == "organization"       # case-insensitive
    assert kind_for("university") == "organization"    # seed, not a category
    assert kind_for("location") == "place"
    assert kind_for("event") == "event"
    assert kind_for("plan") == "project"
    assert kind_for("research paper") == "artifact"
    assert kind_for("design principle") == "concept"
    assert kind_for("system component") == "system"
    assert kind_for("made-up thing") is None


def test_shipped_taxonomy_seeds_only_the_structural_kinds():
    kind_for = Policy().taxonomy.kind_for
    assert kind_for("System") == "system" and kind_for("place") == "place"
    assert kind_for("company") is None                 # the owner's vocabulary is gone
    assert kind_for("repository") is None


def test_category_beats_seed_and_seed_beats_nothing():
    t = Taxonomy(categories=(Category("paper", "artifact", "x"),),
                 kind_seeds={"paper": "artifact", "lab": "organization"})
    assert t.kind_for("Paper") == "artifact" and t.kind_for("lab") == "organization"
    assert t.kind_for("lake") is None


def test_typo_of_catches_real_typos():
    existing = ["company", "organization", "tool", "concept"]
    assert class_name_typo_of("conpany", existing) == "company"     # 1 substitution
    assert class_name_typo_of("icompany", existing) == "company"    # 1 insertion
    assert class_name_typo_of("companyy", existing) == "company"


def test_typo_of_spares_distinct_names():
    existing = ["company", "method", "metric", "person", "plan", "pattern"]
    assert class_name_typo_of("platform", existing) is None
    assert class_name_typo_of("method", existing) == "method"  # exact = itself, harmless
    assert class_name_typo_of("metric", existing) == "metric"
    assert class_name_typo_of("prison", existing) is None      # NOT folded into person
    assert class_name_typo_of("clan", existing) is None        # short names never fold


def test_typo_of_short_names_never_fold():
    # < 5 letters: edit distance 1 is a different word (plan/clan/flan), never a typo
    assert class_name_typo_of("plna", ["plan"]) is None
