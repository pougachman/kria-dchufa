"""LLM screening and scoring (milestone M2).

    python -m kria.pipeline.screen run 2026-10            # submit, wait, collect, print report
    python -m kria.pipeline.screen run 2026-10 --limit 20 # small paid trial first
    python -m kria.pipeline.screen submit 2026-10         # submit only (batch runs up to 24 h)
    python -m kria.pipeline.screen collect                # collect the latest submitted run
    python -m kria.pipeline.screen report 2026-10         # report and ranked shortlist

Every article the code filter kept in the month's latest ingest run is sent to Claude Haiku
through the Message Batches API with the versioned prompt in prompts/. The model returns JSON in
a fixed schema: relevance, 1-3 topics, design, three 1-5 scores with one-sentence reasons, and a
short summary. Code validates every answer; nothing the model writes becomes a reference (the
article is always the PubMed record it was asked about). Each result is stored with the model
name and prompt version, and each run with its tokens, cost and time.

Re-running a month screens only the articles that have no valid screening for this model and
prompt version yet (new ones, or ones that failed), up to MAX_ATTEMPTS per article.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import sys
import time
from collections import Counter
from pathlib import Path

import yaml
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from kria.config import settings
from kria.db.models import Article, IngestRun, QueryVersion, RunHit, Screening, ScreeningRun
from kria.pipeline.batches import USAGE_FIELDS, as_utc

ROOT = Path(__file__).resolve().parents[2]
PROMPTS = ROOT / "prompts"
DATA = ROOT / "data"

SCHEMA_VERSION = 1
MAX_TOKENS = 4096  # thinking counts toward this; screening answers are ~300 tokens
EFFORT = "low"
MAX_ATTEMPTS = 3
MAX_BATCH = 100_000  # API limit per batch

DESIGNS = [
    "rct",
    "cluster_rct",
    "non_randomized_trial",
    "meta_analysis",
    "systematic_review",
    "guideline",
    "prospective_cohort",
    "retrospective_cohort",
    "registry",
    "case_control",
    "cross_sectional",
    "diagnostic_accuracy",
    "prediction_rule",
    "secondary_analysis",
    "economic_evaluation",
    "qualitative",
    "quality_improvement",
    "education_study",
    "simulation_study",
    "narrative_review",
    "case_report_or_series",
    "basic_science",
    "protocol",
    "other",
]
SCORES = ("practice_changing", "board_exam", "journal_club")
FAILED_STATUSES = ("invalid", "errored", "refused", "max_tokens", "expired", "canceled")


# ---- prompt and schema -----------------------------------------------------------------------


def load_taxonomy(data_dir: Path = DATA) -> dict:
    return yaml.safe_load((data_dir / "taxonomy.yaml").read_text(encoding="utf-8"))


def load_pricing(data_dir: Path = DATA) -> dict:
    return yaml.safe_load((data_dir / "pricing.yaml").read_text(encoding="utf-8"))


def render_prompt(version: str, taxonomy: dict, prompts_dir: Path | None = None) -> str:
    template = ((prompts_dir or PROMPTS) / f"{version}.md").read_text(encoding="utf-8")
    groups = taxonomy["groups"]
    lines = []
    for group in groups:
        lines.append(f"{group.capitalize()} topics:")
        lines += [f"- {t['id']}: {t['en']}" for t in taxonomy["topics"] if t["group"] == group]
        lines.append("")
    if "{{topics}}" not in template:
        raise ValueError(f"prompt {version} has no {{{{topics}}}} placeholder")
    return template.replace("{{topics}}", "\n".join(lines).rstrip())


def output_schema(topic_ids: list[str]) -> dict:
    """JSON schema for structured outputs. Score ranges are enums (no numeric constraints)."""

    def score() -> dict:
        return {"type": "integer", "enum": [1, 2, 3, 4, 5]}

    props: dict = {
        "relevant": {"type": "boolean"},
        "topics": {"type": "array", "items": {"type": "string", "enum": topic_ids}},
        "design": {"type": "string", "enum": DESIGNS},
    }
    for s in SCORES:
        props[s] = score()
        props[f"{s}_reason"] = {"type": "string"}
    props["summary"] = {"type": "string"}
    return {
        "type": "object",
        "properties": props,
        "required": list(props),
        "additionalProperties": False,
    }


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


# ---- which articles ----------------------------------------------------------------------------


def latest_ingest_run(session: Session, month: str) -> IngestRun | None:
    return session.scalar(
        select(IngestRun)
        .where(IngestRun.month == month, IngestRun.status == "ok")
        .order_by(IngestRun.id.desc())
    )


def kept_articles(session: Session, ingest_run: IngestRun) -> list[Article]:
    """Articles found in this ingest run that the code filter kept, in PMID order."""
    rows = session.scalars(
        select(Article)
        .join(RunHit, RunHit.article_id == Article.id)
        .where(RunHit.run_id == ingest_run.id, Article.excluded_reason.is_(None))
        .distinct()
    )
    return sorted(rows, key=lambda a: int(a.pmid))


def source_keys(session: Session, ingest_run: IngestRun) -> dict[int, list[str]]:
    """Article id -> the source keys (topic:x, pool:y) whose query found it in this run."""
    out: dict[int, list[str]] = {}
    rows = session.execute(
        select(RunHit.article_id, QueryVersion.source_key)
        .join(QueryVersion, QueryVersion.id == RunHit.query_version_id)
        .where(RunHit.run_id == ingest_run.id)
    )
    for article_id, key in rows:
        out.setdefault(article_id, []).append(key)
    return {k: sorted(set(v)) for k, v in out.items()}


def articles_to_screen(
    session: Session, articles: list[Article], model: str, prompt_version: str
) -> list[Article]:
    """Articles without a valid screening for this model and prompt, with attempts left."""
    ids = [a.id for a in articles]
    done, attempts = set(), Counter()
    for i in range(0, len(ids), 500):
        chunk = ids[i : i + 500]
        rows = session.execute(
            select(Screening.article_id, Screening.status).where(
                Screening.article_id.in_(chunk),
                Screening.model == model,
                Screening.prompt_version == prompt_version,
            )
        )
        for article_id, status in rows:
            attempts[article_id] += 1
            if status == "ok":
                done.add(article_id)
    return [a for a in articles if a.id not in done and attempts[a.id] < MAX_ATTEMPTS]


# ---- request building --------------------------------------------------------------------------


def custom_id(article: Article) -> str:
    return f"pmid-{article.pmid}"


def article_text(article: Article, keys: list[str], topic_names: dict[str, str]) -> str:
    hints = []
    for key in keys:
        if key.startswith("topic:"):
            tid = key.split(":", 1)[1]
            hints.append(f"{tid} ({topic_names.get(tid, tid)})")
    pools = [k for k in keys if k.startswith("pool:")]
    if "pool:core-em" in pools:
        hints.append("core emergency medicine journal (any topic)")
    if "pool:general" in pools:
        hints.append("general medical journal (any topic)")
    lines = [
        "<article>",
        f"PMID: {article.pmid}",
        f"Title: {article.title}",
        f"Journal: {article.journal} ({article.journal_ta})",
        f"Publication types: {', '.join(article.pub_types) or 'not given'}",
        f"Published: {article.pub_date or 'not given'}",
        f"Journal's usual topics (hint only): {'; '.join(hints) or 'none'}",
        f"MeSH terms: {', '.join(article.mesh) or 'none yet'}",
        f"Keywords: {', '.join(article.keywords) or 'none'}",
    ]
    if article.abstract:
        lines.append(f"Abstract: {article.abstract}")
    else:
        lines.append("Abstract: none (not available in PubMed yet)")
    lines.append("</article>")
    return "\n".join(lines)


def build_request(article: Article, text: str, system: str, schema: dict, model: str) -> dict:
    return {
        "custom_id": custom_id(article),
        "params": {
            "model": model,
            "max_tokens": MAX_TOKENS,
            "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            "messages": [{"role": "user", "content": text}],
            "output_config": {
                "effort": EFFORT,
                "format": {"type": "json_schema", "schema": schema},
            },
        },
    }


# ---- validation and cost -----------------------------------------------------------------------


def validate_output(text: str | None, topic_ids: set[str]) -> tuple[dict | None, list[str]]:
    """Check the model's JSON in code. Returns (clean values, problems)."""
    if not text:
        return None, ["empty output"]
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        return None, [f"not JSON: {exc}"]
    if not isinstance(data, dict):
        return None, ["not a JSON object"]
    problems = []
    if not isinstance(data.get("relevant"), bool):
        problems.append("relevant is not true/false")
    topics = data.get("topics")
    if not isinstance(topics, list):
        problems.append("topics is not a list")
        topics = []
    topics = list(dict.fromkeys(t for t in topics if isinstance(t, str)))
    unknown = [t for t in topics if t not in topic_ids]
    if unknown:
        problems.append(f"unknown topics {unknown}")
    if not topics:
        problems.append("no topic")
    if data.get("design") not in DESIGNS:
        problems.append(f"unknown design {data.get('design')!r}")
    for s in SCORES:
        v = data.get(s)
        if isinstance(v, bool) or not isinstance(v, int) or not 1 <= v <= 5:
            problems.append(f"{s} is not 1-5")
        reason = data.get(f"{s}_reason")
        if not isinstance(reason, str) or not reason.strip():
            problems.append(f"{s}_reason is empty")
    if not isinstance(data.get("summary"), str) or not data["summary"].strip():
        problems.append("summary is empty")
    if problems:
        return None, problems
    clean = {
        "relevant": data["relevant"],
        "topics": topics[:3],  # the prompt asks for 1-3, most fitting first
        "design": data["design"],
        "summary": data["summary"].strip(),
    }
    for s in SCORES:
        clean[s] = data[s]
        clean[f"{s}_reason"] = data[f"{s}_reason"].strip()
    return clean, []


def cost_usd(usage: dict, model: str, pricing: dict, batch: bool = True) -> float:
    """USD cost of one request's usage, from data/pricing.yaml."""
    if model not in pricing["models"]:
        raise KeyError(f"no price for {model} in data/pricing.yaml")
    p = pricing["models"][model]
    write, read = pricing["cache_write_multiplier"], pricing["cache_read_multiplier"]
    dollars = (
        usage.get("input_tokens", 0) * p["input"]
        + usage.get("cache_creation_input_tokens", 0) * p["input"] * write
        + usage.get("cache_read_input_tokens", 0) * p["input"] * read
        + usage.get("output_tokens", 0) * p["output"]
    ) / 1_000_000
    return dollars * (pricing["batch_discount"] if batch else 1.0)


# ---- submit and collect ------------------------------------------------------------------------


def pending_run(session: Session, month: str | None = None) -> ScreeningRun | None:
    q = select(ScreeningRun).where(ScreeningRun.status == "submitted")
    if month:
        q = q.where(ScreeningRun.month == month)
    return session.scalar(q.order_by(ScreeningRun.id.desc()))


def submit_month(
    session: Session,
    batches,
    month: str,
    limit: int | None = None,
    model: str | None = None,
    prompt_version: str | None = None,
) -> ScreeningRun | None:
    """Submit one batch for the month's unscreened articles. None if nothing is left to screen."""
    model = model or settings.screening_model
    prompt_version = prompt_version or settings.screening_prompt
    ingest = latest_ingest_run(session, month)
    if ingest is None:
        raise RuntimeError(f"no successful ingest run for {month}; run kria.pipeline.ingest first")
    if pending_run(session, month):
        raise RuntimeError(f"a screening batch for {month} is still running; collect it first")
    mark_interrupted(session, month)
    taxonomy = load_taxonomy()
    system = render_prompt(prompt_version, taxonomy)
    topic_ids = [t["id"] for t in taxonomy["topics"]]
    schema = output_schema(topic_ids)
    todo = articles_to_screen(session, kept_articles(session, ingest), model, prompt_version)
    if limit is not None:
        todo = todo[:limit]
    if not todo:
        return None
    if len(todo) > MAX_BATCH:
        raise RuntimeError(f"{len(todo)} requests is more than one batch allows ({MAX_BATCH})")
    keys = source_keys(session, ingest)
    names = {t["id"]: t["en"] for t in taxonomy["topics"]}
    requests = [
        build_request(a, article_text(a, keys.get(a.id, []), names), system, schema, model)
        for a in todo
    ]
    run = ScreeningRun(
        month=month,
        ingest_run_id=ingest.id,
        model=model,
        prompt_version=prompt_version,
        prompt_sha256=sha256(system),
        settings={
            "max_tokens": MAX_TOKENS,
            "effort": EFFORT,
            "schema_version": SCHEMA_VERSION,
            "schema_sha256": sha256(json.dumps(schema, sort_keys=True)),
            "taxonomy_version": taxonomy["version"],
            "batch": True,
        },
        status="submitted",
        submitted_at=dt.datetime.now(dt.UTC),
        requested=len(requests),
    )
    # Record the attempt before calling the API, so a failure (or a process that is stopped) is
    # visible on /status instead of leaving no trace.
    run.status = "submitting"
    session.add(run)
    session.commit()
    log(f"submitting {len(requests)} articles from {month} to {model} (run {run.id})")
    try:
        run.batch_id = batches.create(requests)
    except Exception as exc:
        run.status, run.error = "failed", f"batch not created: {exc!r}"[:2000]
        run.finished_at = dt.datetime.now(dt.UTC)
        session.commit()
        raise
    run.status = "submitted"
    session.commit()
    log(f"batch {run.batch_id} created")
    return run


def log(message: str) -> None:
    """Progress line for the job logs (Railway shows them per run)."""
    print(f"[{dt.datetime.now(dt.UTC):%H:%M:%S}] {message}", flush=True)


def mark_interrupted(session: Session, month: str) -> int:
    """Runs left in "submitting" were stopped before the batch was created: mark them failed."""
    stale = session.scalars(
        select(ScreeningRun).where(ScreeningRun.month == month, ScreeningRun.status == "submitting")
    ).all()
    for run in stale:
        run.status = "failed"
        run.error = "interrupted: the process stopped before the batch was created"
        run.finished_at = dt.datetime.now(dt.UTC)
    if stale:
        session.commit()
    return len(stale)


def collect(session: Session, batches, run: ScreeningRun) -> bool:
    """Store the results if the batch has ended. Returns False while it is still running.

    A run whose collection failed can be collected again by hand (`collect RUN_ID`): nothing was
    stored for it, and the API keeps batch results for 29 days.
    """
    if run.status == "ok":
        return True
    state = batches.status(run.batch_id)
    if state["status"] != "ended":
        log(f"batch {run.batch_id} is {state['status']}: {state['counts']}")
        return False
    log(f"batch {run.batch_id} ended; storing results")
    pricing = load_pricing()
    topic_ids = {t["id"] for t in load_taxonomy()["topics"]}
    by_cid = {
        f"pmid-{a.pmid}": a
        for a in session.scalars(
            select(Article)
            .join(RunHit, RunHit.article_id == Article.id)
            .where(RunHit.run_id == run.ingest_run_id)
            .distinct()
        )
    }
    totals = dict.fromkeys(USAGE_FIELDS, 0)
    cost, statuses, unknown = 0.0, Counter(), []
    try:
        for res in batches.results(run.batch_id):
            article = by_cid.get(res["custom_id"])
            if article is None:
                unknown.append(res["custom_id"])
                continue
            row = Screening(
                run_id=run.id,
                article_id=article.id,
                model=res["model"] or run.model,
                prompt_version=run.prompt_version,
                raw_output=res["text"],
                error=res["error"],
                **res["usage"],
            )
            for f in USAGE_FIELDS:
                totals[f] += res["usage"][f]
            cost += cost_usd(res["usage"], run.model, pricing, batch=True)
            if res["type"] != "succeeded":
                row.status = res["type"]  # errored | expired | canceled
            elif res["stop_reason"] == "refusal":
                row.status = "refused"
            elif res["stop_reason"] == "max_tokens":
                row.status = "max_tokens"
            else:
                clean, problems = validate_output(res["text"], topic_ids)
                if clean is None:
                    row.status, row.error = "invalid", "; ".join(problems)
                else:
                    row.status = "ok"
                    for k, v in clean.items():
                        setattr(row, k, v)
            statuses[row.status] += 1
            session.add(row)
        for f, v in totals.items():
            setattr(run, f, v)
        run.cost_usd = round(cost, 6)
        run.error = None
        run.succeeded = statuses["ok"]
        run.failed = sum(statuses.values()) - statuses["ok"]
        # Batch time from the API's own clock: created -> ended.
        run.submitted_at = as_utc(state["created_at"]) or run.submitted_at
        run.ended_at = as_utc(state["ended_at"])
        run.finished_at = dt.datetime.now(dt.UTC)
        run.status = "ok"
        session.flush()
        run.report = build_report(session, run, statuses, unknown)
        session.commit()
    except Exception as exc:
        session.rollback()
        run.status, run.error = "failed", repr(exc)  # results stay at the API
        run.finished_at = dt.datetime.now(dt.UTC)
        session.add(run)
        session.commit()
        raise
    return True


def wait_and_collect(session: Session, batches, run: ScreeningRun, poll_seconds: int = 60):
    while not collect(session, batches, run):
        time.sleep(poll_seconds)
    return run


# ---- report ------------------------------------------------------------------------------------


def latest_screenings(
    session: Session, month: str, model: str | None = None, prompt_version: str | None = None
) -> list[tuple[Article, Screening]]:
    """The newest valid screening of each article kept in the month's latest ingest run."""
    model = model or settings.screening_model
    prompt_version = prompt_version or settings.screening_prompt
    ingest = latest_ingest_run(session, month)
    if ingest is None:
        return []
    articles = {a.id: a for a in kept_articles(session, ingest)}
    newest = (
        select(func.max(Screening.id))
        .where(
            Screening.status == "ok",
            Screening.model == model,
            Screening.prompt_version == prompt_version,
        )
        .group_by(Screening.article_id)
    )
    rows = session.scalars(select(Screening).where(Screening.id.in_(newest)))
    return [(articles[s.article_id], s) for s in rows if s.article_id in articles]


def build_report(session: Session, run: ScreeningRun, statuses: Counter, unknown: list) -> dict:
    from kria.pipeline.rank import shortlist

    pairs = latest_screenings(session, run.month, run.model, run.prompt_version)
    ingest = session.get(IngestRun, run.ingest_run_id)
    kept = len(kept_articles(session, ingest))
    relevant = [s for _, s in pairs if s.relevant]
    topics = Counter(t for s in relevant for t in s.topics)
    duration = None
    if run.ended_at:
        duration = (as_utc(run.ended_at) - as_utc(run.submitted_at)).total_seconds()
    return {
        "month": run.month,
        "model": run.model,
        "prompt_version": run.prompt_version,
        "this_run": {
            "requested": run.requested,
            "results": dict(statuses.most_common()),
            "unknown_custom_ids": unknown[:20],
            "input_tokens": run.input_tokens,
            "output_tokens": run.output_tokens,
            "cache_creation_input_tokens": run.cache_creation_input_tokens,
            "cache_read_input_tokens": run.cache_read_input_tokens,
            "cost_usd": run.cost_usd,
            "batch_seconds": duration,
        },
        "month_totals": {
            "kept_by_code_filter": kept,
            "screened_ok": len(pairs),
            "not_yet_screened": kept - len(pairs),
            "relevant": len(relevant),
            "high_yield_4_plus": sum(1 for s in relevant if s.high_yield_score >= 4),
            "score_distribution": {
                s: dict(sorted(Counter(getattr(x, s) for x in relevant).items())) for s in SCORES
            },
            "designs": dict(Counter(s.design for s in relevant).most_common()),
            "topics": dict(topics.most_common()),
        },
        "shortlist": [
            {k: item[k] for k in ("rank", "pmid", "score", "design", "topics", "title")}
            for item in shortlist(pairs, size=40)
        ],
    }


def format_report(report: dict) -> str:
    r, m = report["this_run"], report["month_totals"]
    lines = [
        f"Screening {report['month']} with {report['model']} / {report['prompt_version']}",
        f"This run: {r['requested']} requested, results {r['results']}",
        f"Tokens: {r['input_tokens']} input, {r['cache_read_input_tokens']} cache read, "
        f"{r['cache_creation_input_tokens']} cache write, {r['output_tokens']} output",
        f"Cost: ${r['cost_usd']:.4f}   Batch time: "
        + (f"{r['batch_seconds'] / 60:.1f} min" if r["batch_seconds"] is not None else "unknown"),
        "",
        f"Month: {m['kept_by_code_filter']} kept by the code filter, {m['screened_ok']} screened, "
        f"{m['not_yet_screened']} not yet screened",
        f"Relevant: {m['relevant']}   High yield (score 4-5): {m['high_yield_4_plus']}",
        f"Designs: {m['designs']}",
        f"Topics: {m['topics']}",
        "",
        "Shortlist:",
    ]
    for s in report["shortlist"]:
        lines.append(
            f"{s['rank']:>3}. [{s['score']}] PMID {s['pmid']} {s['design']} "
            f"{','.join(s['topics'])}: {s['title'][:90]}"
        )
    if r["unknown_custom_ids"]:
        lines.append(f"Results for unknown articles (ignored): {r['unknown_custom_ids']}")
    return "\n".join(lines)


# ---- command line ------------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Screen one month's articles with Claude.")
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name in ("run", "submit"):
        p = sub.add_parser(name)
        p.add_argument("month")
        p.add_argument("--limit", type=int, help="screen at most this many articles")
    p = sub.add_parser("collect")
    p.add_argument("run_id", nargs="?", type=int)
    p = sub.add_parser("report")
    p.add_argument("month")
    args = parser.parse_args(argv)

    from kria.db.base import SessionLocal
    from kria.pipeline.batches import ClaudeBatches

    with SessionLocal() as session:
        if args.cmd == "report":
            run = session.scalar(
                select(ScreeningRun)
                .where(ScreeningRun.month == args.month, ScreeningRun.status == "ok")
                .order_by(ScreeningRun.id.desc())
            )
            if run is None:
                print(f"No finished screening run for {args.month}.")
                return 1
            print(format_report(run.report))
            return 0
        batches = ClaudeBatches(api_key=settings.anthropic_api_key)
        if args.cmd == "collect":
            run = session.get(ScreeningRun, args.run_id) if args.run_id else pending_run(session)
            if run is None:
                print("No submitted screening run to collect.")
                return 1
            if not collect(session, batches, run):
                print(f"Batch {run.batch_id} for {run.month} is still running.")
                return 0
            print(format_report(run.report))
            return 0
        run = submit_month(session, batches, args.month, limit=args.limit)
        if run is None:
            print(f"Nothing left to screen for {args.month}.")
            return 0
        print(f"Submitted {run.requested} articles as batch {run.batch_id} (run {run.id}).")
        if args.cmd == "run":
            wait_and_collect(session, batches, run)
            print(format_report(run.report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
