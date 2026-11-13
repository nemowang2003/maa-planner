from maa_planner.daemon.tools import stage_catalog


def test_catalog_tables_share_one_commit_and_existing_snapshot_is_reused(tmp_path, monkeypatch):
    reads = []
    sha = "a" * 40

    def get(url):
        reads.append(url)
        return {"sha": sha} if url.endswith("commits/master") else {"table": url}

    monkeypatch.setattr(stage_catalog, "_get", get)
    first = stage_catalog.load_stage_catalog(tmp_path)
    assert len(reads) == 4
    assert reads[0].endswith("/repos/ArknightsAssets/ArknightsGamedata/commits/master")
    assert all("/cn/gamedata/excel/" in url for url in reads[1:])
    assert all("/" + sha + "/" in url for url in reads[1:])
    assert first["commit"] == sha
    assert stage_catalog.load_stage_catalog(tmp_path) == first
    assert len(reads) == 5  # A version check, with no re-download of the tables.


def test_failed_download_never_publishes_a_partial_snapshot(tmp_path, monkeypatch):
    import pytest

    def get(url):
        if url.endswith("commits/master"):
            return {"sha": "b" * 40}
        raise OSError("offline")

    monkeypatch.setattr(stage_catalog, "_get", get)
    with pytest.raises(OSError, match="offline"):
        stage_catalog.load_stage_catalog(tmp_path)
    assert not list(tmp_path.rglob("*.json"))


def test_incomplete_cached_document_is_replaced_instead_of_reused(tmp_path, monkeypatch):
    root = tmp_path / "stage-catalog"
    root.mkdir()
    sha = "c" * 40
    (root / (sha + ".json")).write_text('{"commit":"' + sha + '"}')

    def get(url):
        return {"sha": sha} if url.endswith("commits/master") else {"valid": True}

    monkeypatch.setattr(stage_catalog, "_get", get)
    assert stage_catalog.load_stage_catalog(tmp_path)["stage_table"] == {"valid": True}


def test_processing_recipes_and_rarity_use_the_requested_immutable_commit(tmp_path, monkeypatch):
    sha = "f" * 40
    reads = []

    def get(url):
        reads.append(url)
        return {"source": url}

    monkeypatch.setattr(stage_catalog, "_get", get)
    first = stage_catalog.load_processing_catalog(tmp_path, sha)
    assert first["commit"] == sha
    assert len(reads) == 2
    assert all("/" + sha + "/cn/gamedata/excel/" in url for url in reads)
    assert {url.rsplit("/", 1)[-1] for url in reads} == {"building_data.json", "item_table.json"}
    assert stage_catalog.load_processing_catalog(tmp_path, sha) == first
    assert len(reads) == 2
