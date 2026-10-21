"""Pure confidence signals."""
import math

import pytest

from kb_core.confidence_signal import (SIGNALS, BackgroundStats, ShippedRule, apply_rule,
                                       level_from_signal, signal_value)

BG = BackgroundStats.from_sims([0.30, 0.32, 0.34, 0.36, 0.38])   # mean 0.34, max 0.38


def test_background_stats():
    assert BG.n == 5 and BG.max == 0.38
    assert math.isclose(BG.mean, 0.34)
    assert math.isclose(BG.std, math.sqrt(0.0008))
    assert BG.percentile_of(0.35) == 0.6          # 3 of 5 strictly below
    assert BackgroundStats.from_sims([0.5]) is None
    assert BackgroundStats.from_sims([None, 0.4, 0.5]).n == 2


def test_background_signals():
    curve = [0.50, 0.40, 0.30]
    assert math.isclose(signal_value("bg_z", curve, BG), (0.50 - 0.34) / math.sqrt(0.0008))
    assert math.isclose(signal_value("bg_gap_max", curve, BG), 0.12)
    assert signal_value("bg_pct", curve, BG) == 1.0
    assert signal_value("bg_z", curve, None) is None          # no background -> undefined


def test_shape_signals():
    curve = [0.50, 0.40, 0.30]
    assert math.isclose(signal_value("gap12", curve, None), 0.10)
    assert math.isclose(signal_value("gap_med", curve, None), 0.10)
    sd = math.sqrt(((0.1) ** 2 + 0 + (0.1) ** 2) / 3)
    assert math.isclose(signal_value("z_in", curve, None), 0.10 / sd)
    assert math.isclose(signal_value("sigma", curve, None), sd)
    assert signal_value("gap12", [0.5], None) is None          # too short
    assert signal_value("z_in", [0.5, 0.5, 0.5], None) is None  # zero spread


def test_every_signal_declares_family_and_min_len():
    for name, s in SIGNALS.items():
        assert s.family in ("background", "shape"), name
        assert s.min_len >= 1, name


def test_level_from_signal_is_inclusive_at_the_cuts():
    assert level_from_signal(1.0, 1.0, 2.0) == "weak"
    assert level_from_signal(2.0, 1.0, 2.0) == "strong"
    assert level_from_signal(0.99, 1.0, 2.0) == "none"


def test_apply_rule_plain_and_gated():
    curve = [0.50, 0.40, 0.30]
    plain = ShippedRule(signal="gap12", cut_weak=0.05, cut_strong=0.2, min_curve=2)
    assert apply_rule(plain, curve, None) == ("weak", pytest.approx(0.10))
    assert apply_rule(plain, [0.5], None) is None              # below min_curve
    gated = ShippedRule(signal="gap12", cut_weak=0.05, cut_strong=0.2, min_curve=2,
                        gate_signal="bg_z", gate_cut=100.0)
    assert apply_rule(gated, curve, BG)[0] == "none"            # gate not cleared
    assert apply_rule(gated, curve, None) is None               # gate undefined -> fallback
