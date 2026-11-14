import datetime as dt
import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from kria.appraisal import engine, service
from kria.appraisal.checks import find_quote, unsupported_numbers
from kria.appraisal.fulltext import (
    OpenAccess,
    Unit,
    matches_article,
    units_from_jats,
    units_from_pdf,
)
from kria.config import settings
from kria.db.models import Appraisal, Article, UploadedFile
from kria.web.main import app, get_session
from tests.test_screen import session  # noqa: F401

PASSWORD = "editor pass"
TITLE = "Early fluids in septic shock: a randomized trial"
DOI = "10.1000/sepsis.2026.1"
PAGES = [
    f"{TITLE}. doi: {DOI}. Funding: This trial was funded by Acme Pharma.",
    "Results. Death at 90 days occurred in 20 of 100 patients (20.0%) in the early-fluids "
    "group and 30 of 100 patients (30.0%) in the usual-care group.",
    "Conclusions. Early fluids reduced mortality in septic shock.",
]


def make_pdf(pages: list[str]) -> bytes:
    """A small valid PDF with one line of Helvetica text per page (for tests only)."""
    objs = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        None,
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    kids = []
    for text in pages:
        safe = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        stream = f"BT /F1 8 Tf 20 700 Td ({safe}) Tj ET"
        objs.append(f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream")
        content_id = len(objs)
        objs.append(
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 1600 792] "
            f"/Resources << /Font << /F1 3 0 R >> >> /Contents {content_id} 0 R >>"
        )
        kids.append(f"{len(objs)} 0 R")
    objs[1] = f"<< /Type /Pages /Kids [{' '.join(kids)}] /Count {len(kids)} >>"
    out, offsets = b"%PDF-1.4\n", []
    for i, body in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n{body}\nendobj\n".encode()
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    out += "".join(f"{o:010d} 00000 n \n" for o in offsets).encode()
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF".encode()
    return out


PDF = make_pdf(PAGES)

JATS = b"""<article><front><article-meta><abstract><p>Background and aims.</p></abstract>
</article-meta></front><body><sec><title>Methods</title><p>We randomized 200 adults.</p></sec>
<sec><title>Results</title><p>Death occurred in 20 of 100 and 30 of 100 patients.</p>
<sec><title>Safety</title><p>No excess harm.</p></sec></sec></body>
<back><table-wrap><label>Table 2</label><caption>Outcomes</caption><table><tr><td>Death</td>
<td>20/100</td><td>30/100</td></tr></table></table-wrap></back></article>"""


# ---- full text, quotes, numbers -----------------------------------------------------------------


def test_pdf_pages_and_matching():
    units = units_from_pdf(PDF)
    assert [u.anchor for u in units] == ["p. 1", "p. 2", "p. 3"]
    assert "20 of 100 patients" in units[1].text
    assert matches_article(units[0].text, DOI, TITLE)[0]
    assert matches_article(units[0].text, None, TITLE)[0]  # by title words
    ok, reason = matches_article("A different paper about asthma", "10.9/x", TITLE)
    assert not ok and "neither" in reason


def test_jats_units():
    units = units_from_jats(JATS)
    anchors = [u.anchor for u in units]
    assert anchors[0] == "Abstract" and "Results" in anchors and "Table 2" in anchors
    results = next(u for u in units if u.anchor == "Results")
    assert "No excess harm" in results.text  # nested subsection kept with its section


def test_quote_check_against_the_cited_page():
    units = units_from_pdf(PDF)
    ok = find_quote("20 of 100 patients (20.0%)", units, "p. 2")
    assert ok["verified"]
    wrong_page = find_quote("20 of 100 patients (20.0%)", units, "p. 3")
    assert not wrong_page["verified"] and wrong_page["where"] == "p. 2"
    invented = find_quote("mortality fell by half", units, "p. 2")
    assert not invented["verified"] and invented["reason"] == "not found in the article"


def test_unsupported_numbers():
    source = " ".join(PAGES)
    stats = engine.compute_stats(extraction()["results"])
    texts = ["Death fell from 30.0% to 20.0%, a 10.0% difference, NNT 10, in 47 centres."]
    assert unsupported_numbers(texts, source, stats) == ["47"]


# ---- open-access lookup -------------------------------------------------------------------------


def oa_client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)


def test_open_access_finds_europe_pmc_xml():
    long_jats = JATS.replace(b"No excess harm.", b"No excess harm. " + b"Detail. " * 400)

    def handler(req):
        if "eutils" in str(req.url):
            return httpx.Response(500)  # PMC unavailable: Europe PMC is the backup
        assert req.url.path.endswith("/PMC123/fullTextXML")
        return httpx.Response(200, content=long_jats)

    ft, log = OpenAccess(http=oa_client(handler)).find("PMC123", DOI)
    assert ft.kind == "pmc_xml" and ft.raw == long_jats and "europepmc.org" in ft.url
    assert "HTTP 200" in log["europe_pmc"] and "HTTP 500" in log["pmc"]


def test_open_access_prefers_ncbi_pmc():
    long_jats = JATS.replace(b"No excess harm.", b"No excess harm. " + b"Detail. " * 400)
    seen = []

    def handler(req):
        seen.append(str(req.url))
        assert "eutils" in str(req.url) and req.url.params["db"] == "pmc"
        assert req.url.params["id"] == "10685906" and req.url.params["api_key"] == "k"
        return httpx.Response(200, content=long_jats)

    ft, log = OpenAccess(http=oa_client(handler), ncbi_api_key="k").find("PMC10685906", DOI)
    assert ft.kind == "pmc_xml" and "pmc.ncbi.nlm.nih.gov/articles/PMC10685906" in ft.url
    assert len(seen) == 1 and "europe_pmc" not in log


def test_open_access_unpaywall_pdf_and_none():
    def handler(req):
        if req.url.path.endswith("/search"):
            return httpx.Response(200, json={"resultList": {"result": [{"doi": DOI}]}})
        if "unpaywall" in str(req.url):
            assert req.url.params["email"] == "contact@example.org"
            return httpx.Response(
                200,
                json={
                    "is_oa": True,
                    "best_oa_location": {
                        "url_for_pdf": "https://oa.example/p.pdf",
                        "license": "cc-by",
                    },
                },
            )
        return httpx.Response(200, content=PDF)

    ft, log = OpenAccess("contact@example.org", http=oa_client(handler)).find(None, DOI)
    assert ft.kind == "oa_pdf" and ft.pdf == PDF and "cc-by" in log["unpaywall"]
    ft, log = OpenAccess(None, http=oa_client(handler)).find(None, DOI)
    assert ft is None and "UNPAYWALL_EMAIL" in log["unpaywall"]
    assert log["europe_pmc_search"] == "no PMC copy"


# ---- identifiers and articles -------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("30449534", ("pmid", "30449534")),
        ("PMID: 30449534", ("pmid", "30449534")),
        ("https://doi.org/10.1056/NEJMoa1806842", ("doi", "10.1056/nejmoa1806842")),
        ("10.1001/jama.2018.7044.", ("doi", "10.1001/jama.2018.7044")),
    ],
)
def test_parse_identifier(raw, expected):
    assert service.parse_identifier(raw) == expected


def test_parse_identifier_rejects_text():
    with pytest.raises(ValueError):
        service.parse_identifier("sepsis fluids trial")


ARTICLE_XML = f"""<PubmedArticleSet><PubmedArticle><MedlineCitation><PMID>777</PMID><Article>
<Journal><JournalIssue><PubDate><Year>2026</Year></PubDate></JournalIssue><Title>Ann Emerg Med
</Title></Journal><ArticleTitle>{TITLE}</ArticleTitle><Abstract><AbstractText>Short abstract.
</AbstractText></Abstract><AuthorList><Author><LastName>Cohen</LastName><ForeName>Dana</ForeName>
</Author></AuthorList><Language>eng</Language><PublicationTypeList><PublicationType>
Randomized Controlled Trial</PublicationType></PublicationTypeList></Article><MedlineJournalInfo>
<MedlineTA>Ann Emerg Med</MedlineTA></MedlineJournalInfo></MedlineCitation><PubmedData>
<ArticleIdList><ArticleId IdType="doi">{DOI}</ArticleId></ArticleIdList></PubmedData>
</PubmedArticle></PubmedArticleSet>""".encode()


class FakeEU:
    def search(self, db, term):
        assert db == "pubmed"
        return (["777"] if DOI in term else []), term

    def fetch_pubmed_xml(self, pmids):
        assert list(pmids) == ["777"]
        yield ARTICLE_XML


def test_find_article_fetches_from_pubmed_once(session):  # noqa: F811
    a = service.find_article(session, FakeEU(), DOI)
    assert a.pmid == "777" and a.doi == DOI and a.title == TITLE
    assert service.find_article(session, FakeEU(), "777").id == a.id
    with pytest.raises(ValueError, match="DOI"):
        service.find_article(session, FakeEU(), "10.1000/missing")


# ---- the engine with a fake model ---------------------------------------------------------------


def extraction():
    return {
        "design": "rct",
        "checklist_tool": "RoB 2",
        "clinical_question": "Do early fluids reduce death in septic shock?",
        "pico": {
            "population": "Adults",
            "intervention": "Early fluids",
            "comparison": "Usual care",
            "outcome": "Death at 90 days",
        },
        "authors_conclusion": {
            "text": "Reduced mortality",
            "anchor": "p. 3",
            "quote": "Early fluids reduced mortality in septic shock.",
        },
        "registration": {"text": "not reported", "quote": "", "anchor": ""},
        "funding_coi": {"text": "Industry", "quote": "funded by Acme Pharma", "anchor": "p. 2"},
        "results": [
            {
                "label": "Death at 90 days",
                "is_primary": True,
                "outcome_type": "binary",
                "event_is_undesirable": True,
                "intervention_events": 20,
                "intervention_total": 100,
                "control_events": 30,
                "control_total": 100,
                "tp": None,
                "fp": None,
                "fn": None,
                "tn": None,
                "reported": "20.0% vs 30.0%",
                "quote": "20 of 100 patients (20.0%) in the early-fluids group",
                "anchor": "p. 2",
            }
        ],
        "facts": [],
    }


def report(final=False):
    r = {
        "case": "A 60-year-old in septic shock.",
        "background": "Fluids are debated.",
        "clinical_question": "Early fluids and death?",
        "authors_conclusions": "Fewer deaths.",
        "risk_of_bias": {
            "tool": "RoB 2",
            "overall": "some concerns",
            "domains": [
                {
                    "domain": "Randomization",
                    "judgement": "low",
                    "reason": "Concealed.",
                    "quote": "30 of 100 patients (30.0%)",
                    "anchor": "p. 2",
                }
            ],
        },
        "quality_checklist": [{"item": "Was it randomized?", "answer": "yes", "comment": ""}],
        "key_results": "Death 20.0% vs 30.0%, a 10.0% absolute reduction, NNT 10, in 47 centres.",
        "talk_nerdy": [
            {"point": "Industry funding.", "quote": "funded by Acme Pharma", "anchor": "p. 1"}
        ],
        "comment_on_conclusion": "Agree, with caution.",
        "grade_certainty": "moderate",
        "grade_reason": "Single trial.",
        "bottom_line": "Early fluids may reduce death.",
        "clinical_application": "Consider early fluids.",
        "what_do_i_tell_my_patient": "It helps.",
        "case_resolution": "Fluids given.",
        "israeli_ed_applicability": "Feasible in Israel.",
        "journal_club_questions": ["Was the outcome patient-important?"],
    }
    if final:
        r["disagreements"] = [
            {"topic": "Funding", "appraisal_view": "Minor", "skeptic_view": "Major"}
        ]
        r["changes"] = ["Added funding concern."]
    return r


class FakeLLM:
    def __init__(self):
        self.calls = []

    def call(self, system, blocks, schema, effort="high"):
        props = schema["properties"]
        self.calls.append((system, blocks, schema, effort))
        if "design" in props:
            out = extraction()
        elif "issues" in props:
            out = {
                "issues": [
                    {
                        "category": "funding_coi",
                        "concern": "Sponsor",
                        "severity": "moderate",
                        "quote": "funded by Acme Pharma",
                        "anchor": "p. 1",
                    }
                ],
                "overall": "Some concerns.",
            }
        else:
            out = report(final="disagreements" in props)
        meta = {
            "model": "claude-opus-5-5",
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 1000, "output_tokens": 500},
            "cost_usd": 0.05,
        }
        return out, meta


def new_appraisal(session, source="upload"):  # noqa: F811
    article = Article(
        pmid="777",
        doi=DOI,
        title=TITLE,
        journal="Annals",
        journal_ta="Ann Emerg Med",
        abstract="Short abstract.",
        pub_types=[],
        languages=["eng"],
        authors=[],
        mesh=[],
        keywords=[],
        flags=[],
        pub_date="2026",
    )
    session.add(article)
    session.flush()
    a = Appraisal(
        article_id=article.id,
        status="queued",
        public_token=engine.new_token(),
        requested_at=engine.now(),
        steps={},
        source=source,
    )
    session.add(a)
    session.flush()
    return a


def test_engine_runs_all_steps_with_checks(session):  # noqa: F811
    a = new_appraisal(session)
    engine.store_file(session, a, PDF, "paper.pdf", uploader="national-editor")
    session.commit()
    llm = FakeLLM()
    a = engine.run(session, a.id, llm)
    assert a.status == "review", a.error
    assert list(a.steps) == ["extract", "report", "skeptic", "reconcile"]
    for name, step in a.steps.items():
        assert step["model"] == "claude-opus-5-5"
        assert step["prompt_version"] == engine.PROMPT_VERSIONS[name]
    assert a.cost_usd == pytest.approx(0.2)
    # statistics are computed in code
    m = a.stats[0]["metrics"]
    assert m["difference"] == pytest.approx(-0.1) and m["NNT"] == pytest.approx(10.0)
    assert m["direction"] == "benefit"
    # the reference comes from the PubMed record, not the model
    assert "PMID 777" in a.report["reference"] and DOI in a.report["reference"]
    # code checks
    c = a.checks
    assert c["unsupported_numbers"] == ["47"]
    bad = [q for q in c["quotes"] if not q["verified"]]
    assert {q["anchor"] for q in bad} == {"p. 2"}  # the funding quote cites the wrong page
    assert c["quotes_verified"] == c["quotes_total"] - len(bad) >= 3
    # the PDF goes to the model as a cached document block, anchors are pages
    system, blocks, _, effort = llm.calls[0]
    assert blocks[0]["type"] == "document" and blocks[0]["cache_control"]
    assert '"p. N"' in system and "{{" not in system
    assert [c[3] for c in llm.calls] == ["medium", "high", "high", "high"]
    assert "600 words" in llm.calls[1][0]  # the one-page report prompt (v2)
    assert "quality_checklist" not in llm.calls[1][2]["properties"]


def test_engine_abstract_only_is_limited_and_failure_is_recorded(session):  # noqa: F811
    a = new_appraisal(session, source="abstract")
    session.commit()
    a = engine.run(session, a.id, FakeLLM())
    assert a.limited and a.status == "review"

    class Broken(FakeLLM):
        def call(self, *args, **kw):
            raise RuntimeError("the model declined")

    a = engine.run(session, a.id, Broken())
    assert a.status == "failed" and "declined" in a.error


def test_purge_expired_files(session):  # noqa: F811
    a = new_appraisal(session)
    old = engine.store_file(session, a, PDF, "old.pdf", uploader="x")
    old.delete_after = engine.now() - dt.timedelta(days=1)
    engine.store_file(session, a, PDF, "new.pdf", uploader="x")
    session.commit()
    assert engine.purge_expired_files(session) == 1
    assert [f.filename for f in session.scalars(select(UploadedFile))] == ["new.pdf"]


def test_schemas_use_only_supported_features():
    for schema in (engine.extract_schema(), engine.report_schema(True), engine.skeptic_schema()):
        text = json.dumps(schema)
        for bad in ("minimum", "maximum", "minItems", "maxItems", "minLength", "maxLength"):
            assert bad not in text

        def check(node):
            if isinstance(node, dict):
                if node.get("type") == "object":
                    assert node["additionalProperties"] is False
                    assert set(node["required"]) == set(node["properties"])
                for v in node.values():
                    check(v)
            elif isinstance(node, list):
                for v in node:
                    check(v)

        check(schema)


def test_prompts_render_for_both_kinds_of_source():
    for step in engine.PROMPT_VERSIONS:
        for kind in ("upload", "pmc_xml"):
            text = engine.load_prompt(step, kind)
            assert "{{" not in text and ("p. N" in text) == (kind == "upload")


# ---- the Claude client wrapper ------------------------------------------------------------------


class FakeStream:
    def __init__(self, msg):
        self.msg = msg

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get_final_message(self):
        return self.msg


def fake_client(stop_reason="end_turn", model="claude-opus-5-5", text='{"ok": true}'):
    usage = SimpleNamespace(
        input_tokens=1_000_000,
        output_tokens=100_000,
        cache_creation_input_tokens=0,
        cache_read_input_tokens=0,
    )
    msg = SimpleNamespace(
        stop_reason=stop_reason,
        model=model,
        usage=usage,
        content=[SimpleNamespace(type="thinking"), SimpleNamespace(type="text", text=text)],
    )
    seen = {}

    def stream(**kw):
        seen.update(kw)
        return FakeStream(msg)

    return SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(stream=stream))), seen


def test_claude_wrapper_parses_and_prices():
    client, seen = fake_client()
    out, meta = engine.Claude(None, client=client).call(
        "sys", [{"type": "text", "text": "x"}], {"type": "object"}
    )
    assert out == {"ok": True} and meta["model"] == "claude-opus-5-5"
    assert meta["cost_usd"] == pytest.approx(4.0 + 2.0)  # $4/M input, $20/M output
    assert seen["model"] == "claude-opus-5-5" and seen["fallbacks"] == "default"
    assert seen["output_config"]["effort"] == "high"
    assert "temperature" not in seen


@pytest.mark.parametrize("stop, msg", [("refusal", "declined"), ("max_tokens", "cut off")])
def test_claude_wrapper_errors(stop, msg):
    client, _ = fake_client(stop_reason=stop)
    with pytest.raises(RuntimeError, match=msg):
        engine.Claude(None, client=client).call("s", [], {})


# ---- the web flow -------------------------------------------------------------------------------


class NoOpenAccess:
    def find(self, pmcid, doi):
        return None, {"europe_pmc_search": "no PMC copy", "unpaywall": "skipped"}


@pytest.fixture
def web(session, monkeypatch):  # noqa: F811
    monkeypatch.setattr(settings, "editor_password", PASSWORD)
    monkeypatch.setattr(settings, "anthropic_api_key", "sk-ant-test")
    app.dependency_overrides[get_session] = lambda: session
    app.state.pubmed = FakeEU
    app.state.open_access = NoOpenAccess
    app.state.appraisal_runner = lambda appraisal_id: engine.run(session, appraisal_id, FakeLLM())
    client = TestClient(app)
    client.post("/editor/login", data={"password": PASSWORD})
    yield client
    app.dependency_overrides.clear()
    for name in ("pubmed", "open_access", "appraisal_runner"):
        delattr(app.state, name)


def test_request_upload_review_approve_publish(web, session):  # noqa: F811
    r = web.post("/editor/appraisals", data={"identifier": DOI}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/editor/appraisals/")
    page = web.get(r.headers["location"]).text
    assert "לא נמצא טקסט מלא בגישה פתוחה" in page and f"https://doi.org/{DOI}" in page
    a = session.scalar(select(Appraisal))
    assert a.status == "needs_pdf"
    assert web.get(f"/a/{a.public_token}").status_code == 404  # nothing public yet

    wrong = make_pdf(["A different paper about asthma inhalers in children"])
    r = web.post(
        f"/editor/appraisals/{a.id}/upload", files={"pdf": ("x.pdf", wrong, "application/pdf")}
    )
    assert "נראה שהקובץ אינו המאמר הזה" in r.text
    r = web.post(
        f"/editor/appraisals/{a.id}/upload", files={"pdf": ("x.pdf", b"hello", "text/plain")}
    )
    assert "הקובץ אינו PDF" in r.text

    r = web.post(
        f"/editor/appraisals/{a.id}/upload", files={"pdf": ("p.pdf", PDF, "application/pdf")}
    )
    assert "הקובץ התקבל" in r.text  # the background run already finished in the test client
    session.refresh(a)
    assert a.status == "review" and a.source == "upload"
    assert session.scalar(select(UploadedFile)).uploader == "national-editor"
    page = web.get(f"/editor/appraisals/{a.id}").text
    assert "BOTTOM LINE" in page and "Early fluids may reduce death." in page
    assert "בדיקות קוד" in page and "47" in page  # the unsupported number is shown
    assert "NNT 10" in page and "RoB 2" in page

    web.post(f"/editor/appraisals/{a.id}/action", data={"action": "approve"})
    session.refresh(a)
    assert a.status == "approved" and a.approved_at is not None
    assert session.scalar(select(UploadedFile)) is None  # the PDF is deleted on approval
    public = web.get(f"/a/{a.public_token}")
    assert public.status_code == 200 and "Early fluids may reduce death." in public.text
    assert "בדיקות קוד" not in public.text  # the checks panel is editor-only
    assert "Guest Skeptic" not in public.text  # the full skeptic list is editor-only
    assert "Remaining Disagreements" in public.text
    # asking again for the same article opens the existing appraisal
    r = web.post("/editor/appraisals", data={"identifier": "777"}, follow_redirects=False)
    assert r.headers["location"] == f"/editor/appraisals/{a.id}"


def test_new_version_supersedes_and_old_link_points_to_it(web, session):  # noqa: F811
    web.post("/editor/appraisals", data={"identifier": "777"})
    first = session.scalar(select(Appraisal))
    web.post(f"/editor/appraisals/{first.id}/action", data={"action": "abstract"})
    web.post(f"/editor/appraisals/{first.id}/action", data={"action": "approve"})
    r = web.post(
        f"/editor/appraisals/{first.id}/action",
        data={"action": "new_version"},
        follow_redirects=False,
    )
    second_id = int(r.headers["location"].rsplit("/", 1)[1])
    assert second_id != first.id
    second = session.get(Appraisal, second_id)
    assert second.status == "needs_pdf"
    web.post(f"/editor/appraisals/{second.id}/action", data={"action": "abstract"})
    web.post(f"/editor/appraisals/{second.id}/action", data={"action": "approve"})
    session.refresh(first)
    assert first.status == "superseded" and second.status == "approved"
    old = web.get(f"/a/{first.public_token}")
    assert old.status_code == 200 and f"/a/{second.public_token}" in old.text
    # a new request for the article opens the current version
    r = web.post("/editor/appraisals", data={"identifier": "777"}, follow_redirects=False)
    assert r.headers["location"] == f"/editor/appraisals/{second.id}"


def test_abstract_only_path_and_list(web, session):  # noqa: F811
    web.post("/editor/appraisals", data={"identifier": "777"})
    a = session.scalar(select(Appraisal))
    web.post(f"/editor/appraisals/{a.id}/action", data={"action": "abstract"})
    session.refresh(a)
    assert a.limited and a.status == "review"
    assert "ניתוח מוגבל" in web.get(f"/editor/appraisals/{a.id}").text
    listing = web.get("/editor/appraisals").text
    assert TITLE in listing and "ממתין לאישורך" in listing


def test_retry_lookup_after_a_failed_search(web, session):  # noqa: F811
    web.post("/editor/appraisals", data={"identifier": "777"})
    a = session.scalar(select(Appraisal))
    assert a.status == "needs_pdf"
    r = web.post(f"/editor/appraisals/{a.id}/action", data={"action": "lookup"})
    assert "גם הפעם לא נמצא" in r.text

    class Found:
        def find(self, pmcid, doi):
            from kria.appraisal.fulltext import FullText

            ft = FullText("pmc_xml", units_from_jats(JATS), url="https://pmc/x", raw=JATS)
            return ft, {"pmc": "PMC1: HTTP 200"}

    app.state.open_access = Found
    web.post(f"/editor/appraisals/{a.id}/action", data={"action": "lookup"})
    session.refresh(a)
    assert a.source == "pmc_xml" and a.status == "review"  # found, then the run finished
    assert session.scalar(select(UploadedFile)).filename == "pmc.xml"


def test_bad_identifier_and_login_required(web):
    r = web.post("/editor/appraisals", data={"identifier": "fluids"})
    assert "נא להזין PMID" in r.text
    web.cookies.clear()
    assert web.get("/editor/appraisals", follow_redirects=False).status_code == 303


def test_approved_appraisal_is_linked_from_the_digest(session):  # noqa: F811
    from kria.pipeline import digest as dg

    a = new_appraisal(session)
    a.status = "approved"
    session.commit()
    items = [
        {
            "article_id": a.article_id,
            "decision": "approved",
            "featured": False,
            "topics": ["infection-sepsis"],
            "rank": 1,
        }
    ]
    dg_ranked = dg.ranked
    dg.ranked = lambda s, m: [dict(i) for i in items]
    try:
        d = dg.content(session, "2026-10")
    finally:
        dg.ranked = dg_ranked
    assert d["top"][0]["appraisal_url"].endswith(f"/a/{a.public_token}")


def test_unit_dataclass():
    assert Unit("p. 1", "x").anchor == "p. 1"
