"""Small client for NCBI E-utilities (PubMed and the NLM Catalog).

Follows the NCBI usage rules: at most 3 requests per second without an API key and 10 with one,
the tool name (and an email, when configured) on every request, and POST for long ID lists.
See https://www.ncbi.nlm.nih.gov/books/NBK25497/ and https://www.ncbi.nlm.nih.gov/books/NBK25499/
"""

from __future__ import annotations

import time
from collections.abc import Iterable, Iterator

import httpx

BASE_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/"

# PubMed esearch returns at most 10,000 IDs for one query. A monthly per-topic query is far
# below that; if a query ever exceeds it we fail loudly instead of silently truncating.
ESEARCH_MAX = 9999
PAGE_SIZE = 5000
FETCH_BATCH = 200


class EUtilsError(RuntimeError):
    pass


class EUtils:
    def __init__(
        self,
        api_key: str | None = None,
        email: str | None = None,
        tool: str = "kria-dchufa",
        http: httpx.Client | None = None,
        min_interval: float | None = None,
        retries: int = 3,
    ):
        self.api_key = api_key
        self.email = email
        self.tool = tool
        self.http = http or httpx.Client(base_url=BASE_URL, timeout=60.0)
        if min_interval is None:
            min_interval = 0.11 if api_key else 0.34
        self.min_interval = min_interval
        self.retries = retries
        self._last = 0.0

    def _params(self, params: dict) -> dict:
        p = {k: v for k, v in params.items() if v is not None}
        p["tool"] = self.tool
        if self.email:
            p["email"] = self.email
        if self.api_key:
            p["api_key"] = self.api_key
        return p

    def _wait(self) -> None:
        delay = self.min_interval - (time.monotonic() - self._last)
        if delay > 0:
            time.sleep(delay)
        self._last = time.monotonic()

    def _request(self, endpoint: str, params: dict, post: bool = False) -> httpx.Response:
        last_error: Exception | None = None
        for attempt in range(self.retries):
            self._wait()
            try:
                if post:
                    resp = self.http.post(endpoint, data=self._params(params))
                else:
                    resp = self.http.get(endpoint, params=self._params(params))
            except httpx.TransportError as exc:
                last_error = exc
            else:
                if resp.status_code == 200:
                    return resp
                if resp.status_code not in (429, 500, 502, 503, 504):
                    raise EUtilsError(f"{endpoint} returned HTTP {resp.status_code}")
                last_error = EUtilsError(f"{endpoint} returned HTTP {resp.status_code}")
            time.sleep(2**attempt)
        raise EUtilsError(f"{endpoint} failed after {self.retries} attempts: {last_error}")

    def _json(self, endpoint: str, params: dict, post: bool = False) -> dict:
        data = self._request(endpoint, {**params, "retmode": "json"}, post=post).json()
        if "error" in data:
            raise EUtilsError(f"{endpoint}: {data['error']}")
        return data

    def search(self, db: str, term: str) -> tuple[list[str], str]:
        """All IDs matching `term`, sorted ascending, plus PubMed's translation of the query."""
        first = self._json("esearch.fcgi", {"db": db, "term": term, "retmax": PAGE_SIZE})
        result = first["esearchresult"]
        if "ERROR" in result:
            raise EUtilsError(f"esearch error: {result['ERROR']}")
        count = int(result["count"])
        if count > ESEARCH_MAX:
            raise EUtilsError(f"query returns {count} records, above the {ESEARCH_MAX} limit")
        ids = list(result["idlist"])
        start = len(ids)
        while start < count:
            page = self._json(
                "esearch.fcgi", {"db": db, "term": term, "retmax": PAGE_SIZE, "retstart": start}
            )["esearchresult"]["idlist"]
            if not page:
                break
            ids.extend(page)
            start += len(page)
        if len(set(ids)) != count:
            raise EUtilsError(f"expected {count} IDs, got {len(set(ids))}")
        return sorted(set(ids), key=int), result.get("querytranslation", "")

    def summary(self, db: str, ids: Iterable[str]) -> dict:
        ids = list(ids)
        if not ids:
            return {}
        return self._json("esummary.fcgi", {"db": db, "id": ",".join(ids)}, post=True)["result"]

    def fetch_pubmed_xml(self, pmids: Iterable[str]) -> Iterator[bytes]:
        """PubMed XML in batches of FETCH_BATCH records."""
        pmids = list(pmids)
        for i in range(0, len(pmids), FETCH_BATCH):
            batch = pmids[i : i + FETCH_BATCH]
            resp = self._request(
                "efetch.fcgi",
                {"db": "pubmed", "id": ",".join(batch), "retmode": "xml"},
                post=True,
            )
            yield resp.content
