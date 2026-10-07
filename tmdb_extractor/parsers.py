"""Pure functions that pull canonical values out of TMDB payloads."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any


def positive_int(value: Any) -> int | None:
    """TMDB uses 0 for 'unknown' runtimes; normalise to None."""
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return None


def mean_positive(values: Iterable[Any]) -> int | None:
    numbers = [v for v in values if positive_int(v) is not None]
    return round(sum(numbers) / len(numbers)) if numbers else None


def cast_entries(people: Iterable[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """{"name", "role"} entries; shared by title credits and episode guest stars."""
    return [
        {"name": p["name"], "role": p.get("character")}
        for p in people or []
        if isinstance(p, dict) and p.get("name")
    ]


def director_names(crew: Iterable[dict[str, Any]] | None) -> list[str]:
    return [
        p["name"]
        for p in crew or []
        if isinstance(p, dict) and p.get("job") == "Director" and p.get("name")
    ]


def extract_imdb_id(payload: dict[str, Any]) -> str | None:
    imdb_id = (payload.get("external_ids") or {}).get("imdb_id")
    return imdb_id if isinstance(imdb_id, str) and imdb_id else None


def movie_certification(payload: dict[str, Any], region: str) -> str | None:
    """Region certification, preferring theatrical (type 3) releases."""
    for country in (payload.get("release_dates") or {}).get("results", []):
        if country.get("iso_3166_1") != region:
            continue
        releases = [r for r in country.get("release_dates", []) if r.get("certification")]
        theatrical = [r for r in releases if r.get("type") == 3]
        chosen = theatrical or releases
        if chosen:
            return chosen[0]["certification"]
    return None


def tv_content_rating(payload: dict[str, Any], region: str) -> str | None:
    for rating in (payload.get("content_ratings") or {}).get("results", []):
        if rating.get("iso_3166_1") == region and rating.get("rating"):
            return rating["rating"]
    return None
