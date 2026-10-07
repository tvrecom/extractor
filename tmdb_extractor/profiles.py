"""
Per-type behaviour (Open/Closed): adding a title type means adding a profile.
The enumerator and extractor never branch on the type.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, ClassVar

from .parsers import (
    mean_positive, movie_certification, positive_int, tv_content_rating,
)
from .records import EpisodeRecordBuilder
from .storage import Streams
from .utils import utc_now


@dataclass(frozen=True)
class ChildRef:
    """A dependent resource of a title (e.g. a TV season)."""

    endpoint: str
    raw_name: str
    number: int


@dataclass(frozen=True)
class ChildPayload:
    ref: ChildRef
    payload: dict[str, Any]
    raw_ref: str


class TitleProfile(ABC):
    type_key: ClassVar[str]            # TMDB path segment and internal type key
    canonical_type: ClassVar[str]      # canonical schema `type`
    output_stream: ClassVar[str]
    append_to_response: ClassVar[str]
    title_field: ClassVar[str]
    date_field: ClassVar[str]
    date_filter: ClassVar[str]         # discover filter/sort prefix
    discover_defaults: ClassVar[Mapping[str, str]] = {}

    def detail_endpoint(self, tmdb_id: int) -> str:
        return f"{self.type_key}/{tmdb_id}"

    def record_id(self, tmdb_id: int) -> str:
        # Namespaced by type: TMDB movie and TV id spaces overlap.
        return f"title_{self.type_key}_{tmdb_id}"

    @abstractmethod
    def maturity_rating(self, payload: dict[str, Any], region: str) -> str | None: ...

    @abstractmethod
    def runtime_minutes(
        self, payload: dict[str, Any], children: Sequence[ChildPayload]
    ) -> int | None: ...

    def child_refs(self, payload: dict[str, Any]) -> list[ChildRef]:
        return []

    def build_child_records(
        self, parent_id: str, payload: dict[str, Any], genres: list[str],
        children: Sequence[ChildPayload],
    ) -> list[tuple[str, dict[str, Any]]]:
        return []


class MovieProfile(TitleProfile):
    type_key = "movie"
    canonical_type = "movie"
    output_stream = Streams.MOVIES
    append_to_response = "credits,external_ids,release_dates"
    title_field = "title"
    date_field = "release_date"
    date_filter = "primary_release_date"
    discover_defaults = {"include_video": "false"}

    def maturity_rating(self, payload, region):
        return movie_certification(payload, region)

    def runtime_minutes(self, payload, children):
        return positive_int(payload.get("runtime"))


class TvProfile(TitleProfile):
    type_key = "tv"
    canonical_type = "series"
    output_stream = Streams.SERIES
    append_to_response = "credits,external_ids,content_ratings"
    title_field = "name"
    date_field = "first_air_date"
    date_filter = "first_air_date"
    discover_defaults = {"include_null_first_air_dates": "false"}

    def __init__(self, now: Callable[[], str] = utc_now) -> None:
        self._episodes = EpisodeRecordBuilder(now)

    def maturity_rating(self, payload, region):
        return tv_content_rating(payload, region)

    def runtime_minutes(self, payload, children):
        """episode_run_time average, else fetched episode runtimes, else last aired."""
        declared = mean_positive(payload.get("episode_run_time") or [])
        if declared is not None:
            return declared
        fetched = mean_positive(
            e.get("runtime")
            for child in children
            for e in child.payload.get("episodes") or []
        )
        if fetched is not None:
            return fetched
        return positive_int((payload.get("last_episode_to_air") or {}).get("runtime"))

    def child_refs(self, payload):
        tmdb_id = payload.get("id")
        refs = []
        for season in payload.get("seasons") or []:
            number = season.get("season_number")
            if isinstance(number, int):  # season 0 (specials) is retained
                refs.append(
                    ChildRef(f"tv/{tmdb_id}/season/{number}", f"season_{number}", number)
                )
        return refs

    def build_child_records(self, parent_id, payload, genres, children):
        language = payload.get("original_language")
        return [
            (
                Streams.EPISODES,
                self._episodes.build(
                    parent_id, language, genres, child.ref.number, episode, child.raw_ref
                ),
            )
            for child in children
            for episode in child.payload.get("episodes") or []
        ]


def default_profiles(now: Callable[[], str] = utc_now) -> dict[str, TitleProfile]:
    return {"movie": MovieProfile(), "tv": TvProfile(now)}
