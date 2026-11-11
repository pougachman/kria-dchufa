import yaml

from kria.pipeline.journals import (
    DATA,
    journal_names,
    load_yaml,
    match_rank,
    normalize,
    resolve,
)


def test_normalize():
    assert normalize("The Lancet") == "lancet"
    assert normalize("Lancet (London, England)") == "lancet"
    assert normalize("Wilderness & Environmental Medicine") == "wilderness environmental medicine"
    assert normalize("JAMA Otolaryngology–Head & Neck Surgery") == normalize(
        "JAMA otolaryngology-- head & neck surgery"
    )


def rec(title, current=True, ta="X", indexed=True, nlm_id="1"):
    return {
        "title": title,
        "current": current,
        "medline_ta": ta,
        "medline_indexed": indexed,
        "nlm_id": nlm_id,
        "issn": [],
    }


def test_match_rank_uses_only_current_main_titles():
    assert match_rank("injury", rec("injury")) == 0
    assert (
        match_rank(
            "world journal of emergency surgery", rec("world journal of emergency surgery wjes")
        )
        == 1
    )
    assert match_rank("injury", rec("injury prevention")) == 1  # subtitle or sister journal
    assert match_rank("injury", rec("injury", current=False)) is None  # ceased title
    assert match_rank("injury", rec("injury", ta="")) is None  # not searchable in PubMed
    assert match_rank("injury", rec("journal of injury")) is None


class FakeCatalog:
    def __init__(self, records):
        self.records = records

    def search(self, db, term):
        return [r["nlm_id"] for r in self.records], term

    def summary(self, db, ids):
        out = {"uids": list(ids)}
        for r in self.records:
            out[r["nlm_id"]] = {
                "nlmuniqueid": r["nlm_id"],
                "medlineta": r["medline_ta"],
                "titlemainsort": r["title"],
                "issnlist": [],
                "currentindexingstatus": "Y" if r["medline_indexed"] else "N",
                "endyear": "9999" if r["current"] else "1990",
            }
        return out


def test_resolve_prefers_exact_title_over_longer_titles():
    eu = FakeCatalog(
        [
            rec("injury prevention", ta="Inj Prev", nlm_id="2"),
            rec("injury", ta="Injury", nlm_id="3"),
        ]
    )
    assert resolve(eu, "Injury")["medline_ta"] == "Injury"


def test_resolve_two_longer_titles_is_ambiguous():
    eu = FakeCatalog(
        [
            rec("critical care medicine", ta="Crit Care Med", nlm_id="2"),
            rec("critical care the official journal", ta="Crit Care", nlm_id="3"),
        ]
    )
    assert resolve(eu, "Critical Care")["status"] == "ambiguous"


def test_every_journal_in_data_is_resolved():
    """Adding a journal without running `python -m kria.pipeline.journals` fails here."""
    resolved = yaml.safe_load((DATA / "journals.yaml").read_text(encoding="utf-8"))
    names = journal_names(load_yaml("taxonomy.yaml"), load_yaml("sources.yaml"))
    assert not resolved["problems"]
    assert set(names) <= set(resolved["journals"])
    for name in names:
        assert resolved["journals"][name]["medline_ta"], name
