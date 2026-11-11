import math

import pytest

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
