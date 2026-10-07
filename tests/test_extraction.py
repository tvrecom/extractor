from tmdb_extractor.records import content_hash
from tests.support import (
    FakeTMDB, add_movie, add_series, make_settings, read_jsonl, run_once,
)


def out(tmp_path, name):
    return read_jsonl(tmp_path / "data" / "extracted" / f"{name}.jsonl")


def test_golden_movie_record(tmp_path):
    fake = FakeTMDB()
    add_movie(fake, 550)
    run_once(make_settings(tmp_path), fake)
    (rec,) = out(tmp_path, "movies")
    cast = [{"name": "Ann Actor", "role": "Hero"}]
    assert rec["id"] == "title_movie_550" and rec["tmdb_id"] == 550
    assert rec["imdb_id"] == "tt0000550" and rec["type"] == "movie"
    assert rec["title"] == "Movie 550" and rec["synopsis"] == "A film."
    assert rec["genres"] == ["Action"] and rec["cast"] == cast
    assert rec["director"] == ["Dee Rector"]
    assert rec["release_date"] == "2023-05-01" and rec["runtime_minutes"] == 101
    assert rec["maturity_rating"] == "PG-13"  # US theatrical, not DE / empty cert
    assert rec["parent_id"] is rec["season"] is rec["episode"] is None
    assert rec["license_expiry"] is None and rec["transcript_ref"] is None
    assert rec["content_hash"] == content_hash("A film.", cast, ["Action"])
    assert rec["source_system"] == "tmdb" and rec["status"] == "active"
    assert (tmp_path / "data" / rec["_source"]["raw_response"]).is_file()


def test_golden_series_with_specials_and_episodes(tmp_path):
    fake = FakeTMDB()
    add_series(fake, 1399, seasons=(0, 1))
    run_once(make_settings(tmp_path), fake)
    (series,) = out(tmp_path, "series")
    eps = out(tmp_path, "episodes")
    assert series["id"] == "title_tv_1399" and series["type"] == "series"
    assert series["maturity_rating"] == "TV-MA" and series["runtime_minutes"] == 45
    assert series["release_date"] == "2020-01-01"
    assert [e["id"] for e in eps] == [
        "title_tv_1399_s0_e1", "title_tv_1399_s0_e2",
        "title_tv_1399_s1_e1", "title_tv_1399_s1_e2"]
    e = eps[2]
    assert e["parent_id"] == "title_tv_1399" and e["season"] == 1 and e["episode"] == 1
    assert e["type"] == "episode" and e["director"] == ["Ep Director"]
    assert e["cast"] == [{"name": "Guest", "role": "Cameo"}]
    assert e["genres"] == series["genres"] and e["runtime_minutes"] == 45
    assert (tmp_path / "data" / e["_source"]["raw_response"]).is_file()


def test_movie_and_tv_with_same_tmdb_id_do_not_collide(tmp_path):
    fake = FakeTMDB()
    add_movie(fake, 5)
    add_series(fake, 5)
    run_once(make_settings(tmp_path), fake)
    ids = {r["id"] for r in out(tmp_path, "movies") + out(tmp_path, "series")}
    assert ids == {"title_movie_5", "title_tv_5"}


def test_malformed_title_fails_alone_and_batch_continues(tmp_path):
    fake = FakeTMDB()
    add_movie(fake, 1)
    add_movie(fake, 2, title=None)  # malformed: no title
    add_movie(fake, 3)
    summary = run_once(make_settings(tmp_path), fake)
    assert {r["tmdb_id"] for r in out(tmp_path, "movies")} == {1, 3}
    (failure,) = out(tmp_path, "failures")
    assert failure["tmdb_id"] == 2 and failure["stage"] == "validate"
    assert summary.extraction.attempted == 3
    assert summary.extraction.succeeded == 2 and summary.extraction.failed == 1
    assert summary.status == "partial"


def test_missing_optional_fields_emit_but_are_flagged(tmp_path):
    fake = FakeTMDB()
    add_movie(fake, 7, overview="", credits={"cast": [], "crew": []})
    summary = run_once(make_settings(tmp_path), fake)
    assert len(out(tmp_path, "movies")) == 1
    assert summary.extraction.flagged == 1 and summary.extraction.failed == 0


def test_genre_ids_resolved_from_cache_when_names_absent(tmp_path):
    fake = FakeTMDB()
    add_movie(fake, 9, genres=[], genre_ids=[28])
    run_once(make_settings(tmp_path), fake)
    assert out(tmp_path, "movies")[0]["genres"] == ["Action"]
    assert sum(1 for p, _ in fake.calls if p.startswith("genre/")) == 2  # once per type


def test_partial_season_failure_emits_nothing_then_retry_emits_once(tmp_path):
    from tmdb_extractor.errors import TMDBRequestError

    fake = FakeTMDB()
    add_series(fake, 10, seasons=(1, 2))
    good = fake.routes["tv/10/season/2"]
    fake.routes["tv/10/season/2"] = TMDBRequestError("HTTP 500", attempts=2, status_code=500)
    settings = make_settings(tmp_path)
    first = run_once(settings, fake)
    assert out(tmp_path, "series") == [] and out(tmp_path, "episodes") == []
    assert first.extraction.failed == 1
    assert out(tmp_path, "failures")[0]["stage"] == "child_fetch"

    fake.routes["tv/10/season/2"] = good
    second = run_once(settings, fake)
    assert second.extraction.succeeded == 1 and second.extraction.retried == 1
    assert len(out(tmp_path, "series")) == 1 and len(out(tmp_path, "episodes")) == 4


def test_404_detail_marks_gone_and_is_never_retried(tmp_path):
    fake = FakeTMDB()
    fake.add_titles("movie", [404])  # discoverable but detail route missing -> 404
    s1 = run_once(make_settings(tmp_path), fake)
    s2 = run_once(make_settings(tmp_path), fake)
    assert s1.extraction.gone == 1 and s2.extraction.attempted == 0
    assert s2.snapshot["backlog"]["gone"] == 1
