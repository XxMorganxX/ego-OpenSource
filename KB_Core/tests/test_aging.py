"""Pure endpoints + SQL/Python parity for the rule-driven content-age decay (kb_core.aging).

The SQL twin is what production runs (inside memory_knn_edges / concept_mention_edges /
promotable_entities); the pure functions are what tests and callers reason with. Rules
come from the policy; the fixtures here spell them out."""
import math

from kb_core.aging import age_factor, age_params, age_sql, matching_rule
from kb_core.policy import DecayRule, Match
from tests.legacy_policy import LEGACY_EMAIL_RULE as EMAIL

JOURNAL = DecayRule("journal", Match(path_prefix="Obsidian.Journal"), 30.0, 0.5)
MAILSYNC = DecayRule("mailsync", Match(source="mailsync", tags=("email",)), 10.0, 0.0)
RULES = (EMAIL, JOURNAL, MAILSYNC)

_COLS = dict(tags="ARRAY['email','mailsync']::text[]", path="'Obsidian.Journal.x'::ltree",
             source="'obsidian'")


def _sql(col, rules=RULES, **cols):
    c = {**_COLS, **cols}
    return "SELECT " + age_sql(col, c["tags"], c["path"], c["source"], rules) + " AS f"


def test_age_factor_endpoints():
    assert age_factor(None, EMAIL) == 1.0                    # undated content never decays
    assert age_factor(0, EMAIL) == 1.0
    assert math.isclose(age_factor(EMAIL.half_life_days, EMAIL),   # 0.6 at one half-life
                        EMAIL.floor + (1 - EMAIL.floor) / 2)
    assert abs(age_factor(10**9, EMAIL) - EMAIL.floor) < 1e-9      # asymptotes to the floor
    assert age_factor(-5, EMAIL) == 1.0                      # future event_at: clamp, no boost
    assert abs(age_factor(30, JOURNAL) - 0.75) < 1e-12       # a different rule's curve


def test_reference_values_from_the_2026_08_12_spec():
    for days, expected in ((7, 0.80), (14, 0.65), (30, 0.44), (60, 0.27), (90, 0.22)):
        assert abs(age_factor(days, EMAIL) - expected) < 0.005


def test_matching_rule_is_first_match_in_file_order():
    assert matching_rule(RULES, tags=["email", "mailsync"], path="Obsidian.Journal.x",
                         source="mailsync") is EMAIL             # email listed first
    assert matching_rule(RULES, tags=["note"], path="Obsidian.Journal.x",
                         source="obsidian") is JOURNAL
    assert matching_rule(RULES, tags=["email"], path="Emails.x", source="mailsync") is EMAIL
    assert matching_rule((JOURNAL, MAILSYNC), tags=["email"], path="Emails.x",
                         source="mailsync") is MAILSYNC           # both conditions hold
    assert matching_rule((MAILSYNC,), tags=["email"], path="Emails.x",
                         source="obsidian") is None             # source mismatch
    assert matching_rule((), tags=["email"], path="x", source="y") is None


def test_no_rules_is_the_constant_one():
    assert age_sql("m.event_at", "m.tags", "m.path", "m.source", ()) == "1.0::float8"
    assert age_params(()) == {"age_ln2": math.log(2)}


def test_sql_python_parity_per_rule(pool):
    with pool.connection() as conn:
        for rule, cols in ((EMAIL, {}),
                           (JOURNAL, dict(tags="ARRAY['note']::text[]")),
                           (MAILSYNC, dict(source="'mailsync'", path="'Emails.a'::ltree"))):
            for days in (0, 1, 17, 90, 400):
                row = conn.execute(
                    _sql("(now() - make_interval(days => %(d)s))", (rule,), **cols),
                    {"d": days, **age_params((rule,))}).fetchone()
                assert math.isclose(row["f"], age_factor(days, rule), rel_tol=1e-6), rule.name
        row = conn.execute(_sql("NULL::timestamptz"), age_params(RULES)).fetchone()
        assert row["f"] == 1.0                            # NULL event_at -> exactly 1.0
        row = conn.execute(_sql("(now() + interval '30 days')"), age_params(RULES)).fetchone()
        assert row["f"] == 1.0                            # future timestamp -> clamped


def test_sql_first_match_wins_and_unmatched_never_decays(pool):
    old = "(now() - interval '400 days')"
    with pool.connection() as conn:
        p = age_params(RULES)
        # email + journal path + mailsync source: EMAIL is first in the list -> its floor.
        assert abs(conn.execute(_sql(old, source="'mailsync'"), p).fetchone()["f"]
                   - EMAIL.floor) < 1e-6
        # journal path without the email tag -> JOURNAL's curve.
        assert abs(conn.execute(_sql(old, tags="ARRAY['note']::text[]"), p).fetchone()["f"]
                   - age_factor(400, JOURNAL)) < 1e-6
        # nothing matches -> exactly 1.0 however old.
        for cols in (dict(tags="ARRAY[]::text[]", path="'Notes.x'::ltree"),
                     dict(tags="ARRAY['note','mailsync']::text[]", path="'Notes.x'::ltree")):
            assert conn.execute(_sql(old, **cols), p).fetchone()["f"] == 1.0


def test_disabled_collapses_every_rule_to_one(pool):
    off = age_params(RULES, enabled=False)
    assert all(off[f"d{i}_floor"] == 1.0 for i in range(len(RULES)))
    assert age_params(RULES)["d0_floor"] == EMAIL.floor      # enabled dict untouched
    with pool.connection() as conn:
        row = conn.execute(_sql("(now() - interval '400 days')"), off).fetchone()
        assert row["f"] == 1.0
