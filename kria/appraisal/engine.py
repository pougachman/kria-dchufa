"""The appraisal engine (milestone M4): the only agentic part of the system.

Steps, each stored with the model that served it and the prompt version:
1. extract   design, risk-of-bias tool, PICO, results with counts, every fact with a verbatim
             quote and an anchor (page or section)
2. (code)    check every quote against its anchor; compute statistics in calc.py
3. report    first-pass SGEM appraisal, using only the extraction and the computed statistics
4. skeptic   a separate Guest Skeptic pass over the draft
5. reconcile final appraisal, with the disagreements that remain
6. (code)    check the final report's quotes and list numbers found in neither the article nor
             the computed statistics

The reference is built by code from the PubMed record, never by the model.
"""

from __future__ import annotations

import base64
import datetime as dt
import json
import secrets
from pathlib import Path

import yaml
from sqlalchemy.orm import Session

from kria.appraisal import calc
from kria.appraisal.checks import find_quote, unsupported_numbers
from kria.appraisal.fulltext import FullText, Unit, units_from_jats, units_from_pdf
from kria.db.models import Appraisal, Article, UploadedFile

ROOT = Path(__file__).resolve().parents[2]
PROMPTS = ROOT / "prompts"
# v2 (Yoav, 2026-11-14, after the first live appraisal: about 5,500 words and 1.02 USD):
# one page, about 600 words, word limits per section, no separate quality checklist.
PROMPT_VERSIONS = {
    "extract": "appraisal_extract.v3",  # v3: PICO elements up to 20 words each
    "report": "appraisal_report.v2",
    "skeptic": "appraisal_skeptic.v2",
    "reconcile": "appraisal_reconcile.v2",
}
MODEL = "claude-opus-5-5"
# Extraction is copying, not judgement: medium effort. Appraisal steps think harder.
EFFORT = {"extract": "medium", "report": "high", "skeptic": "high", "reconcile": "high"}
MAX_TOKENS = 32000
FILE_DAYS = 30

TOOLS = ["RoB 2", "ROBINS-I", "QUADAS-2", "PROBAST", "AMSTAR 2", "AGREE II", "none"]
DESIGNS = [
    "rct",
    "cluster_rct",
    "non_randomized_trial",
    "systematic_review_meta_analysis",
    "cohort",
    "case_control",
    "cross_sectional",
    "diagnostic_accuracy",
    "prediction_model",
    "guideline",
    "other",
]
SKEPTIC_CATEGORIES = [
    "spin",
    "outcome_switching",
    "composite_outcome",
    "surrogate_outcome",
    "multiplicity",
    "subgroup_credibility",
    "early_stopping",
    "fragility",
    "funding_coi",
    "generalisability",
    "statistics",
    "unsupported_claim",
    "other",
]


# ---- JSON schemas for structured output (no numeric or length constraints: not supported) ----


def _obj(props: dict) -> dict:
    return {
        "type": "object",
        "properties": props,
        "required": list(props),
        "additionalProperties": False,
    }


S = {"type": "string"}
INT_OR_NULL = {"anyOf": [{"type": "integer"}, {"type": "null"}]}
QUOTED = _obj({"text": S, "quote": S, "anchor": S})


def extract_schema() -> dict:
    result = _obj(
        {
            "label": S,
            "is_primary": {"type": "boolean"},
            "outcome_type": {
                "type": "string",
                "enum": ["binary", "diagnostic", "continuous", "time_to_event", "other"],
            },
            "event_is_undesirable": {"type": "boolean"},
            "intervention_events": INT_OR_NULL,
            "intervention_total": INT_OR_NULL,
            "control_events": INT_OR_NULL,
            "control_total": INT_OR_NULL,
            "tp": INT_OR_NULL,
            "fp": INT_OR_NULL,
            "fn": INT_OR_NULL,
            "tn": INT_OR_NULL,
            "reported": S,
            "quote": S,
            "anchor": S,
        }
    )
    return _obj(
        {
            "design": {"type": "string", "enum": DESIGNS},
            "checklist_tool": {"type": "string", "enum": TOOLS},
            "clinical_question": S,
            "pico": _obj({"population": S, "intervention": S, "comparison": S, "outcome": S}),
            "authors_conclusion": QUOTED,
            "registration": QUOTED,
            "funding_coi": QUOTED,
            "results": {"type": "array", "items": result},
            "facts": {"type": "array", "items": _obj({"statement": S, "quote": S, "anchor": S})},
        }
    )


def report_schema(final: bool = False, checklist: bool = False) -> dict:
    domain = _obj(
        {
            "domain": S,
            "judgement": {
                "type": "string",
                "enum": ["low", "some concerns", "high", "unclear", "not applicable"],
            },
            "reason": S,
            "quote": S,
            "anchor": S,
        }
    )
    props = {
        "case": S,
        "background": S,
        "clinical_question": S,
        "authors_conclusions": S,
        "risk_of_bias": _obj(
            {"tool": S, "domains": {"type": "array", "items": domain}, "overall": S}
        ),
        "key_results": S,
        "talk_nerdy": {
            "type": "array",
            "items": _obj({"point": S, "quote": S, "anchor": S}),
        },
        "comment_on_conclusion": S,
        "grade_certainty": {
            "type": "string",
            "enum": ["high", "moderate", "low", "very low", "not applicable"],
        },
        "grade_reason": S,
        "bottom_line": S,
        "clinical_application": S,
        "what_do_i_tell_my_patient": S,
        "case_resolution": S,
        "israeli_ed_applicability": S,
        "journal_club_questions": {"type": "array", "items": S},
    }
    if checklist:  # prompt v1 only
        props["quality_checklist"] = {
            "type": "array",
            "items": _obj(
                {
                    "item": S,
                    "answer": {"type": "string", "enum": ["yes", "no", "unclear", "n/a"]},
                    "comment": S,
                }
            ),
        }
    if final:
        props["disagreements"] = {
            "type": "array",
            "items": _obj({"topic": S, "appraisal_view": S, "skeptic_view": S}),
        }
        props["changes"] = {"type": "array", "items": S}
    return _obj(props)


def skeptic_schema() -> dict:
    issue = _obj(
        {
            "category": {"type": "string", "enum": SKEPTIC_CATEGORIES},
            "concern": S,
            "severity": {"type": "string", "enum": ["minor", "moderate", "major"]},
            "quote": S,
            "anchor": S,
        }
    )
    return _obj({"issues": {"type": "array", "items": issue}, "overall": S})


# ---- prompts and article content ---------------------------------------------------------------


def anchor_rule(kind: str) -> str:
    if kind in ("oa_pdf", "upload"):
        return (
            'The anchor is the PDF page, written "p. N", where N counts the pages of the file '
            "(1 = the first page), not the journal's printed page number."
        )
    return (
        "The anchor is the name of the section or table the quote is in, copied exactly from "
        'the [[...]] markers in the article text (for example "Results" or "Table 2").'
    )


def load_prompt(step: str, kind: str) -> str:
    text = (PROMPTS / f"{PROMPT_VERSIONS[step]}.md").read_text(encoding="utf-8")
    return text.replace("{{anchor_rule}}", anchor_rule(kind))


def article_blocks(article: Article, ft: FullText) -> list[dict]:
    """The article as content blocks; the first block is cached and reused by every step."""
    head = (
        f"Article: PMID {article.pmid}; DOI {article.doi or 'not given'}; "
        f"{article.journal}; {article.title}"
    )
    if ft.pdf is not None:
        return [
            {
                "type": "document",
                "source": {
                    "type": "base64",
                    "media_type": "application/pdf",
                    "data": base64.standard_b64encode(ft.pdf).decode(),
                },
                "title": article.title[:250],
                "cache_control": {"type": "ephemeral"},
            },
            {"type": "text", "text": head},
        ]
    text = "\n\n".join(f"[[{u.anchor}]]\n{u.text}" for u in ft.units)
    return [
        {
            "type": "text",
            "text": f"{head}\n\n<article>\n{text}\n</article>",
            "cache_control": {"type": "ephemeral"},
        }
    ]


def reference(article: Article) -> str:
    """The citation, from the PubMed record only."""
    names = [
        a if isinstance(a, str) else (a.get("name") or a.get("collective") or "")
        for a in (article.authors or [])
    ]
    names = [n for n in names if n]
    authors = ", ".join(names[:3]) + (" et al" if len(names) > 3 else "")
    parts = [p for p in [authors, article.title, article.journal_ta, article.pub_date] if p]
    tail = f"PMID {article.pmid}" + (f"; doi:{article.doi}" if article.doi else "")
    return ". ".join(parts) + ". " + tail


# ---- statistics ---------------------------------------------------------------------------------


def compute_stats(results: list[dict]) -> list[dict]:
    """calc.py statistics for every result whose counts the article states."""
    out = []
    for r in results:
        c = [r.get(k) for k in ("intervention_events", "intervention_total")]
        c += [r.get(k) for k in ("control_events", "control_total")]
        d = [r.get(k) for k in ("tp", "fp", "fn", "tn")]
        entry = {"label": r["label"], "is_primary": r.get("is_primary", False)}
        if r.get("outcome_type") == "binary" and all(isinstance(x, int) for x in c):
            e1, n1, e0, n0 = c
            if 0 <= e1 <= n1 and 0 <= e0 <= n0 and n1 > 0 and n0 > 0:
                entry["kind"] = "binary"
                entry["counts"] = {"e_int": e1, "n_int": n1, "e_ctrl": e0, "n_ctrl": n0}
                entry["metrics"] = calc.binary_effect(
                    e1, n1, e0, n0, undesirable=r.get("event_is_undesirable", True)
                )
                out.append(entry)
        elif r.get("outcome_type") == "diagnostic" and all(isinstance(x, int) for x in d):
            tp, fp, fn, tn = d
            if tp + fn > 0 and tn + fp > 0:
                entry["kind"] = "diagnostic"
                entry["counts"] = {"tp": tp, "fp": fp, "fn": fn, "tn": tn}
                m = calc.diagnostic(tp, fp, fn, tn)
                m["post_pos"] = {str(k): v for k, v in m["post_pos"].items()}
                m["post_neg"] = {str(k): v for k, v in m["post_neg"].items()}
                entry["metrics"] = m
                out.append(entry)
    return json.loads(json.dumps(out, default=_json_default))


def _json_default(v):
    if isinstance(v, tuple):
        return list(v)
    return str(v)


def _clean_floats(obj):
    if isinstance(obj, float):
        if obj != obj or obj in (float("inf"), float("-inf")):
            return None
        return obj
    if isinstance(obj, dict):
        return {k: _clean_floats(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_clean_floats(v) for v in obj]
    return obj


# ---- checks -------------------------------------------------------------------------------------


def quoted_items(extraction: dict | None, report: dict | None, skeptic: dict | None) -> list:
    """(where, quote, anchor) for every quote the model gave."""
    items = []
    if extraction:
        for key in ("authors_conclusion", "registration", "funding_coi"):
            q = extraction.get(key) or {}
            items.append((key, q.get("quote", ""), q.get("anchor", "")))
        for r in extraction.get("results", []):
            items.append((f"result: {r['label']}", r.get("quote", ""), r.get("anchor", "")))
        for f in extraction.get("facts", []):
            items.append((f"fact: {f['statement'][:60]}", f.get("quote", ""), f.get("anchor", "")))
    if report:
        for d in (report.get("risk_of_bias") or {}).get("domains", []):
            items.append((f"bias: {d['domain']}", d.get("quote", ""), d.get("anchor", "")))
        for t in report.get("talk_nerdy", []):
            items.append(
                (f"talk nerdy: {t['point'][:60]}", t.get("quote", ""), t.get("anchor", ""))
            )
    if skeptic:
        for i in skeptic.get("issues", []):
            items.append((f"skeptic: {i['concern'][:60]}", i.get("quote", ""), i.get("anchor", "")))
    return [x for x in items if x[1].strip()]


# Not checked for numbers: quotes (checked against their anchors), the reference (built by
# code from PubMed) and the case vignette, which is a made-up patient by design (SGEM style).
NOT_NUMBER_CHECKED = ("quote", "anchor", "reference", "case", "case_resolution")


def report_texts(report: dict) -> list[str]:
    out = []

    def walk(v, key=""):
        if key in NOT_NUMBER_CHECKED:
            return
        if isinstance(v, str):
            out.append(v)
        elif isinstance(v, dict):
            for k, x in v.items():
                walk(x, k)
        elif isinstance(v, list):
            for x in v:
                walk(x, key)

    walk(report)
    return out


def run_checks(ft: FullText, extraction: dict, report: dict, skeptic: dict, stats: list) -> dict:
    quotes = []
    for where, quote, anchor in quoted_items(extraction, report, skeptic):
        res = find_quote(quote, ft.units, anchor)
        quotes.append({"where": where, "quote": quote, "anchor": anchor, **res})
    verified = sum(q["verified"] for q in quotes)
    return {
        "quotes_total": len(quotes),
        "quotes_verified": verified,
        "quotes": quotes,
        "unsupported_numbers": unsupported_numbers(report_texts(report), ft.joined(), stats),
    }


# ---- running an appraisal -----------------------------------------------------------------------


def now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def new_token() -> str:
    return secrets.token_urlsafe(24)


def stored_fulltext(session: Session, appraisal: Appraisal, article: Article) -> FullText:
    """The text the appraisal runs on: the stored file, or the abstract (limited)."""
    f = (
        session.query(UploadedFile)
        .filter(UploadedFile.appraisal_id == appraisal.id)
        .order_by(UploadedFile.id.desc())
        .first()
    )
    if f is None:
        units = [Unit("Title", article.title)]
        units.append(Unit("Abstract", article.abstract or "No abstract in PubMed."))
        return FullText("abstract", units, url=f"https://pubmed.ncbi.nlm.nih.gov/{article.pmid}/")
    if f.filename.endswith(".xml"):
        return FullText("pmc_xml", units_from_jats(f.content), url=appraisal.source_url)
    return FullText(appraisal.source or "upload", units_from_pdf(f.content), pdf=f.content)


def store_file(
    session: Session, appraisal: Appraisal, content: bytes, filename: str, uploader: str
) -> UploadedFile:
    import hashlib

    f = UploadedFile(
        appraisal_id=appraisal.id,
        uploader=uploader,
        filename=filename[:255],
        sha256=hashlib.sha256(content).hexdigest(),
        size=len(content),
        content=content,
        uploaded_at=now(),
        delete_after=now() + dt.timedelta(days=FILE_DAYS),
    )
    session.add(f)
    return f


def delete_files(session: Session, appraisal_id: int) -> int:
    n = (
        session.query(UploadedFile)
        .filter(UploadedFile.appraisal_id == appraisal_id)
        .delete(synchronize_session=False)
    )
    return n


def purge_expired_files(session: Session) -> int:
    """Delete full-text files past their 30 days (run by the daily job)."""
    n = (
        session.query(UploadedFile)
        .filter(UploadedFile.delete_after < now())
        .delete(synchronize_session=False)
    )
    session.commit()
    return n


def run(session: Session, appraisal_id: int, llm) -> Appraisal:
    """Run every step for one appraisal; on failure the appraisal records the error."""
    appraisal = session.get(Appraisal, appraisal_id)
    article = session.get(Article, appraisal.article_id)
    appraisal.status, appraisal.started_at, appraisal.error = "running", now(), None
    appraisal.steps, appraisal.cost_usd = {}, 0.0
    session.commit()
    try:
        ft = stored_fulltext(session, appraisal, article)
        appraisal.limited = ft.kind == "abstract"
        blocks = article_blocks(article, ft)

        def step(name: str, schema: dict, extra: str) -> dict:
            appraisal.stage = name
            session.commit()
            output, meta = llm.call(
                system=load_prompt(name, ft.kind),
                blocks=blocks + [{"type": "text", "text": extra}],
                schema=schema,
                effort=EFFORT[name],
            )
            appraisal.steps = {
                **appraisal.steps,
                name: {"output": output, "prompt_version": PROMPT_VERSIONS[name], **meta},
            }
            appraisal.cost_usd = round(appraisal.cost_usd + (meta.get("cost_usd") or 0), 4)
            session.commit()
            return output

        extraction = step("extract", extract_schema(), "Extract the data now.")
        stats = _clean_floats(compute_stats(extraction.get("results", [])))
        appraisal.stats = stats
        context = (
            "Extracted data (JSON):\n"
            + json.dumps(extraction, ensure_ascii=False)
            + "\n\nStatistics computed by code (JSON; use these numbers):\n"
            + json.dumps(stats, ensure_ascii=False)
        )
        if ft.kind == "abstract":
            context += "\n\nOnly the abstract is available: this appraisal is LIMITED. Say so."
        draft = step("report", report_schema(), context + "\n\nWrite the first-pass appraisal.")
        skeptic = step(
            "skeptic",
            skeptic_schema(),
            context + "\n\nFirst-pass draft (JSON):\n" + json.dumps(draft, ensure_ascii=False),
        )
        final = step(
            "reconcile",
            report_schema(final=True),
            context
            + "\n\nFirst-pass draft (JSON):\n"
            + json.dumps(draft, ensure_ascii=False)
            + "\n\nGuest Skeptic concerns (JSON):\n"
            + json.dumps(skeptic, ensure_ascii=False)
            + "\n\nWrite the final appraisal.",
        )
        final["reference"] = reference(article)
        appraisal.report = final
        appraisal.checks = run_checks(ft, extraction, final, skeptic, stats)
        appraisal.status, appraisal.stage, appraisal.finished_at = "review", None, now()
        session.commit()
    except Exception as exc:
        session.rollback()
        appraisal = session.get(Appraisal, appraisal_id)
        appraisal.status, appraisal.error = "failed", repr(exc)[:2000]
        appraisal.finished_at = now()
        session.commit()
    return appraisal


# ---- the Claude client --------------------------------------------------------------------------


class Claude:
    """Calls Claude with structured output. Tests use a fake with the same `call` method."""

    def __init__(self, api_key: str | None, client=None, model: str = MODEL):
        import anthropic

        if client is None:
            if not api_key:
                raise RuntimeError("ANTHROPIC_API_KEY is not set")
            client = anthropic.Anthropic(api_key=api_key, timeout=900, max_retries=2)
        self.client = client
        self.model = model
        self.pricing = yaml.safe_load((ROOT / "data" / "pricing.yaml").read_text())

    def call(
        self, system: str, blocks: list[dict], schema: dict, effort: str = "high"
    ) -> tuple[dict, dict]:
        with self.client.beta.messages.stream(
            model=self.model,
            max_tokens=MAX_TOKENS,
            system=system,
            messages=[{"role": "user", "content": blocks}],
            output_config={"effort": effort, "format": {"type": "json_schema", "schema": schema}},
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        ) as stream:
            msg = stream.get_final_message()
        if msg.stop_reason == "refusal":
            raise RuntimeError(f"the model declined ({getattr(msg, 'stop_details', None)})")
        if msg.stop_reason == "max_tokens":
            raise RuntimeError("the answer was cut off (max_tokens)")
        text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
        usage = {
            k: getattr(msg.usage, k, 0) or 0
            for k in (
                "input_tokens",
                "output_tokens",
                "cache_creation_input_tokens",
                "cache_read_input_tokens",
            )
        }
        return json.loads(text), {
            "model": msg.model,
            "effort": effort,
            "stop_reason": msg.stop_reason,
            "usage": usage,
            "cost_usd": self.cost(msg.model, usage),
            "at": now().isoformat(),
        }

    def cost(self, model: str, usage: dict) -> float | None:
        p = self.pricing["models"].get(model)
        if p is None:
            return None
        return (
            usage["input_tokens"] * p["input"]
            + usage["cache_creation_input_tokens"]
            * p["input"]
            * self.pricing["cache_write_multiplier"]
            + usage["cache_read_input_tokens"] * p["input"] * self.pricing["cache_read_multiplier"]
            + usage["output_tokens"] * p["output"]
        ) / 1_000_000
