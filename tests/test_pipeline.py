import json
import sqlite3

from tmdb_extractor.budget import RunBudget
from tmdb_extractor.cli import RunLock, main
from tmdb_extractor.errors import TMDBRequestError
from tests.support import (
    API_KEY, FakeTMDB, add_movie, make_settings, read_jsonl, run_once,
)


def test_detail_cap_never_loses_enumerated_ids(tmp_path):
    """Regression for C1: ids beyond the per-run cap stay queued, not discarded."""
    fake = FakeTMDB()
    for i in range(1, 9):
        add_movie(fake, i)
    settings = make_settings(tmp_path, max_details=3)
    seen = []
    for _ in range(3):
        summary = run_once(settings, fake)
        seen.append(summary.extraction.attempted)
    assert seen == [3, 3, 2]
    assert len(read_jsonl(tmp_path / "data/extracted/movies.jsonl")) == 8


def test_summary_invariants_and_cumulative_progress_across_runs(tmp_path):
    fake = FakeTMDB()
    for i in range(1, 6):
        add_movie(fake, i)
    settings = make_settings(tmp_path, max_details=2)
    s1 = run_once(settings, fake)
    s2 = run_once(settings, fake)
    for s in (s1, s2):
        e = s.extraction
        assert e.attempted == e.succeeded + e.failed
    assert s1.snapshot["backlog"]["done"] == 2
    assert s2.snapshot["backlog"]["done"] == 4  # cumulative, not reset per run
    assert s2.snapshot["progress"]["movie"]["estimated_total"] == 5
    assert s2.snapshot["progress"]["movie"]["percent"] == 80.0
    lines = read_jsonl(tmp_path / "data/run_summaries.jsonl")
    assert len(lines) == 2 and lines[1]["extraction"]["attempted"] == 2


def test_persistent_failure_surfaces_in_retry_pass_until_exhausted(tmp_path):
    fake = FakeTMDB()
    add_movie(fake, 1)
    fake.routes["movie/1"] = TMDBRequestError("HTTP 500", attempts=2, status_code=500)
    settings = make_settings(tmp_path, max_title_retries=2)
    s1, s2, s3 = (run_once(settings, fake) for _ in range(3))
    assert (s1.extraction.failed, s2.extraction.failed, s2.extraction.retried) == (1, 1, 1)
    assert s3.extraction.attempted == 0  # exhausted: parked, but visible
    assert s3.snapshot["backlog"]["failed_exhausted"] == 1


def test_enumeration_failure_does_not_block_extraction_and_summary_still_written(tmp_path):
    fake = FakeTMDB()
    add_movie(fake, 1)
    # Seed the queue with one page only (frontier stays open), then break discover.
    run_once(make_settings(tmp_path, max_details=0, max_enum_pages=1), fake)

    class Broken(FakeTMDB):
        def get_json(self, path, params=None):
            if path.startswith("discover/"):
                raise RuntimeError("boom")
            return fake.get_json(path, params)

    summary = run_once(make_settings(tmp_path), Broken())
    assert summary.status == "error" and any("enumeration" in e for e in summary.errors)
    assert summary.extraction.succeeded == 1
    assert len(read_jsonl(tmp_path / "data/run_summaries.jsonl")) == 2


def test_deadline_stops_dispatch_and_leaves_work_queued(tmp_path):
    fake = FakeTMDB()
    for i in range(1, 4):
        add_movie(fake, i)
    settings = make_settings(tmp_path, max_details=0)
    run_once(settings, fake)  # enumerate only

    from tmdb_extractor.cli import build_pipeline
    pipeline, db = build_pipeline(make_settings(tmp_path), fake)
    ticks = iter([0.0] + [5.0] * 1000)  # first read starts the run, then time jumps
    pipeline._budget_factory = lambda: RunBudget(10, 10, max_seconds=1, clock=lambda: next(ticks))

    summary = pipeline.run()
    db.close()
    assert summary.extraction.attempted == 0
    assert summary.snapshot["backlog"]["pending"] == 3


def test_run_lock_prevents_overlap(tmp_path):
    a, b = RunLock(tmp_path / "l"), RunLock(tmp_path / "l")
    assert a.acquire() and not b.acquire()
    a.release()
    assert b.acquire()
    b.release()


def test_main_exits_zero_when_another_run_holds_the_lock(tmp_path):
    env = {"TMDB_API_KEY": API_KEY, "TMDB_DATA_DIR": str(tmp_path / "d")}
    (tmp_path / "d").mkdir()
    holder = RunLock(tmp_path / "d" / "run.lock")
    assert holder.acquire()
    assert main(env, client=FakeTMDB()) == 0
    assert not (tmp_path / "d" / "run_summaries.jsonl").exists()
    holder.release()


def test_api_key_never_reaches_disk_or_logs(tmp_path, caplog):
    import requests
    from tmdb_extractor.http_client import RetryPolicy, TMDBClient

    class Exploding:
        def get(self, url, params=None, timeout=None):
            raise requests.ConnectionError(f"HTTPSConnectionPool ... ?api_key={API_KEY}&page=1")

    client = TMDBClient(API_KEY, "https://x/3", 1, RetryPolicy(2, 0.0, jitter=0.0),
                        session_factory=Exploding, sleeper=lambda s: None)
    settings = make_settings(tmp_path)
    # Seed one title via a fake, then fail extraction through the exploding client.
    fake = FakeTMDB()
    add_movie(fake, 1)
    run_once(make_settings(tmp_path, max_details=0), fake)
    with caplog.at_level("DEBUG"):
        summary = run_once(settings, client)
    assert summary.errors  # enumeration/genres failed loudly...
    blob = b"".join(p.read_bytes() for p in (tmp_path / "data").rglob("*") if p.is_file())
    assert API_KEY.encode() not in blob
    assert API_KEY not in "\n".join(r.getMessage() for r in caplog.records)
    assert API_KEY not in json.dumps(summary.to_dict())


def test_legacy_state_migration(tmp_path):
    data = tmp_path / "data"
    (data / "raw" / "movie").mkdir(parents=True)
    (data / "raw" / "tv" / "3").mkdir(parents=True)
    (data / "raw" / "movie" / "1.json").write_text("{}")
    (data / "raw" / "tv" / "3.json").write_text(json.dumps({"seasons": [{"season_number": 1}]}))
    # tv 3 is missing its season_1.json -> partially extracted -> must be redone
    con = sqlite3.connect(data / "state.sqlite3")
    con.executescript("""
        CREATE TABLE seen_titles (tmdb_id INTEGER, type TEXT, first_seen_at TEXT, PRIMARY KEY (tmdb_id, type));
        CREATE TABLE enumeration_checkpoint (id INTEGER PRIMARY KEY, phase TEXT, year INTEGER, page INTEGER, updated_at TEXT);
        CREATE TABLE failures (tmdb_id INTEGER, type TEXT, stage TEXT, error TEXT, attempts INTEGER, created_at TEXT);
        INSERT INTO seen_titles VALUES (1,'movie','t'),(2,'movie','t'),(3,'tv','t'),(4,'movie','t');
        INSERT INTO failures VALUES (4,'movie','detail_extraction','boom',3,'t');
        INSERT INTO enumeration_checkpoint VALUES (1,'release_movie',2024,3,'t');
    """)
    con.commit()
    con.close()

    from tmdb_extractor.cli import build_pipeline
    pipeline, db = build_pipeline(make_settings(tmp_path), FakeTMDB())
    pipeline._bootstrap()
    pipeline._bootstrap()  # idempotent
    status = {(r["tmdb_id"], r["type"]): r["status"]
              for r in db.query("SELECT tmdb_id, type, status FROM titles")}
    assert status == {(1, "movie"): "done", (2, "movie"): "pending",
                      (3, "tv"): "pending", (4, "movie"): "failed"}
    windows = {(w["title_type"], w["kind"], w["date_gte"][:4]): (w["status"], w["next_page"])
               for w in db.query("SELECT * FROM enum_windows")}
    assert windows[("movie", "popularity", "")][0] == "complete"
    assert windows[("tv", "popularity", "")][0] == "complete"
    assert windows[("movie", "date", "2024")] == ("pending", 1)  # restart: sort changed
    db.close()
