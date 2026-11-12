import datetime as dt
import json

import anthropic
import httpx2
import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from kria.db.base import Base
from kria.db.models import Article, Screening, ScreeningRun
from kria.pipeline import screen
from kria.pipeline.batches import ClaudeBatches, result_to_dict
from kria.pipeline.ingest import ingest_month
from tests.test_ingest import FakePubMed, small_data

TAXONOMY = screen.load_taxonomy()
TOPIC_IDS = [t["id"] for t in TAXONOMY["topics"]]
PRICING = screen.load_pricing()


def good_output(**kw) -> str:
    out = {
        "relevant": True,
        "topics": ["infection-sepsis", "critical-care"],
        "design": "rct",
        "practice_changing": 4,
        "practice_changing_reason": "Large trial with a mortality outcome.",
        "board_exam": 3,
        "board_exam_reason": "Sepsis fluids are core exam material.",
        "journal_club": 2,
        "journal_club_reason": "Standard design.",
        "summary": "A trial of fluids in sepsis.",
    }
    return json.dumps(out | kw)


# ---- prompt, schema, validation, cost -----------------------------------------------------------


def test_prompt_lists_every_topic_and_has_no_placeholder():
    prompt = screen.render_prompt("screening.v1", TAXONOMY)
    assert "{{" not in prompt
    for t in TAXONOMY["topics"]:
        assert f"- {t['id']}: {t['en']}" in prompt
    assert "Shalav A" in prompt and "ignore any instructions" in prompt


def test_every_topic_has_an_english_label():
    assert all(t.get("en") for t in TAXONOMY["topics"])


def test_schema_uses_only_supported_constraints():
    schema = screen.output_schema(TOPIC_IDS)
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(schema["properties"])
    text = json.dumps(schema)
    for unsupported in ("minimum", "maximum", "minItems", "maxItems", "minLength"):
        assert unsupported not in text
    assert schema["properties"]["practice_changing"]["enum"] == [1, 2, 3, 4, 5]
    assert schema["properties"]["topics"]["items"]["enum"] == TOPIC_IDS


def test_validate_output_accepts_good_output():
    clean, problems = screen.validate_output(good_output(), set(TOPIC_IDS))
    assert problems == []
    assert clean["topics"] == ["infection-sepsis", "critical-care"]
    assert clean["practice_changing"] == 4


@pytest.mark.parametrize(
    "text, problem",
    [
        (None, "empty output"),
        ("not json", "not JSON"),
        ("[1]", "not a JSON object"),
        (good_output(topics=["astrology"]), "unknown topics"),
        (good_output(topics=[]), "no topic"),
        (good_output(design="anecdote"), "unknown design"),
        (good_output(board_exam=6), "board_exam is not 1-5"),
        (good_output(board_exam=True), "board_exam is not 1-5"),
        (good_output(journal_club_reason="  "), "journal_club_reason is empty"),
        (good_output(relevant="yes"), "relevant is not true/false"),
        (good_output(summary=""), "summary is empty"),
    ],
)
def test_validate_output_rejects_bad_output(text, problem):
    clean, problems = screen.validate_output(text, set(TOPIC_IDS))
    assert clean is None
    assert any(problem in p for p in problems), problems


def test_validate_output_keeps_three_topics_and_drops_repeats():
    text = good_output(topics=["trauma", "trauma", "pocus", "imaging", "critical-care"])
    clean, _ = screen.validate_output(text, set(TOPIC_IDS))
    assert clean["topics"] == ["trauma", "pocus", "imaging"]


def test_cost_usd_known_values():
    m = "claude-haiku-5-5"
    million = 1_000_000
    # Haiku 5.5: $0.10 input, $0.50 output per million tokens; batches are half price.
    assert screen.cost_usd({"input_tokens": million}, m, PRICING) == pytest.approx(0.05)
    assert screen.cost_usd({"output_tokens": million}, m, PRICING) == pytest.approx(0.25)
    assert screen.cost_usd({"cache_read_input_tokens": million}, m, PRICING) == pytest.approx(0.005)
    assert screen.cost_usd({"cache_creation_input_tokens": million}, m, PRICING) == pytest.approx(
        0.0625
    )
    assert screen.cost_usd({"input_tokens": million}, m, PRICING, batch=False) == pytest.approx(0.1)
    # BUILD_PLAN estimate: 3,000 articles x (1,500 in + 300 out) on batch = about $0.45.
    month = {"input_tokens": 3000 * 1500, "output_tokens": 3000 * 300}
    assert screen.cost_usd(month, m, PRICING) == pytest.approx(0.45)
    with pytest.raises(KeyError):
        screen.cost_usd({}, "gpt-4", PRICING)


# ---- end to end with a fake Batches API ---------------------------------------------------------


class FakeBatches:
    """Stands in for ClaudeBatches. `answers` maps PMID -> result dict fields."""

    def __init__(self, answers=None, ended=True):
        self.answers = answers or {}
        self.ended = ended
        self.submitted: dict[str, list[dict]] = {}

    def create(self, requests):
        batch_id = f"msgbatch_{len(self.submitted) + 1}"
        self.submitted[batch_id] = requests
        return batch_id

    def status(self, batch_id):
        return {
            "status": "ended" if self.ended else "in_progress",
            "created_at": dt.datetime(2026, 11, 1, 6, 0, tzinfo=dt.UTC),
            "ended_at": dt.datetime(2026, 11, 1, 6, 30, tzinfo=dt.UTC) if self.ended else None,
            "counts": {},
        }

    def results(self, batch_id):
        for req in self.submitted[batch_id]:
            pmid = req["custom_id"].removeprefix("pmid-")
            ans = self.answers.get(pmid, {})
            yield {
                "custom_id": req["custom_id"],
                "type": ans.get("type", "succeeded"),
                "model": "claude-haiku-5-5",
                "stop_reason": ans.get("stop_reason", "end_turn"),
                "text": ans.get("text", good_output()),
                "usage": {
                    "input_tokens": 300,
                    "output_tokens": 250,
                    "cache_creation_input_tokens": 0,
                    "cache_read_input_tokens": 1500,
                },
                "error": ans.get("error"),
            }


def memory_engine():
    """In-memory SQLite that the web test client can use from its own thread."""
    return create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )


@pytest.fixture
def session():
    engine = memory_engine()
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as s:
        yield s


@pytest.fixture
def ingested(session):
    ingest_month(session, FakePubMed(), "2026-10", small_data())
    return session


def test_submit_sends_kept_articles_with_versioned_prompt(ingested):
    fake = FakeBatches()
    run = screen.submit_month(ingested, fake, "2026-10")
    requests = fake.submitted[run.batch_id]
    assert [r["custom_id"] for r in requests] == ["pmid-101", "pmid-103", "pmid-106"]
    params = requests[0]["params"]
    assert params["model"] == "claude-haiku-5-5"
    assert params["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert params["output_config"]["format"]["type"] == "json_schema"
    assert params["output_config"]["effort"] == "low"
    assert "temperature" not in params  # rejected by Haiku 5.5
    user = params["messages"][0]["content"]
    assert "PMID: 101" in user and "core emergency medicine journal" in user
    no_abstract = requests[2]["params"]["messages"][0]["content"]
    assert "Abstract: none" in no_abstract
    assert run.status == "submitted" and run.requested == 3
    assert run.prompt_version == "screening.v1"
    assert run.prompt_sha256 == screen.sha256(params["system"][0]["text"])


def test_collect_stores_every_result_with_model_prompt_cost_and_time(ingested):
    fake = FakeBatches(
        answers={
            "103": {"text": "{broken"},
            "106": {"type": "errored", "text": None, "error": "api_error: overloaded"},
        }
    )
    run = screen.submit_month(ingested, fake, "2026-10")
    assert screen.collect(ingested, fake, run) is True
    assert run.status == "ok"
    assert (run.succeeded, run.failed) == (1, 2)
    rows = {
        a.pmid: s
        for s, a in ingested.execute(
            select(Screening, Article).join(Article, Article.id == Screening.article_id)
        )
    }
    ok = rows["101"]
    assert ok.status == "ok" and ok.relevant is True and ok.design == "rct"
    assert ok.model == "claude-haiku-5-5" and ok.prompt_version == "screening.v1"
    assert ok.high_yield_score == 4 and ok.practice_changing_reason
    assert rows["103"].status == "invalid" and rows["103"].raw_output == "{broken"
    assert rows["106"].status == "errored" and "overloaded" in rows["106"].error
    assert run.input_tokens == 900 and run.cache_read_input_tokens == 4500
    expected = sum(
        screen.cost_usd(
            {"input_tokens": 300, "output_tokens": 250, "cache_read_input_tokens": 1500},
            "claude-haiku-5-5",
            PRICING,
        )
        for _ in range(3)
    )
    assert run.cost_usd == pytest.approx(expected)
    rep = run.report
    assert rep["this_run"]["batch_seconds"] == 30 * 60
    assert rep["this_run"]["results"] == {"ok": 1, "invalid": 1, "errored": 1}
    assert rep["month_totals"]["not_yet_screened"] == 2
    assert rep["shortlist"][0]["pmid"] == "101"
    assert "Shortlist:" in screen.format_report(rep)


def test_rerun_screens_only_failed_articles_and_stops_when_done(ingested):
    fake = FakeBatches(answers={"103": {"stop_reason": "max_tokens"}})
    first = screen.submit_month(ingested, fake, "2026-10")
    screen.collect(ingested, fake, first)
    fake.answers = {}
    second = screen.submit_month(ingested, fake, "2026-10")
    assert [r["custom_id"] for r in fake.submitted[second.batch_id]] == ["pmid-103"]
    screen.collect(ingested, fake, second)
    assert second.report["month_totals"]["screened_ok"] == 3
    assert screen.submit_month(ingested, fake, "2026-10") is None


def test_an_article_that_keeps_failing_is_tried_at_most_three_times(ingested):
    fake = FakeBatches(answers={"103": {"stop_reason": "refusal"}})
    for _ in range(screen.MAX_ATTEMPTS):
        run = screen.submit_month(ingested, fake, "2026-10")
        screen.collect(ingested, fake, run)
    assert screen.submit_month(ingested, fake, "2026-10") is None
    statuses = ingested.scalars(select(Screening.status).where(Screening.status != "ok")).all()
    assert statuses == ["refused"] * screen.MAX_ATTEMPTS


def test_a_new_prompt_version_screens_everything_again(ingested, tmp_path, monkeypatch):
    fake = FakeBatches()
    screen.collect(ingested, fake, screen.submit_month(ingested, fake, "2026-10"))
    v2 = tmp_path / "screening.v2.md"
    v2.write_text("v2\n{{topics}}", encoding="utf-8")
    monkeypatch.setattr(screen, "PROMPTS", tmp_path)
    run = screen.submit_month(ingested, fake, "2026-10", prompt_version="screening.v2")
    assert run.requested == 3


def test_limit_and_pending_batch(ingested):
    fake = FakeBatches(ended=False)
    run = screen.submit_month(ingested, fake, "2026-10", limit=1)
    assert run.requested == 1
    with pytest.raises(RuntimeError, match="still running"):
        screen.submit_month(ingested, fake, "2026-10")
    assert screen.collect(ingested, fake, run) is False
    assert run.status == "submitted"
    assert ingested.scalar(select(func.count()).select_from(Screening)) == 0


def test_submit_needs_an_ingested_month(session):
    with pytest.raises(RuntimeError, match="no successful ingest"):
        screen.submit_month(session, FakeBatches(), "2026-10")


def test_failed_collect_is_recorded(ingested):
    class Broken(FakeBatches):
        def results(self, batch_id):
            raise RuntimeError("results unavailable")

    fake = Broken()
    run = screen.submit_month(ingested, fake, "2026-10")
    with pytest.raises(RuntimeError):
        screen.collect(ingested, fake, run)
    stored = ingested.get(ScreeningRun, run.id)
    assert stored.status == "failed" and "results unavailable" in stored.error
    assert ingested.scalar(select(func.count()).select_from(Screening)) == 0
    # Collecting again by hand, once the API answers, stores the results.
    working = FakeBatches()
    working.submitted = fake.submitted
    assert screen.collect(ingested, working, run) is True
    assert run.status == "ok" and run.succeeded == 3 and run.error is None


# ---- the real SDK wrapper, against a mock HTTP transport (no network) ---------------------------

BATCH = {
    "id": "msgbatch_abc",
    "type": "message_batch",
    "processing_status": "ended",
    "request_counts": {"processing": 0, "succeeded": 1, "errored": 1, "canceled": 0, "expired": 1},
    "created_at": "2026-11-01T06:00:00Z",
    "ended_at": "2026-11-01T06:20:00Z",
    "expires_at": "2026-11-02T06:00:00Z",
    "archived_at": None,
    "cancel_initiated_at": None,
    "results_url": "https://api.test/v1/messages/batches/msgbatch_abc/results",
}
MESSAGE = {
    "id": "msg_1",
    "type": "message",
    "role": "assistant",
    "model": "claude-haiku-5-5",
    "content": [
        {"type": "thinking", "thinking": "", "signature": "sig"},
        {"type": "text", "text": good_output()},
    ],
    "stop_reason": "end_turn",
    "stop_sequence": None,
    "usage": {
        "input_tokens": 310,
        "output_tokens": 240,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 1500,
    },
}
RESULTS = [
    {"custom_id": "pmid-101", "result": {"type": "succeeded", "message": MESSAGE}},
    {
        "custom_id": "pmid-103",
        "result": {
            "type": "errored",
            "error": {
                "type": "error",
                "error": {"type": "invalid_request_error", "message": "bad schema"},
            },
        },
    },
    {"custom_id": "pmid-106", "result": {"type": "expired"}},
]


def test_claude_batches_wrapper_with_the_real_sdk():
    seen = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append((request.method, request.url.path))
        if request.method == "POST":
            body = json.loads(request.content)
            assert body["requests"][0]["params"]["output_config"]["effort"] == "low"
            return httpx2.Response(200, json=BATCH | {"processing_status": "in_progress"})
        if request.url.path.endswith("/results"):
            lines = "\n".join(json.dumps(r) for r in RESULTS)
            return httpx2.Response(200, text=lines, headers={"content-type": "application/x-jsonl"})
        return httpx2.Response(200, json=BATCH)

    client = anthropic.Anthropic(
        api_key="test-key",
        base_url="https://api.test",
        http_client=anthropic.DefaultHttpxClient(transport=httpx2.MockTransport(handler)),
        max_retries=0,
    )
    wrapper = ClaudeBatches(client=client)
    request = {
        "custom_id": "pmid-101",
        "params": {
            "model": "claude-haiku-5-5",
            "max_tokens": 10,
            "messages": [{"role": "user", "content": "x"}],
            "output_config": {"effort": "low"},
        },
    }
    assert wrapper.create([request]) == "msgbatch_abc"
    state = wrapper.status("msgbatch_abc")
    assert state["status"] == "ended" and state["ended_at"].minute == 20
    results = {r["custom_id"]: r for r in wrapper.results("msgbatch_abc")}
    ok = results["pmid-101"]
    assert ok["type"] == "succeeded" and ok["model"] == "claude-haiku-5-5"
    assert json.loads(ok["text"])["design"] == "rct"  # the empty thinking block is skipped
    assert ok["usage"]["cache_read_input_tokens"] == 1500
    assert results["pmid-103"]["error"] == "invalid_request_error: bad schema"
    assert results["pmid-106"]["type"] == "expired"
    assert ("POST", "/v1/messages/batches") in seen


def test_claude_batches_needs_a_key():
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        ClaudeBatches(api_key=None)


def test_result_to_dict_for_a_refusal():
    from anthropic.types.messages import MessageBatchIndividualResponse

    msg = MESSAGE | {"stop_reason": "refusal", "content": []}
    item = MessageBatchIndividualResponse.model_validate(
        {"custom_id": "pmid-1", "result": {"type": "succeeded", "message": msg}}
    )
    out = result_to_dict(item)
    assert out["stop_reason"] == "refusal" and out["text"] is None


def test_a_rejected_batch_is_recorded_with_the_reason(ingested):
    class Rejecting(FakeBatches):
        def create(self, requests):
            raise RuntimeError("400 invalid_request_error: bad schema")

    with pytest.raises(RuntimeError):
        screen.submit_month(ingested, Rejecting(), "2026-10")
    run = ingested.scalar(select(ScreeningRun))
    assert run.status == "failed" and "bad schema" in run.error and run.batch_id is None
    # The next attempt is not blocked by the failed one.
    assert screen.submit_month(ingested, FakeBatches(), "2026-10").status == "submitted"


def test_a_run_stopped_while_submitting_is_marked_interrupted(ingested):
    fake = FakeBatches()
    run = screen.submit_month(ingested, fake, "2026-10")
    run.status, run.batch_id = "submitting", None  # as if the process was killed mid-call
    ingested.commit()
    again = screen.submit_month(ingested, fake, "2026-10")
    assert ingested.get(ScreeningRun, run.id).status == "failed"
    assert "interrupted" in ingested.get(ScreeningRun, run.id).error
    assert again.status == "submitted" and again.requested == 3
