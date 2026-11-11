"""Deterministic statistics for critical appraisal.

All numbers in an appraisal are computed here, never by the language model.
"""

import math

from scipy.stats import fisher_exact


def wilson(x: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson score interval for a proportion x/n."""
    p = x / n
    d = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / d
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return centre - half, centre + half


def risk_metrics(e1: int, n1: int, e0: int, n0: int) -> dict:
    """Binary outcome, assumed undesirable. e1/n1 = intervention, e0/n0 = control.

    ARR > 0 means fewer bad outcomes with the intervention. The ARR interval uses
    Newcombe's hybrid score method (method 10).
    """
    p1, p0 = e1 / n1, e0 / n0
    arr = p0 - p1
    l1, u1 = wilson(e1, n1)
    l0, u0 = wilson(e0, n0)
    lo = arr - math.sqrt((p0 - l0) ** 2 + (u1 - p1) ** 2)
    hi = arr + math.sqrt((u0 - p0) ** 2 + (p1 - l1) ** 2)
    rr = p1 / p0 if p0 else float("nan")
    nnt = 1 / arr if arr else float("inf")
    _, p = fisher_exact([[e1, n1 - e1], [e0, n0 - e0]])
    return {
        "p_int": p1,
        "p_ctrl": p0,
        "ARR": arr,
        "ARR_95CI": (lo, hi),
        "RR": rr,
        "RRR": 1 - rr,
        "NNT": nnt,
        "fisher_p": float(p),
    }


def fragility_index(e1: int, n1: int, e0: int, n0: int, alpha: float = 0.05) -> int | None:
    """Walsh et al. 2014: in the arm with fewer events, convert non-events to events
    until Fisher's exact p >= alpha. Returns None if the result is not significant."""
    _, p = fisher_exact([[e1, n1 - e1], [e0, n0 - e0]])
    if p >= alpha:
        return None
    arm = 1 if e1 <= e0 else 0
    a, c, f = e1, e0, 0
    while p < alpha:
        if arm == 1:
            a += 1
        else:
            c += 1
        f += 1
        _, p = fisher_exact([[a, n1 - a], [c, n0 - c]])
    return f


def diagnostic(tp: int, fp: int, fn: int, tn: int, pretest=(0.05, 0.15, 0.30)) -> dict:
    """Diagnostic accuracy from a 2x2 table, with post-test probabilities."""
    sens, spec = tp / (tp + fn), tn / (tn + fp)
    lrp = sens / (1 - spec) if spec < 1 else float("inf")
    lrn = (1 - sens) / spec

    def post(pre: float, lr: float) -> float:
        odds = pre / (1 - pre) * lr
        return odds / (1 + odds)

    return {
        "sens": sens,
        "sens_95CI": wilson(tp, tp + fn),
        "spec": spec,
        "spec_95CI": wilson(tn, tn + fp),
        "LRpos": lrp,
        "LRneg": lrn,
        "post_pos": {p: post(p, lrp) for p in pretest},
        "post_neg": {p: post(p, lrn) for p in pretest},
    }
