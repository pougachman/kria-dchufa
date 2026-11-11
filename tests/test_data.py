from pathlib import Path

import yaml

DATA = Path(__file__).resolve().parents[1] / "data"


def test_taxonomy_has_33_unique_topics_in_known_groups():
    tax = yaml.safe_load((DATA / "taxonomy.yaml").read_text(encoding="utf-8"))
    ids = [t["id"] for t in tax["topics"]]
    assert len(ids) == 33
    assert len(set(ids)) == len(ids)
    for t in tax["topics"]:
        assert t["group"] in tax["groups"]
        assert t["journals"], t["id"]


def test_sources_file_loads():
    src = yaml.safe_load((DATA / "sources.yaml").read_text(encoding="utf-8"))
    assert "Annals of Emergency Medicine" in src["core_em_journals"]["core"]
