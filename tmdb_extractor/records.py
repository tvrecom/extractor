"""Builders for raw extraction records (canonical-schema-shaped, pre-normalization)."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from .parsers import cast_entries, director_names, extract_imdb_id, positive_int
from .utils import sha256_json, utc_now

if TYPE_CHECKING:  # pragma: no cover
    from .profiles import TitleProfile

SOURCE_SYSTEM = "tmdb"


def content_hash(synopsis: str | None, cast: list[dict[str, Any]], genres: list[str]) -> str:
    """Hash only retrieval-relevant fields (synopsis, cast, genres)."""
    return sha256_json({"synopsis": synopsis or "", "cast": cast, "genres": genres})


def _base_record(**fields: Any) -> dict[str, Any]:
    return {
        "license_expiry": None,
        "transcript_ref": None,
        "status": "active",
        "source_system": SOURCE_SYSTEM,
        **fields,
    }


class TitleRecordBuilder:
    """Builds movie/series records; type differences come from the TitleProfile."""

    def __init__(self, region: str, now: Callable[[], str] = utc_now) -> None:
        self._region = region
        self._now = now

    def build(
        self, profile: "TitleProfile", tmdb_id: int, payload: dict[str, Any],
        genres: list[str], runtime: int | None, raw_ref: str,
    ) -> dict[str, Any]:
        credits = payload.get("credits") or {}
        cast = cast_entries(credits.get("cast"))
        synopsis = payload.get("overview")
        return _base_record(
            id=profile.record_id(tmdb_id),
            tmdb_id=tmdb_id,
            imdb_id=extract_imdb_id(payload),
            type=profile.canonical_type,
            parent_id=None,
            title=payload.get(profile.title_field),
            synopsis=synopsis,
            genres=genres,
            cast=cast,
            director=director_names(credits.get("crew")),
            release_date=payload.get(profile.date_field) or None,
            season=None,
            episode=None,
            runtime_minutes=runtime,
            maturity_rating=profile.maturity_rating(payload, self._region),
            language=payload.get("original_language"),
            content_hash=content_hash(synopsis, cast, genres),
            last_updated=self._now(),
            _source={"raw_response": raw_ref},
        )


class EpisodeRecordBuilder:
    def __init__(self, now: Callable[[], str] = utc_now) -> None:
        self._now = now

    def build(
        self, parent_id: str, language: str | None, genres: list[str],
        season_number: int, episode: dict[str, Any], raw_ref: str,
    ) -> dict[str, Any]:
        season = episode.get("season_number")
        season = season if isinstance(season, int) else season_number
        number = episode.get("episode_number")
        cast = cast_entries(episode.get("guest_stars"))
        synopsis = episode.get("overview") or ""
        return _base_record(
            id=f"{parent_id}_s{season}_e{number}",
            tmdb_id=episode.get("id"),
            imdb_id=None,
            type="episode",
            parent_id=parent_id,
            title=episode.get("name"),
            synopsis=synopsis,
            genres=genres,
            cast=cast,
            director=director_names(episode.get("crew")),
            release_date=episode.get("air_date") or None,
            season=season,
            episode=number,
            runtime_minutes=positive_int(episode.get("runtime")),
            maturity_rating=None,
            language=language,
            content_hash=content_hash(synopsis, cast, genres),
            last_updated=self._now(),
            _source={"raw_response": raw_ref},
        )
