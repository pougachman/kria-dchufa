import httpx
import pytest

from kria.pipeline import eutils as eu_mod
from kria.pipeline.eutils import BASE_URL, EUtils, EUtilsError


def client(handler, **kw):
    http = httpx.Client(base_url=BASE_URL, transport=httpx.MockTransport(handler))
    return EUtils(http=http, min_interval=0, **kw)


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(eu_mod.time, "sleep", lambda s: None)


def esearch(ids, count, translation="q"):
    return {"esearchresult": {"count": str(count), "idlist": ids, "querytranslation": translation}}


def test_search_pages_and_sorts(monkeypatch):
    monkeypatch.setattr(eu_mod, "PAGE_SIZE", 2)
    pages = {None: ["5", "3"], "2": ["4", "1"], "4": ["2"]}

    def handler(req):
        assert req.url.params["tool"] == "kria-dchufa"
        assert req.url.params["api_key"] == "k"
        return httpx.Response(200, json=esearch(pages[req.url.params.get("retstart")], 5))

    ids, translation = client(handler, api_key="k").search("pubmed", "x")
    assert ids == ["1", "2", "3", "4", "5"] and translation == "q"


def test_search_refuses_to_truncate():
    def handler(req):
        return httpx.Response(200, json=esearch([], 10_000))

    with pytest.raises(EUtilsError, match="limit"):
        client(handler).search("pubmed", "x")


def test_error_payload_raises():
    def handler(req):
        return httpx.Response(200, json={"error": "API key invalid"})

    with pytest.raises(EUtilsError, match="API key invalid"):
        client(handler).search("pubmed", "x")


def test_retries_server_errors_then_succeeds():
    calls = []

    def handler(req):
        calls.append(1)
        if len(calls) < 3:
            return httpx.Response(503)
        return httpx.Response(200, json=esearch(["1"], 1))

    assert client(handler).search("pubmed", "x")[0] == ["1"]
    assert len(calls) == 3


def test_client_errors_are_not_retried():
    calls = []

    def handler(req):
        calls.append(1)
        return httpx.Response(400)

    with pytest.raises(EUtilsError):
        client(handler).search("pubmed", "x")
    assert len(calls) == 1


def test_fetch_posts_in_batches(monkeypatch):
    monkeypatch.setattr(eu_mod, "FETCH_BATCH", 2)
    seen = []

    def handler(req):
        assert req.method == "POST"
        seen.append(dict(httpx.QueryParams(req.content.decode()))["id"])
        return httpx.Response(200, content=b"<PubmedArticleSet/>")

    chunks = list(client(handler).fetch_pubmed_xml(["1", "2", "3"]))
    assert seen == ["1,2", "3"] and len(chunks) == 2
