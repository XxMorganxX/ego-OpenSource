from datetime import datetime, timezone

from kb_core.importance import (
    NodeStats, default_modifier_registry, compute_importance,
    decay_factor, effective_importance, dormancy_factor, IMPORTANCE_MAX,
)
from kb_core.policy import Importance, Policy, Projects

IMP = Importance()                      # shipped: 8 d, 0.65, saturation 10
FLOOR, HL = IMP.recency_floor, IMP.recency_half_life_seconds

# The project_dormancy modifier is inert without a [projects] section (see
# test_registry_without_projects_section_never_decays_inherited), so the pre-existing
# project-dormancy tests below need one to exercise actual decay; the old hardcoded
# folder_label="Projects" / half-life 182.5 (Projects' own defaults).
REG = default_modifier_registry(Policy(projects=Projects("Projects")))
NOW = datetime(2026, 6, 14, tzinfo=timezone.utc)


def _stats(**over):
    base = dict(access_count=0, last_accessed_at=None, ingested_at=NOW, modifiers={})
    base.update(over)
    return NodeStats(**base)


def test_neutral_node_is_1():
    imp, mods = compute_importance(_stats(), REG)
    assert imp == 1.0
    assert mods == {"manual": 0.0, "access": 0.0, "inherited": 0.0,
                    "project_dormancy": 0.0}


def test_inherited_adds_directly():
    imp, mods = compute_importance(_stats(modifiers={"inherited": 1.5}), REG)
    assert imp == 2.5                 # 1.0 base + 1.0 * 1.5
    assert mods["inherited"] == 1.5


def test_manual_adds_directly():
    imp, mods = compute_importance(_stats(modifiers={"manual": 1.0}), REG)
    assert imp == 2.0
    assert mods["manual"] == 1.0


def test_access_saturates():
    imp, _ = compute_importance(_stats(access_count=10), REG)
    assert imp == 1.0 + 0.5 * (10 / 20)          # 1.25


def test_importance_is_clamped():
    imp, _ = compute_importance(_stats(modifiers={"manual": 99.0}), REG)
    assert imp == IMPORTANCE_MAX


def test_decay_factor_endpoints():
    assert decay_factor(0, FLOOR, HL) == 1.0
    mid = decay_factor(HL, FLOOR, HL)
    assert abs(mid - (FLOOR + (1 - FLOOR) * 0.5)) < 1e-9
    assert abs(decay_factor(10**12, FLOOR, HL) - FLOOR) < 1e-6
    assert decay_factor(HL, 0.5, HL) == 0.75                 # floor is a parameter now


def test_effective_importance_decays_boost_not_baseline():
    assert effective_importance(1.0, 10**9, FLOOR, HL) == 1.0          # neutral never penalized
    assert effective_importance(2.0, 0, FLOOR, HL) == 2.0              # fresh = full boost
    half = effective_importance(2.0, HL, FLOOR, HL)
    assert abs(half - (1.0 + 1.0 * (FLOOR + (1 - FLOOR) * 0.5))) < 1e-9


def test_dormancy_factor_endpoints():
    assert Projects("Projects").dormancy_half_life_days == 182.5
    assert dormancy_factor(0, 182.5) == 1.0
    assert dormancy_factor(None, 182.5) == 1.0                    # no signal, no decay
    assert dormancy_factor(-5, 182.5) == 1.0                      # future activity clamps
    assert abs(dormancy_factor(182.5, 182.5) - 0.5) < 1e-12
    assert abs(dormancy_factor(365, 182.5) - 0.25) < 1e-12
    assert abs(dormancy_factor(10, 10) - 0.5) < 1e-12             # half-life is a parameter
    assert dormancy_factor(10**6, 182.5) < 1e-6                   # no floor


def test_registry_reads_saturation_and_dormancy_half_life_from_policy():
    pol = Policy(importance=Importance(access_saturation=2.0),
                 projects=Projects("Projects", dormancy_half_life_days=10.0))
    reg = default_modifier_registry(pol)
    imp, mods = compute_importance(_stats(access_count=2), reg)
    assert mods["access"] == 0.5                                  # 2 / (2 + 2)
    assert abs(imp - 1.25) < 1e-12
    _, mods = compute_importance(_stats(modifiers={"inherited": 1.0}, project_dormant_days=10), reg)
    assert abs(mods["project_dormancy"] - 0.5) < 1e-12            # one 10-day half-life


def test_registry_without_projects_section_never_decays_inherited():
    reg = default_modifier_registry(Policy())
    imp, mods = compute_importance(_stats(modifiers={"inherited": 1.0}, project_dormant_days=400), reg)
    assert mods["project_dormancy"] == 0.0 and imp == 2.0


def test_project_dormancy_removes_inherited_boost_only():
    # Full boost while the project is active.
    imp, mods = compute_importance(_stats(modifiers={"inherited": 1.0},
                                          project_dormant_days=0), REG)
    assert imp == 2.0
    assert mods["project_dormancy"] == 0.0
    # Half the boost after one half-life; identity holds exactly by construction.
    for days in (30, 182, 365, 730):
        imp, mods = compute_importance(_stats(modifiers={"inherited": 1.0},
                                              project_dormant_days=days), REG)
        assert abs(imp - (1.0 + dormancy_factor(days, 182.5))) < 1e-12
        assert mods["project_dormancy"] == 1.0 - dormancy_factor(days, 182.5)


def test_project_dormancy_is_zero_without_inherited_boost():
    imp, mods = compute_importance(_stats(project_dormant_days=1000), REG)
    assert imp == 1.0
    assert mods["project_dormancy"] == 0.0


def test_project_dormancy_leaves_manual_and_access_alone():
    imp, mods = compute_importance(
        _stats(access_count=10, modifiers={"manual": 1.0, "inherited": 1.0},
               project_dormant_days=365), REG)
    # 1 + manual 1.0 + access 0.25 + inherited 1.0 * 0.25
    assert abs(imp - (1.0 + 1.0 + 0.25 + 0.25)) < 1e-12
    assert mods["manual"] == 1.0
    assert mods["access"] == 0.5
