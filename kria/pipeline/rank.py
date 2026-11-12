"""Rank screened articles into the editor's shortlist. Deterministic code, no model.

High yield = the highest of the three scores (practice-changing, board exam, journal club).
Ties are broken, in order, by connection to ED work (ed_relevance, from screening.v2: studies
from other specialties rank below ED studies with the same score; Yoav, 2026-11-12), study
design, journal tier, the sum of the three scores, and PMID (so the order never changes).
The spec's other tie-breakers, FOAMed coverage and past curator ratings, are added when those
signals exist (FOAMed feeds and M5 ratings).
Only articles screened as relevant are ranked.
"""

from __future__ import annotations

from functools import cache

from kria.db.models import Article, Screening

DESIGN_TIER = {
    **dict.fromkeys(["meta_analysis", "systematic_review", "rct", "cluster_rct", "guideline"], 0),
    **dict.fromkeys(
        ["non_randomized_trial", "prospective_cohort", "diagnostic_accuracy", "prediction_rule"], 1
    ),
    **dict.fromkeys(
        [
            "secondary_analysis",
            "retrospective_cohort",
            "registry",
            "case_control",
            "cross_sectional",
            "economic_evaluation",
            "quality_improvement",
            "education_study",
            "simulation_study",
            "qualitative",
        ],
        2,
    ),
}  # anything else (narrative review, case report, basic science, protocol, other): 3


@cache
def journal_tiers() -> dict[str, int]:
    """MEDLINE abbreviation -> 0 (core EM and core general journals), 1 (other pool journals)."""
    from kria.pipeline.ingest import load_data

    data = load_data()
    journals, tiers = data["journals"], {}
    for section in ("core_em_journals", "general_journals"):
        for tier_name, names in data["sources"][section].items():
            for name in names:
                ta = journals[name]["medline_ta"]
                tiers[ta] = min(tiers.get(ta, 2), 0 if tier_name == "core" else 1)
    return tiers


def sort_key(article: Article, s: Screening, tiers: dict[str, int]) -> tuple:
    total = s.practice_changing + s.board_exam + s.journal_club
    return (
        -s.high_yield_score,
        -(s.ed_relevance or 0),
        DESIGN_TIER.get(s.design, 3),
        tiers.get(article.journal_ta, 2),
        -total,
        int(article.pmid),
    )


def shortlist(
    pairs: list[tuple[Article, Screening]],
    size: int | None = 40,
    topics: set[str] | None = None,
    tiers: dict[str, int] | None = None,
) -> list[dict]:
    """Ranked relevant articles, optionally only those with one of `topics`."""
    tiers = journal_tiers() if tiers is None else tiers
    chosen = [
        (a, s)
        for a, s in pairs
        if s.status == "ok" and s.relevant and (not topics or set(s.topics) & topics)
    ]
    chosen.sort(key=lambda p: sort_key(p[0], p[1], tiers))
    if size is not None:
        chosen = chosen[:size]
    return [
        {
            "rank": i,
            "pmid": a.pmid,
            "doi": a.doi,
            "title": a.title,
            "journal": a.journal_ta,
            "score": s.high_yield_score,
            "scores": {
                "practice_changing": s.practice_changing,
                "board_exam": s.board_exam,
                "journal_club": s.journal_club,
            },
            "reasons": {
                "practice_changing": s.practice_changing_reason,
                "board_exam": s.board_exam_reason,
                "journal_club": s.journal_club_reason,
            },
            "ed": (
                {"score": s.ed_relevance, "reason": s.ed_relevance_reason}
                if s.ed_relevance is not None
                else None
            ),
            "design": s.design,
            "topics": list(s.topics),
            "summary": s.summary,
            "no_abstract": "no_abstract" in (a.flags or []),
            "screening_id": s.id,
        }
        for i, (a, s) in enumerate(chosen, 1)
    ]
