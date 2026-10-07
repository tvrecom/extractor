"""
Resumable catalog enumeration over persisted windows.

Each page commit is one transaction: new IDs and the window cursor advance
together, so a crash can re-fetch at most one page and never loses or skips IDs.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any

from .budget import RunBudget
from .config import TMDB_MAX_PAGE
from .errors import TMDBRequestError
from .http_client import ApiClient
from .models import COMPLETE, DATE, PENDING, POPULARITY, Window, WindowSpec
from .profiles import TitleProfile
from .state import MetaRepository, TitleQueue, UnitOfWork, WindowRepository

LOGGER = logging.getLogger(__name__)


class DiscoverQueryBuilder:
    """Single place that knows how to express a window as /discover parameters."""

    def __init__(self, language: str) -> None:
        self._language = language

    def build(self, profile: TitleProfile, window: Window, page: int) -> dict[str, Any]:
        params: dict[str, Any] = {
            "language": self._language,
            "page": page,
            "include_adult": "false",
            **profile.discover_defaults,
        }
        if window.kind == DATE:
            # Ascending date order is stable between cron runs; popularity is not.
            params["sort_by"] = f"{profile.date_filter}.asc"
            params[f"{profile.date_filter}.gte"] = window.date_gte
            params[f"{profile.date_filter}.lte"] = window.date_lte
        else:
            params["sort_by"] = "popularity.desc"
        return params


class WindowSeeder:
    """Idempotently ensures the popularity and release-year windows exist."""

    def __init__(
        self, windows: WindowRepository, profiles: Mapping[str, TitleProfile],
        start_year: int, end_year: int,
    ) -> None:
        self._windows = windows
        self._profiles = profiles
        self._start = start_year
        self._end = end_year

    def specs(self) -> Iterator[WindowSpec]:
        for key in self._profiles:
            yield WindowSpec(key, POPULARITY, priority=0)
        for year in range(self._end, self._start - 1, -1):  # newest first
            for key in self._profiles:
                yield WindowSpec(key, DATE, f"{year:04d}-01-01", f"{year:04d}-12-31", 1)

    def seed(self) -> int:
        return self._windows.add_missing(self.specs())


@dataclass
class EnumerationResult:
    pages: int = 0
    new_ids: int = 0
    splits: int = 0
    windows_completed: int = 0
    complete: bool = False
    errors: list[str] = field(default_factory=list)


class Enumerator:
    def __init__(
        self, client: ApiClient, windows: WindowRepository, titles: TitleQueue,
        uow: UnitOfWork, profiles: Mapping[str, TitleProfile],
        query_builder: DiscoverQueryBuilder, popularity_pages: int,
        max_pages_per_query: int = TMDB_MAX_PAGE, park_after: int = 3,
    ) -> None:
        self._client = client
        self._windows = windows
        self._titles = titles
        self._uow = uow
        self._profiles = profiles
        self._query = query_builder
        self._popularity_pages = min(popularity_pages, max_pages_per_query)
        self._max_pages = max_pages_per_query
        self._park_after = park_after

    def run(self, budget: RunBudget) -> EnumerationResult:
        result = EnumerationResult()
        while result.pages < budget.max_enum_pages and not budget.expired():
            window = self._windows.next_open()
            if window is None:
                result.complete = True
                LOGGER.info("Enumeration frontier is exhausted")
                break
            try:
                self._step(window, result)
            except TMDBRequestError as exc:
                message = f"{window.describe()}: {exc}"
                result.errors.append(message)
                transient = exc.status_code is None or exc.status_code == 429 or exc.status_code >= 500
                if transient:
                    break  # try again next run; don't hammer a struggling API
                if not self._windows.record_failure(window.id, str(exc), self._park_after):
                    break
        LOGGER.info(
            "Enumeration: pages=%s new_ids=%s splits=%s", result.pages, result.new_ids, result.splits
        )
        return result

    def _page_cap(self, window: Window) -> int:
        return self._popularity_pages if window.kind == POPULARITY else self._max_pages

    def _step(self, window: Window, result: EnumerationResult) -> None:
        cap = self._page_cap(window)
        if window.next_page > cap:  # e.g. cap lowered after a checkpoint was written
            self._windows.mark_complete(window.id)
            result.windows_completed += 1
            return

        profile = self._profiles[window.title_type]
        payload = self._client.get_json(
            f"discover/{profile.type_key}",
            self._query.build(profile, window, window.next_page),
        )
        result.pages += 1
        total_results = int(payload.get("total_results") or 0)
        total_pages = int(payload.get("total_pages") or 0)

        oversized = window.kind == DATE and total_pages > self._max_pages
        if oversized and window.next_page == 1 and window.span_days >= 1:
            self._windows.split(window.id, window.bisect(), total_results, total_pages)
            result.splits += 1
            LOGGER.info("Bisected %s (%s pages)", window.describe(), total_pages)
            return

        ids = [
            item["id"] for item in payload.get("results") or []
            if isinstance(item, dict) and isinstance(item.get("id"), int)
        ]
        finished = window.next_page >= min(total_pages, cap)
        with self._uow.transaction():
            result.new_ids += self._titles.add_pending(ids, window.title_type)
            self._windows.record_page(
                window.id, window.next_page + 1, COMPLETE if finished else PENDING,
                total_results, total_pages, len(ids), oversized,
            )
        if oversized and window.next_page == 1:
            LOGGER.warning("%s exceeds %s pages at one-day granularity: truncated",
                           window.describe(), self._max_pages)
        result.windows_completed += finished


class BaselineEstimator:
    """Records TMDB's unfiltered discover totals once, as the progress denominator."""

    def __init__(
        self, client: ApiClient, meta: MetaRepository,
        profiles: Mapping[str, TitleProfile], language: str,
    ) -> None:
        self._client = client
        self._meta = meta
        self._profiles = profiles
        self._language = language

    def ensure(self) -> None:
        for key, profile in self._profiles.items():
            if self._meta.get(f"baseline:{key}") is not None:
                continue
            payload = self._client.get_json(
                f"discover/{profile.type_key}",
                {"language": self._language, "page": 1, "include_adult": "false"},
            )
            self._meta.set(f"baseline:{key}", str(int(payload.get("total_results") or 0)))

    def get(self, key: str) -> int | None:
        value = self._meta.get(f"baseline:{key}")
        return int(value) if value is not None else None
