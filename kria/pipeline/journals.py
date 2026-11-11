"""Map journal names from data/ to exact NLM Catalog records.

Queries use the NLM title abbreviation with the [ta] field, which matches one journal exactly.
Run `python -m kria.pipeline.journals` after editing journal names in taxonomy.yaml or
sources.yaml; it rewrites data/journals.yaml. Names that do not match exactly are listed with
candidates and must be fixed in the `overrides` section of that file by a person.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import yaml

from kria.config import settings
from kria.pipeline.eutils import EUtils

DATA = Path(__file__).resolve().parents[2] / "data"
JOURNALS_FILE = DATA / "journals.yaml"


def load_yaml(name: str) -> dict:
    return yaml.safe_load((DATA / name).read_text(encoding="utf-8"))


def journal_names(taxonomy: dict, sources: dict) -> list[str]:
    """Every journal named in the taxonomy and in the shared pools, without duplicates."""
    names: list[str] = []
    for pool in ("core_em_journals", "general_journals"):
        for tier in sources[pool].values():
            names.extend(tier)
    for topic in taxonomy["topics"]:
        names.extend(topic["journals"])
    return list(dict.fromkeys(names))


def clean(title: str) -> str:
    """Lowercase, no punctuation or qualifiers, "&" spelled "and", no leading "the"."""
    t = title.lower()
    t = re.sub(r"\([^)]*\)", " ", t)  # "Lancet (London, England)" -> "lancet"
    t = t.replace("&", " and ")
    t = re.sub(r"[^\w\s]", " ", t)
    t = re.sub(r"^\s*the\s+", "", t)
    return " ".join(t.split())


def normalize(title: str) -> str:
    """Comparison key. NLM sort titles drop "&", so "and" is dropped on both sides."""
    return " ".join(w for w in clean(title).split() if w != "and")


def _record(rec: dict) -> dict:
    return {
        "nlm_id": rec["nlmuniqueid"],
        "medline_ta": rec.get("medlineta", ""),
        "title": rec.get("titlemainsort", ""),
        "issn": sorted({i["issn"] for i in rec.get("issnlist", []) if i.get("issn")}),
        "medline_indexed": rec.get("currentindexingstatus") == "Y",
        "current": rec.get("endyear", "").strip() in ("", "9999"),
    }


def match_rank(wanted: str, rec: dict) -> int | None:
    """0 = current main title equals the name, 1 = main title is the name plus a subtitle.

    Former titles and related titles are deliberately ignored: matching on them maps a journal
    to its predecessor or to a sister journal.
    """
    if not rec["current"] or not rec["medline_ta"]:
        return None
    main = normalize(rec["title"])
    if main == wanted:
        return 0
    if main.startswith(wanted + " "):
        return 1
    return None


def resolve(eu: EUtils, name: str) -> dict:
    wanted = normalize(name)
    # The [Title] index misses some titles that start with "The", so three forms are combined.
    c = clean(name)
    term = f'("{c}"[Title] OR "the {c}"[Title] OR "{c}"[All Fields]) AND ncbijournals[filter]'
    ids, _ = eu.search("nlmcatalog", term)
    cands: list[dict] = []
    for i in range(0, min(len(ids), 3000), 200):
        recs = eu.summary("nlmcatalog", ids[i : i + 200])
        cands += [_record(recs[u]) for u in recs.get("uids", [])]
    ranked = sorted(
        (
            (r, not c["medline_indexed"], c)
            for c in cands
            if (r := match_rank(wanted, c)) is not None
        ),
        key=lambda t: (t[0], t[1]),
    )
    if not ranked:
        return {"status": "not_found", "candidates": cands[:5]}
    if ranked[0][0] == 0:
        # Exact title: when several share it (e.g. an overseas edition), prefer MEDLINE-indexed.
        best = [c for r, m, c in ranked if (r, m) == ranked[0][:2]]
    else:
        # Title plus subtitle: accepted only if it is the only such journal, because a longer
        # title can also be a sister journal ("Injury" vs "Injury prevention").
        best = [c for r, m, c in ranked if r == 1]
    if len(best) > 1:
        return {"status": "ambiguous", "candidates": best[:5]}
    return {"status": "ok", **best[0], "candidates": []}


def build(eu: EUtils) -> dict:
    taxonomy, sources = load_yaml("taxonomy.yaml"), load_yaml("sources.yaml")
    old = (
        yaml.safe_load(JOURNALS_FILE.read_text(encoding="utf-8")) if JOURNALS_FILE.exists() else {}
    )
    overrides = (old or {}).get("overrides") or {}
    journals, problems = {}, {}
    for name in journal_names(taxonomy, sources):
        if name in overrides:
            journals[name] = overrides[name] | {"source": "override"}
            continue
        r = resolve(eu, name)
        if r["status"] == "ok":
            r.pop("candidates")
            journals[name] = r | {"source": "nlm_catalog"}
        else:
            problems[name] = r
    return {"journals": journals, "problems": problems, "overrides": overrides}


def main() -> int:
    eu = EUtils(api_key=settings.ncbi_api_key, email=settings.ncbi_email, tool=settings.ncbi_tool)
    out = build(eu)
    header = (
        "# Generated by `python -m kria.pipeline.journals` from the NLM Catalog. Do not edit\n"
        "# `journals` or `problems` by hand: fix a problem by adding the journal to `overrides`\n"
        "# (nlm_id, medline_ta, title) and running the command again.\n"
    )
    body = yaml.safe_dump(out, allow_unicode=True, sort_keys=False, width=100)
    JOURNALS_FILE.write_text(header + body, encoding="utf-8")
    print(f"{len(out['journals'])} journals resolved, {len(out['problems'])} need a decision")
    for name, p in out["problems"].items():
        print(f"- {name}: {p['status']}")
        for c in p["candidates"]:
            print(f"    {c['nlm_id']}  {c['medline_ta']!r}  {c['title']}  current={c['current']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
