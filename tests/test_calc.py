import math

import pytest

from kria.appraisal import calc
from kria.appraisal.calc import diagnostic, fragility_index, risk_metrics


def test_risk_metrics_known_values():
    r = risk_metrics(20, 200, 40, 200)
    assert r["ARR"] == pytest.approx(0.10)
    assert r["RR"] == pytest.approx(0.5)
    assert r["NNT"] == pytest.approx(10.0)
    lo, hi = r["ARR_95CI"]
    assert lo == pytest.approx(0.0300, abs=1e-4)
    assert hi == pytest.approx(0.1699, abs=1e-4)
    assert r["fisher_p"] == pytest.approx(0.00742, abs=1e-5)


def test_fragility_index_significant():
    assert fragility_index(20, 200, 40, 200) == 5


def test_fragility_index_not_significant_returns_none():
    assert fragility_index(20, 200, 22, 200) is None


def test_diagnostic_known_values():
    d = diagnostic(90, 30, 10, 170)
    assert d["sens"] == pytest.approx(0.90)
    assert d["spec"] == pytest.approx(0.85)
    assert d["LRpos"] == pytest.approx(6.0)
    assert d["LRneg"] == pytest.approx(0.1176, abs=1e-4)
    assert d["post_pos"][0.15] == pytest.approx(0.5143, abs=1e-4)
    assert math.isfinite(d["post_neg"][0.05])


def test_binary_effect_bad_event_benefit():
    # Death 20/100 with treatment vs 30/100 with control: 10 points fewer deaths, NNT 10.
    r = calc.binary_effect(20, 100, 30, 100, undesirable=True)
    assert r["difference"] == pytest.approx(-0.10)
    assert r["direction"] == "benefit"
    assert r["NNT"] == pytest.approx(10.0) and r["NNH"] is None
    lo, hi = r["difference_95CI"]
    assert lo < -0.10 < hi and hi < 0.03  # Newcombe interval, mirrored from the ARR interval
    assert r["RR"] == pytest.approx(2 / 3)


def test_binary_effect_good_event():
    # Survival 30/100 with treatment vs 20/100 with control: benefit, NNT 10.
    r = calc.binary_effect(30, 100, 20, 100, undesirable=False)
    assert r["direction"] == "benefit" and r["NNT"] == pytest.approx(10.0)
    # Survival lower with treatment: harm, NNH 10.
    r = calc.binary_effect(20, 100, 30, 100, undesirable=False)
    assert r["direction"] == "harm" and r["NNH"] == pytest.approx(10.0) and r["NNT"] is None


def test_binary_effect_no_difference_and_fragility():
    r = calc.binary_effect(10, 100, 10, 100)
    assert r["direction"] == "none" and r["NNT"] is None and r["NNH"] is None
    assert r["fragility_index"] is None  # not significant
    sig = calc.binary_effect(5, 200, 25, 200)
    assert sig["fisher_p"] < 0.05 and sig["fragility_index"] >= 1
