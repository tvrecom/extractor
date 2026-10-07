import pytest

from tmdb_extractor.budget import RunBudget
from tmdb_extractor.enumeration import DiscoverQueryBuilder, Enumerator, WindowSeeder
from tmdb_extractor.profiles import default_profiles
from tmdb_extractor.state import Database, TitleQueue, WindowRepository
from tests.support import FakeTMDB


class Rig:
    def __init__(self, tmp_path, fake, max_pages=500, popularity_pages=2, years=(2023, 2023)):
        self.db = Database(tmp_path / "s.sqlite3")
        self.titles, self.windows = TitleQueue(self.db), WindowRepository(self.db)
        self.profiles = default_profiles()
        self.fake = fake
        WindowSeeder(self.windows, self.profiles, *years).seed()
        self.enum = Enumerator(fake, self.windows, self.titles, self.db, self.profiles,
                               DiscoverQueryBuilder("en-US"), popularity_pages, max_pages)

    def run(self, pages):
        return self.enum.run(RunBudget(pages, 0))

    def drain(self, per_run=3, limit=500):
        for _ in range(limit):
            if self.run(per_run).complete:
                return
        raise AssertionError("enumeration never finished")

    def ids(self, kind):
        return {r["tmdb_id"] for r in self.db.query("SELECT tmdb_id FROM titles WHERE type=?", (kind,))}


def spread(fake, kind, n, start=1):
    for i in range(n):
        fake.add_titles(kind, [start + i], date=f"2023-{(i % 12) + 1:02d}-{(i % 27) + 1:02d}")


def test_stop_and_resume_yields_no_duplicates_and_full_coverage(tmp_path):
    fake = FakeTMDB(page_size=5)
    spread(fake, "movie", 23)
    spread(fake, "tv", 12, start=1000)
    rig = Rig(tmp_path, fake)
    total_new = 0
    while True:
        r = rig.run(2)  # tiny budget forces many interrupted runs
        total_new += r.new_ids
        if r.complete:
            break
    assert rig.ids("movie") == {t["id"] for t in fake.universe["movie"]}
    assert rig.ids("tv") == {t["id"] for t in fake.universe["tv"]}
    assert total_new == 23 + 12  # every id reported new exactly once


def test_resume_continues_at_exact_next_page(tmp_path):
    fake = FakeTMDB(page_size=5)
    spread(fake, "movie", 30)
    rig = Rig(tmp_path, fake, popularity_pages=3)
    rig.run(2)
    w = rig.windows.next_open()
    assert (w.title_type, w.kind, w.next_page) == ("movie", "popularity", 3)
    fake.calls.clear()
    rig.run(1)
    assert fake.discover_calls()[0]["page"] == 3


def test_date_windows_use_stable_ascending_sort(tmp_path):
    fake = FakeTMDB(page_size=5)
    spread(fake, "movie", 6)
    rig = Rig(tmp_path, fake)
    rig.drain()
    date_calls = [p for p in fake.discover_calls() if "primary_release_date.gte" in p]
    assert date_calls and all(p["sort_by"] == "primary_release_date.asc" for p in date_calls)


def test_oversized_window_is_bisected_and_fully_covered(tmp_path):
    fake = FakeTMDB(page_size=2, max_page=3)  # 6 results max per query
    spread(fake, "movie", 20)
    rig = Rig(tmp_path, fake, max_pages=3)
    rig.drain()
    assert rig.ids("movie") == {t["id"] for t in fake.universe["movie"]}
    stats = rig.windows.stats()
    assert stats["split"] >= 1 and stats["truncated"] == 0
    leaves = rig.db.query("SELECT SUM(total_results) AS n FROM enum_windows "
                          "WHERE kind='date' AND title_type='movie' AND status='complete'")
    assert leaves[0]["n"] == 20  # children's reported totals sum to the parent's


def test_single_day_overflow_is_flagged_truncated_not_fatal(tmp_path):
    fake = FakeTMDB(page_size=2, max_page=3)
    fake.add_titles("movie", list(range(1, 21)), date="2023-03-03")
    rig = Rig(tmp_path, fake, max_pages=3)
    rig.drain()
    assert rig.windows.stats()["truncated"] == 1
    row = rig.db.query("SELECT ids_seen, total_results FROM enum_windows "
                       "WHERE kind='date' AND title_type='movie' AND truncated=1")[0]
    assert row["ids_seen"] == 6 and row["total_results"] == 20  # gap is auditable


def test_page_commit_is_atomic(tmp_path, monkeypatch):
    fake = FakeTMDB(page_size=5)
    spread(fake, "movie", 10)
    rig = Rig(tmp_path, fake)

    def boom(*a, **k):
        raise RuntimeError("disk full")

    monkeypatch.setattr(rig.windows, "record_page", boom)
    with pytest.raises(RuntimeError):
        rig.run(1)
    assert rig.ids("movie") == set()  # ids rolled back with the cursor
    assert rig.windows.next_open().next_page == 1


def test_transient_error_stops_enumeration_without_advancing(tmp_path):
    from tmdb_extractor.errors import TMDBRequestError

    class Flaky(FakeTMDB):
        def get_json(self, path, params=None):
            raise TMDBRequestError("HTTP 500", attempts=3, status_code=500)

    rig = Rig(tmp_path, Flaky())
    result = rig.run(5)
    assert result.pages == 0 and result.errors and not result.complete
    assert rig.windows.next_open().next_page == 1


def test_persistent_client_error_parks_window_and_moves_on(tmp_path):
    from tmdb_extractor.errors import TMDBRequestError

    class BadMovie(FakeTMDB):
        def get_json(self, path, params=None):
            if path == "discover/movie":
                raise TMDBRequestError("HTTP 422", attempts=1, status_code=422)
            return super().get_json(path, params)

    fake = BadMovie(page_size=5)
    spread(fake, "tv", 3)
    rig = Rig(tmp_path, fake)
    for _ in range(10):
        if rig.run(5).complete:
            break
    assert rig.windows.stats()["failed"] >= 1
    assert rig.ids("tv") == {t["id"] for t in fake.universe["tv"]}
