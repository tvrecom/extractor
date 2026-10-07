"""Test doubles and payload builders."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

from tmdb_extractor.cli import build_pipeline
from tmdb_extractor.config import Settings
from tmdb_extractor.errors import TMDBRequestError

API_KEY = "SECRET-KEY-123"


def make_settings(tmp_path: Path, **overrides: Any) -> Settings:
    base = dict(
        api_key=API_KEY, data_dir=tmp_path / "data", start_year=2023, end_year=2024,
        concurrency=2, max_enum_pages=50, max_details=100, popularity_pages=2,
        max_attempts=2, backoff_base_seconds=0.0,
    )
    base.update(overrides)
    return Settings(**base)


def movie_payload(tmdb_id: int, **extra: Any) -> dict[str, Any]:
    payload = {
        "id": tmdb_id, "title": f"Movie {tmdb_id}", "overview": "A film.",
        "genres": [{"id": 28, "name": "Action"}], "release_date": "2023-05-01",
        "runtime": 101, "original_language": "en",
        "credits": {
            "cast": [{"name": "Ann Actor", "character": "Hero"}],
            "crew": [{"name": "Dee Rector", "job": "Director"},
                     {"name": "Cam Era", "job": "Cinematographer"}],
        },
        "external_ids": {"imdb_id": f"tt{tmdb_id:07d}"},
        "release_dates": {"results": [
            {"iso_3166_1": "DE", "release_dates": [{"certification": "12", "type": 3}]},
            {"iso_3166_1": "US", "release_dates": [
                {"certification": "", "type": 1},
                {"certification": "PG-13", "type": 3}]},
        ]},
    }
    payload.update(extra)
    return payload


def tv_payload(tmdb_id: int, seasons=(0, 1, 2), **extra: Any) -> dict[str, Any]:
    payload = {
        "id": tmdb_id, "name": f"Show {tmdb_id}", "overview": "A series.",
        "genres": [{"id": 18, "name": "Drama"}], "first_air_date": "2020-01-01",
        "episode_run_time": [40, 50], "original_language": "en",
        "seasons": [{"season_number": n} for n in seasons],
        "credits": {"cast": [{"name": "Tv Star", "character": "Lead"}], "crew": []},
        "external_ids": {"imdb_id": "tt9999999"},
        "content_ratings": {"results": [{"iso_3166_1": "US", "rating": "TV-MA"}]},
    }
    payload.update(extra)
    return payload


def season_payload(season: int, episodes: int = 2) -> dict[str, Any]:
    return {"season_number": season, "episodes": [
        {"id": season * 100 + n, "name": f"Ep {n}", "overview": "Plot.",
         "season_number": season, "episode_number": n, "air_date": "2020-02-0%d" % n,
         "runtime": 45, "guest_stars": [{"name": "Guest", "character": "Cameo"}],
         "crew": [{"name": "Ep Director", "job": "Director"}]}
        for n in range(1, episodes + 1)]}


class FakeTMDB:
    """Implements ApiClient. Discover is a real paginated filter over a universe."""

    def __init__(self, page_size: int = 20, max_page: int = 500) -> None:
        self.page_size = page_size
        self.max_page = max_page
        self.universe: dict[str, list[dict[str, Any]]] = {"movie": [], "tv": []}
        self.routes: dict[str, Any] = {}
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._lock = threading.Lock()
        self.genres = {"movie": [{"id": 28, "name": "Action"}], "tv": [{"id": 18, "name": "Drama"}]}

    def add_titles(self, kind: str, ids: list[int], date: str = "2023-06-01") -> None:
        for i in ids:
            self.universe[kind].append({"id": i, "date": date, "pop": i})

    def get_json(self, path, params=None):
        params = dict(params or {})
        with self._lock:
            self.calls.append((path, params))
        if path.startswith("discover/"):
            return self._discover(path.split("/")[1], params)
        if path.startswith("genre/"):
            return {"genres": self.genres[path.split("/")[1]]}
        route = self.routes.get(path)
        if route is None:
            raise TMDBRequestError(f"HTTP 404: {path}", attempts=1, status_code=404)
        if isinstance(route, Exception):
            raise route
        return route(params) if callable(route) else route

    def _discover(self, kind: str, params: dict[str, Any]) -> dict[str, Any]:
        date_key = "primary_release_date" if kind == "movie" else "first_air_date"
        items = list(self.universe[kind])
        if f"{date_key}.gte" in params:
            lo, hi = params[f"{date_key}.gte"], params[f"{date_key}.lte"]
            items = [t for t in items if lo <= t["date"] <= hi]
        if params.get("sort_by") == "popularity.desc":
            items.sort(key=lambda t: -t["pop"])
        else:
            items.sort(key=lambda t: (t["date"], t["id"]))
        page = params["page"]
        if page > self.max_page:
            raise TMDBRequestError("HTTP 422: page too high", attempts=1, status_code=422)
        total_pages = -(-len(items) // self.page_size)
        chunk = items[(page - 1) * self.page_size: page * self.page_size]
        return {"page": page, "results": [{"id": t["id"]} for t in chunk],
                "total_results": len(items), "total_pages": total_pages}

    def discover_calls(self) -> list[dict[str, Any]]:
        return [p for path, p in self.calls if path.startswith("discover/")]


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def run_once(settings: Settings, client: FakeTMDB):
    pipeline, db = build_pipeline(settings, client)
    try:
        return pipeline.run()
    finally:
        db.close()


def add_movie(fake: FakeTMDB, tmdb_id: int, **extra: Any) -> None:
    fake.add_titles("movie", [tmdb_id])
    fake.routes[f"movie/{tmdb_id}"] = movie_payload(tmdb_id, **extra)


def add_series(fake: FakeTMDB, tmdb_id: int, seasons=(0, 1), **extra: Any) -> None:
    fake.add_titles("tv", [tmdb_id])
    fake.routes[f"tv/{tmdb_id}"] = tv_payload(tmdb_id, seasons, **extra)
    for n in seasons:
        fake.routes[f"tv/{tmdb_id}/season/{n}"] = season_payload(n)
