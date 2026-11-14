"""Code checks on model output: quotes against their anchors, and numbers against the sources.

Every number in an appraisal must come from the article or from kria/appraisal/calc.py. These
checks do not trust the model: a quote is verified only if it is found, after normalising
spacing, dashes and similar typography, in the unit (page, section or table) it cites.
Numbers in the report that appear neither in the article nor in the computed statistics are
listed for the editor.
"""

from __future__ import annotations

import re
import unicodedata

from kria.appraisal.fulltext import Unit

DASHES = dict.fromkeys(map(ord, "‐‑‒–—―−"), "-")
NUMBER = re.compile(r"(?<![\w.])-?\d+(?:[.,]\d+)*(?:\.\d+)?")


def normalise(text: str) -> str:
    text = unicodedata.normalize("NFKC", text or "").translate(DASHES)
    text = text.replace(" ", " ").replace("·", ".").lower()
    text = re.sub(r"-\s+", "-", text)  # words hyphenated across a PDF line break
    return " ".join(text.split())


def _squash(text: str) -> str:
    return re.sub(r"[^a-z0-9.%<>=-]", "", text)


def find_quote(quote: str, units: list[Unit], anchor: str | None) -> dict:
    """Is the quote on the cited page/section? If not, is it anywhere in the article?"""
    q = normalise(quote)
    if len(q) < 4:
        return {"verified": False, "where": None, "reason": "quote too short"}
    qs = _squash(q)

    def contains(unit: Unit) -> bool:
        t = normalise(unit.text)
        return q in t or (len(qs) >= 8 and qs in _squash(t))

    cited = [u for u in units if anchor and normalise(u.anchor) == normalise(anchor)]
    if any(contains(u) for u in cited):
        return {"verified": True, "where": cited[0].anchor, "reason": "found at the cited place"}
    elsewhere = next((u for u in units if contains(u)), None)
    if elsewhere is not None:
        return {
            "verified": False,
            "where": elsewhere.anchor,
            "reason": f"found at {elsewhere.anchor}, not at the cited {anchor or 'place'}",
        }
    return {"verified": False, "where": None, "reason": "not found in the article"}


def numbers_in(text: str) -> set[str]:
    """Numbers in a text, as canonical strings ("1,234" -> "1234", "0.50" -> "0.5")."""
    out = set()
    for m in NUMBER.findall(normalise(text)):
        raw = m.replace(",", "") if re.fullmatch(r"-?\d{1,3}(,\d{3})+", m) else m.replace(",", ".")
        try:
            value = float(raw)
        except ValueError:
            continue
        out.add(_canon(value))
    return out


def _canon(value: float) -> str:
    return f"{value:.4f}".rstrip("0").rstrip(".") if value % 1 else str(int(value))


def stat_numbers(stats: list[dict]) -> set[str]:
    """Every number in the computed statistics, at the precisions a report may round them to."""
    out: set[str] = set()

    def add(v):
        if isinstance(v, bool) or v is None:
            return
        if isinstance(v, int | float):
            if v != v or v in (float("inf"), float("-inf")):
                return
            out.add(_canon(float(v)))
            for digits in (0, 1, 2, 3):
                out.add(_canon(round(float(v), digits)))
                out.add(_canon(round(float(v) * 100, digits)))  # as a percentage
            out.add(_canon(abs(float(v))))
        elif isinstance(v, dict):
            for x in v.values():
                add(x)
        elif isinstance(v, list | tuple):
            for x in v:
                add(x)

    for s in stats:
        add(s)
    return out


def unsupported_numbers(report_texts: list[str], source: str, stats: list[dict]) -> list[str]:
    """Numbers in the report found neither in the article nor in the computed statistics."""
    allowed = numbers_in(source) | stat_numbers(stats)
    found: set[str] = set()
    for t in report_texts:
        found |= numbers_in(t)
    small = {str(i) for i in range(0, 11)}  # counts like "two trials", list numbering, 1-10
    return sorted(found - allowed - small, key=lambda x: float(x))
