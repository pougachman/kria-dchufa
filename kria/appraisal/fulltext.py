"""Full text for appraisal, only through authorized routes (CLAUDE.md rule 6).

1. Open access, automatic: Europe PMC full-text XML (the PMC open-access subset), then an
   open-access PDF that Unpaywall points to.
2. Otherwise the requesting editor downloads the PDF with their own access and uploads it.
   The upload is checked against the article (DOI or title) before it is used.
3. Otherwise an abstract-only appraisal, labelled limited.

Never: library portals, automated downloads of subscription content, anyone's credentials.

The text is kept as "units" with an anchor (a PDF page, or a section or table of the XML), so
every quote the model gives can be checked against the place it says it came from.
"""

from __future__ import annotations

import io
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

import httpx

EUROPE_PMC = "https://www.ebi.ac.uk/europepmc/webservices/rest"
UNPAYWALL = "https://api.unpaywall.org/v2"
MAX_PDF_BYTES = 40 * 1024 * 1024
USER_AGENT = "kria-dchufa/1.0 (literature appraisal for EM residency programs)"


@dataclass
class Unit:
    anchor: str  # "p. 3", "Results", "Table 2", "Abstract"
    text: str


@dataclass
class FullText:
    kind: str  # pmc_xml | oa_pdf | upload | abstract
    units: list[Unit]
    url: str | None = None
    pdf: bytes | None = None
    raw: bytes | None = None  # the source file as received (PMC XML)
    notes: list[str] = field(default_factory=list)

    def joined(self) -> str:
        return "\n".join(u.text for u in self.units)


def _text(el: ET.Element | None) -> str:
    return " ".join("".join(el.itertext()).split()) if el is not None else ""


# ---- parsing -----------------------------------------------------------------------------------


def units_from_jats(xml: bytes) -> list[Unit]:
    """Abstract, each top-level body section, and each table of a JATS (PMC) article."""
    root = ET.fromstring(xml)
    units: list[Unit] = []
    abstract = root.find(".//front//abstract")
    if abstract is not None and _text(abstract):
        units.append(Unit("Abstract", _text(abstract)))
    body = root.find(".//body")
    if body is not None:
        for i, sec in enumerate(body.findall("sec"), 1):
            title = _text(sec.find("title")) or f"Section {i}"
            parts = []
            for el in sec.iter():
                if el.tag == "p" or el.tag == "title":
                    parts.append(_text(el))
            text = " ".join(p for p in parts if p)
            if text:
                units.append(Unit(title, text))
        loose = [_text(p) for p in body.findall("p")]
        if any(loose):
            units.insert(1 if units else 0, Unit("Introduction", " ".join(loose)))
    for i, tw in enumerate(root.iter("table-wrap"), 1):
        label = _text(tw.find("label")) or f"Table {i}"
        rows = []
        for tr in tw.iter("tr"):
            cells = [_text(c) for c in tr if c.tag in ("td", "th")]
            rows.append(" | ".join(cells))
        caption = _text(tw.find("caption"))
        text = " ".join(x for x in [caption, " ; ".join(rows)] if x)
        if text:
            units.append(Unit(label, text))
    for fn in root.iter("fn-group"):
        if _text(fn):
            units.append(Unit("Footnotes and disclosures", _text(fn)))
    return units


def units_from_pdf(pdf: bytes) -> list[Unit]:
    """One unit per page, numbered as in the PDF viewer (1 = first page of the file)."""
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(pdf))
    return [
        Unit(f"p. {i}", " ".join((page.extract_text() or "").split()))
        for i, page in enumerate(reader.pages, 1)
    ]


def is_pdf(data: bytes) -> bool:
    return data[:5] == b"%PDF-"


def matches_article(text: str, doi: str | None, title: str) -> tuple[bool, str]:
    """Does this text (the first pages of an uploaded PDF) belong to the article?"""
    flat = " ".join(text.lower().split())
    if doi:
        d = doi.lower()
        if d in flat or d in flat.replace(" ", ""):
            return True, "DOI found in the file"
    words = [w for w in re.findall(r"[a-z0-9]+", title.lower()) if len(w) > 3]
    if not words:
        return False, "the title has no words to compare"
    found = sum(1 for w in words if w in flat)
    share = found / len(words)
    if share >= 0.7:
        return True, f"{found} of {len(words)} title words found in the file"
    return False, f"neither the DOI nor the title was found in the file ({found}/{len(words)})"


# ---- open-access lookup ------------------------------------------------------------------------


class OpenAccess:
    """Looks for a legal open-access full text. Tests pass an httpx client with a mock transport."""

    def __init__(self, unpaywall_email: str | None = None, http: httpx.Client | None = None):
        self.email = unpaywall_email
        self.http = http or httpx.Client(
            timeout=60, follow_redirects=True, headers={"User-Agent": USER_AGENT}
        )

    def find(self, pmcid: str | None, doi: str | None) -> tuple[FullText | None, dict]:
        """(full text or None, a log of what was tried, for the editor screen)."""
        log: dict = {}
        if not pmcid and doi:
            pmcid = self._pmcid_for_doi(doi, log)
        if pmcid:
            r = self.http.get(f"{EUROPE_PMC}/{pmcid}/fullTextXML")
            log["europe_pmc"] = f"{pmcid}: HTTP {r.status_code}"
            if r.status_code == 200 and r.content.strip().startswith(b"<"):
                units = units_from_jats(r.content)
                if sum(len(u.text) for u in units) > 2000:
                    url = f"https://europepmc.org/article/PMC/{pmcid}"
                    return FullText("pmc_xml", units, url=url, raw=r.content), log
                log["europe_pmc"] += " (no body text)"
        if doi and self.email:
            found = self._unpaywall_pdf(doi, log)
            if found:
                return found, log
        elif doi:
            log["unpaywall"] = "skipped: UNPAYWALL_EMAIL is not set"
        return None, log

    def _pmcid_for_doi(self, doi: str, log: dict) -> str | None:
        r = self.http.get(
            f"{EUROPE_PMC}/search",
            params={"query": f'DOI:"{doi}"', "format": "json", "resultType": "lite"},
        )
        if r.status_code != 200:
            log["europe_pmc_search"] = f"HTTP {r.status_code}"
            return None
        hits = r.json().get("resultList", {}).get("result", [])
        pmcid = next((h.get("pmcid") for h in hits if h.get("pmcid")), None)
        log["europe_pmc_search"] = pmcid or "no PMC copy"
        return pmcid

    def _unpaywall_pdf(self, doi: str, log: dict) -> FullText | None:
        r = self.http.get(f"{UNPAYWALL}/{doi}", params={"email": self.email})
        if r.status_code != 200:
            log["unpaywall"] = f"HTTP {r.status_code}"
            return None
        data = r.json()
        loc = data.get("best_oa_location") or {}
        url = loc.get("url_for_pdf")
        if not data.get("is_oa") or not url:
            log["unpaywall"] = "no open-access PDF"
            return None
        pdf = self.http.get(url)
        if pdf.status_code != 200 or not is_pdf(pdf.content) or len(pdf.content) > MAX_PDF_BYTES:
            log["unpaywall"] = f"open-access PDF not downloadable (HTTP {pdf.status_code})"
            return None
        log["unpaywall"] = f"open-access PDF ({loc.get('license') or 'license not given'})"
        return FullText("oa_pdf", units_from_pdf(pdf.content), url=url, pdf=pdf.content)
