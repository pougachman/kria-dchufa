"""Monthly PubMed ingestion (milestone M1).

    python -m kria.pipeline.ingest 2026-10

For one calendar month this:
1. builds one versioned PubMed query per source: each of the 33 topics (its own journals), the
   core EM journal pool and the general journal pool;
2. retrieves every record PubMed *created* in that month ([crdt]). The creation date never changes,
   so re-running a month returns the same set of records;
3. parses them, applies the code filter in data/filters.yaml, and removes DOI duplicates;
4. stores articles, which query found each one, and a per-source count report.

Topic assignment by content, screening and scoring are M2.
"""

from __future__ import annotations

import argparse
import calendar
import datetime as dt
import hashlib
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session

from kria.config import settings
from kria.db.models import Article, IngestRun, QueryVersion, RunHit, Topic
from kria.pipeline import pubmed_xml
from kria.pipeline.eutils import EUtils

DATA = Path(__file__).resolve().parents[2] / "data"
POOLS = {
    "pool:core-em": ("core_em_journals", "כתבי עת מרכזיים ברפואה דחופה"),
    "pool:general": ("general_journals", "כתבי עת כלליים"),
}
ARTICLE_FIELDS = (
    "doi",
    "pmcid",
    "title",
    "abstract",
    "journal",
    "journal_ta",
    "nlm_id",
    "pub_types",
    "pub_date",
    "entrez_date",
    "latest_pub_date",
    "languages",
    "authors",
    "mesh",
    "keywords",
    "flags",
)


def load_data(data_dir: Path = DATA) -> dict:
    def load(name: str) -> dict:
        return yaml.safe_load((data_dir / name).read_text(encoding="utf-8"))

    return {
        "taxonomy": load("taxonomy.yaml"),
        "sources": load("sources.yaml"),
        "journals": load("journals.yaml")["journals"],
        "filters": load("filters.yaml"),
    }


def parse_month(month: str) -> tuple[dt.date, dt.date]:
    try:
        first = dt.datetime.strptime(month, "%Y-%m").date()
    except ValueError as exc:
        raise ValueError(f"month must look like 2026-10, got {month!r}") from exc
    last = first.replace(day=calendar.monthrange(first.year, first.month)[1])
    return first, last


def date_clause(month: str) -> str:
    first, last = parse_month(month)
    return f'("{first:%Y/%m/%d}"[crdt] : "{last:%Y/%m/%d}"[crdt])'


def journal_query(abbreviations: list[str]) -> str:
    return "(" + " OR ".join(f'"{ta}"[ta]' for ta in abbreviations) + ")"


def build_sources(data: dict) -> dict[str, dict]:
    """Source key -> label, journal abbreviations, and the query without the date clause."""
    journals = data["journals"]

    def tas(names: list[str]) -> list[str]:
        missing = [n for n in names if n not in journals]
        if missing:
            raise KeyError(f"not in data/journals.yaml (run kria.pipeline.journals): {missing}")
        return sorted({journals[n]["medline_ta"] for n in names})

    out: dict[str, dict] = {}
    for key, (section, label) in POOLS.items():
        names = [n for tier in data["sources"][section].values() for n in tier]
        out[key] = {"label": label, "journals": tas(names)}
    for topic in data["taxonomy"]["topics"]:
        out[f"topic:{topic['id']}"] = {"label": topic["he"], "journals": tas(topic["journals"])}
    for src in out.values():
        src["query"] = journal_query(src["journals"])
    return out


def months_before(day: dt.date, months: int) -> dt.date:
    """First day of the month `months` before the month of `day`."""
    index = day.year * 12 + day.month - 1 - months
    return dt.date(index // 12, index % 12 + 1, 1)


def exclusion_reason(rec: dict, filters: dict, month_start: dt.date | None = None) -> str | None:
    types = set(rec["pub_types"])
    for t in filters["always_exclude"]:
        if t in types:
            return f"type:{t}"
    if "RetractionIn" in rec["flags"]:
        return "retracted"
    if not rec["title"]:
        return "no_title"
    if filters.get("languages") and not set(rec["languages"]) & set(filters["languages"]):
        return "language"
    max_age = filters.get("max_age_months")
    if max_age and month_start and rec.get("latest_pub_date"):
        if rec["latest_pub_date"] < months_before(month_start, max_age):
            return "old_publication"
    keep = bool(types & set(filters["keep_types"]))
    if not keep:
        for t in filters["exclude"]:
            if t in types:
                return f"type:{t}"
        if not rec["abstract"]:
            if filters.get("exclude_no_abstract"):
                return "no_abstract"
            for pattern in filters.get("title_exclude_if_no_abstract", []):
                if re.match(pattern, rec["title"], re.IGNORECASE):
                    return "title:letter_or_correction"
    return None


def sync_topics(session: Session, taxonomy: dict) -> None:
    existing = {t.id: t for t in session.scalars(select(Topic))}
    for t in taxonomy["topics"]:
        row = existing.get(t["id"]) or Topic(id=t["id"])
        row.name_he, row.group = t["he"], t["group"]
        row.suggested, row.active = bool(t.get("suggested")), True
        session.add(row)
    for tid, row in existing.items():
        if tid not in {t["id"] for t in taxonomy["topics"]}:
            row.active = False


def query_version(session: Session, key: str, src: dict) -> QueryVersion:
    sha = hashlib.sha256(src["query"].encode()).hexdigest()
    qv = session.scalar(
        select(QueryVersion).where(QueryVersion.source_key == key, QueryVersion.sha256 == sha)
    )
    if qv:
        return qv
    latest = session.scalar(
        select(QueryVersion.version)
        .where(QueryVersion.source_key == key)
        .order_by(QueryVersion.version.desc())
    )
    qv = QueryVersion(
        source_key=key,
        version=(latest or 0) + 1,
        query=src["query"],
        journals=src["journals"],
        sha256=sha,
    )
    session.add(qv)
    session.flush()
    return qv


def _chunks(items: list, n: int = 500):
    for i in range(0, len(items), n):
        yield items[i : i + n]


def upsert_articles(session: Session, records: list[dict], run: IngestRun) -> dict[str, Article]:
    by_pmid: dict[str, Article] = {}
    pmids = [r["pmid"] for r in records]
    for chunk in _chunks(pmids):
        for a in session.scalars(select(Article).where(Article.pmid.in_(chunk))):
            by_pmid[a.pmid] = a
    for rec in records:
        art = by_pmid.get(rec["pmid"])
        if art is None:
            art = Article(pmid=rec["pmid"], first_seen_run_id=run.id)
            session.add(art)
            by_pmid[rec["pmid"]] = art
        for f in ARTICLE_FIELDS:
            setattr(art, f, rec[f])
        art.excluded_reason = rec["excluded_reason"]
        art.duplicate_of_id = None
    session.flush()
    return by_pmid


def mark_doi_duplicates(session: Session, articles: dict[str, Article]) -> int:
    """Two PubMed records with one DOI: keep the earliest PMID, mark the rest as duplicates.

    Also checks records stored in earlier months, so a re-indexed article is not screened twice.
    """
    groups: dict[str, list[Article]] = defaultdict(list)
    for a in articles.values():
        if a.doi and a.excluded_reason is None:
            groups[a.doi].append(a)
    dois = list(groups)
    for chunk in _chunks(dois):
        rows = session.scalars(
            select(Article).where(Article.doi.in_(chunk), Article.pmid.not_in(list(articles)))
        )
        for a in rows:
            if a.excluded_reason is None:
                groups[a.doi].append(a)
    marked = 0
    for group in groups.values():
        if len(group) < 2:
            continue
        group.sort(key=lambda a: int(a.pmid))
        for dup in group[1:]:
            if dup.pmid in articles:
                dup.excluded_reason, dup.duplicate_of_id = "duplicate_doi", group[0].id
                marked += 1
    return marked


def _previous_hits(session: Session, run: IngestRun) -> tuple[int | None, dict[str, set[str]]]:
    prev = session.scalar(
        select(IngestRun)
        .where(
            IngestRun.month == run.month,
            IngestRun.status == "ok",
            IngestRun.filter_version == run.filter_version,
            IngestRun.id != run.id,
        )
        .order_by(IngestRun.id.desc())
    )
    if prev is None:
        return None, {}
    hits: dict[str, set[str]] = defaultdict(set)
    rows = session.execute(
        select(QueryVersion.source_key, Article.pmid)
        .join(RunHit, RunHit.query_version_id == QueryVersion.id)
        .join(Article, Article.id == RunHit.article_id)
        .where(RunHit.run_id == prev.id)
    )
    for key, pmid in rows:
        hits[key].add(pmid)
    return prev.id, hits


def ingest_month(session: Session, eu: EUtils, month: str, data: dict | None = None) -> IngestRun:
    data = data or load_data()
    clause = date_clause(month)
    run = IngestRun(
        month=month,
        started_at=dt.datetime.now(dt.UTC),
        status="running",
        filter_version=data["filters"]["version"],
    )
    session.add(run)
    session.commit()
    try:
        sync_topics(session, data["taxonomy"])
        sources = build_sources(data)
        found: dict[str, list[str]] = {}
        versions: dict[str, QueryVersion] = {}
        translations: dict[str, str] = {}
        for key, src in sources.items():
            versions[key] = query_version(session, key, src)
            found[key], translations[key] = eu.search("pubmed", f"{src['query']} AND {clause}")

        all_pmids = sorted({p for ids in found.values() for p in ids}, key=int)
        records = []
        for xml in eu.fetch_pubmed_xml(all_pmids):
            records.extend(pubmed_xml.parse(xml))
        missing = sorted(set(all_pmids) - {r["pmid"] for r in records}, key=int)
        month_start = parse_month(month)[0]
        for rec in records:
            rec["excluded_reason"] = exclusion_reason(rec, data["filters"], month_start)

        articles = upsert_articles(session, records, run)
        duplicates = mark_doi_duplicates(session, articles)
        for key, pmids in found.items():
            for p in pmids:
                if p in articles:
                    session.add(
                        RunHit(
                            run_id=run.id,
                            article_id=articles[p].id,
                            query_version_id=versions[key].id,
                        )
                    )

        prev_id, prev_hits = _previous_hits(session, run)
        run.report = build_report(
            month,
            sources,
            versions,
            found,
            articles,
            missing,
            duplicates,
            prev_id,
            prev_hits,
            translations,
        )
        run.status, run.finished_at = "ok", dt.datetime.now(dt.UTC)
        session.commit()
    except Exception as exc:
        session.rollback()
        run.status, run.error, run.finished_at = "failed", repr(exc), dt.datetime.now(dt.UTC)
        session.add(run)
        session.commit()
        raise
    return run


def build_report(
    month, sources, versions, found, articles, missing, duplicates, prev_id, prev_hits, translations
) -> dict:
    per_source, mismatches, per_journal = {}, [], defaultdict(lambda: {"found": 0, "kept": 0})
    for key, src in sources.items():
        hits = [articles[p] for p in found[key] if p in articles]
        allowed = set(src["journals"])
        mismatches += [a.pmid for a in hits if a.journal_ta not in allowed]
        per_source[key] = {
            "label": src["label"],
            "query_version": versions[key].version,
            "found": len(found[key]),
            "kept": sum(a.excluded_reason is None for a in hits),
            "zero_hit_journals": sorted(allowed - {a.journal_ta for a in hits}),
            "pubmed_translation": translations[key],
        }
    for a in articles.values():
        per_journal[a.journal_ta]["found"] += 1
        per_journal[a.journal_ta]["kept"] += a.excluded_reason is None
    reasons = Counter(a.excluded_reason for a in articles.values() if a.excluded_reason)
    comparison = None
    if prev_id is not None:
        now = {k: set(v) for k, v in found.items()}
        changed = {
            k: {
                "added": sorted(now.get(k, set()) - prev_hits.get(k, set()))[:20],
                "removed": sorted(prev_hits.get(k, set()) - now.get(k, set()))[:20],
            }
            for k in set(now) | set(prev_hits)
            if now.get(k, set()) != prev_hits.get(k, set())
        }
        comparison = {"previous_run_id": prev_id, "identical": not changed, "changed": changed}
    return {
        "month": month,
        "total_unique": len(articles),
        "kept": sum(a.excluded_reason is None for a in articles.values()),
        "excluded": dict(reasons.most_common()),
        "duplicates_marked": duplicates,
        "not_returned_by_efetch": missing,
        "journal_mismatches": sorted(set(mismatches)),
        "sources": per_source,
        "journals": dict(sorted(per_journal.items(), key=lambda kv: -kv[1]["kept"])),
        "comparison": comparison,
    }


def format_report(report: dict) -> str:
    lines = [
        f"Month {report['month']}: {report['total_unique']} unique records, "
        f"{report['kept']} kept for screening",
        "",
        f"{'source':34} {'found':>6} {'kept':>6}  zero-hit journals",
    ]
    for key, s in report["sources"].items():
        zero = ", ".join(s["zero_hit_journals"])
        lines.append(f"{key:34} {s['found']:>6} {s['kept']:>6}  {zero}")
    lines += ["", "Excluded by the code filter:"]
    lines += [f"  {reason:32} {n:>6}" for reason, n in report["excluded"].items()]
    lines.append(f"DOI duplicates marked: {report['duplicates_marked']}")
    if report["not_returned_by_efetch"]:
        lines.append(f"PMIDs found but not returned by efetch: {report['not_returned_by_efetch']}")
    if report["journal_mismatches"]:
        lines.append(f"Journal mismatches (check queries): {report['journal_mismatches'][:20]}")
    cmp = report["comparison"]
    if cmp is None:
        lines.append("First run for this month.")
    elif cmp["identical"]:
        lines.append(f"Same set of records as run {cmp['previous_run_id']}.")
    else:
        lines.append(f"Differs from run {cmp['previous_run_id']}: {cmp['changed']}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Ingest one month of PubMed records.")
    parser.add_argument("month", help="calendar month, for example 2026-10")
    args = parser.parse_args(argv)
    parse_month(args.month)

    from kria.db.base import SessionLocal

    eu = EUtils(api_key=settings.ncbi_api_key, email=settings.ncbi_email, tool=settings.ncbi_tool)
    with SessionLocal() as session:
        run = ingest_month(session, eu, args.month)
        print(format_report(run.report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
