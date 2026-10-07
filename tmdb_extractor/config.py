"""Runtime settings. Loading performs validation only: no I/O, no network."""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import find_dotenv, load_dotenv

# usecwd=True: look for .env from the directory the app is run in (walking up),
load_dotenv(find_dotenv(usecwd=True))

from .errors import ConfigError

BASE_URL = "https://api.themoviedb.org/3"
TMDB_MAX_PAGE = 500  # TMDB rejects page > 500 for any query


class _EnvReader:
    """Typed env access that accumulates every problem instead of failing on the first."""

    def __init__(self, env: Mapping[str, str]) -> None:
        self._env = env
        self.problems: list[str] = []

    def text(self, name: str, default: str | None) -> str | None:
        value = self._env.get(name)
        return value.strip() if value and value.strip() else default

    def _number(
        self,
        name: str,
        default: Any,
        cast: Callable[[str], Any],
        kind: str,
        minimum: float | None,
        maximum: float | None,
    ) -> Any:
        raw = self.text(name, None)
        if raw is None:
            return default
        try:
            value = cast(raw)
        except ValueError:
            self.problems.append(f"{name} must be {kind}, got {raw!r}")
            return default
        if (minimum is not None and value < minimum) or (
            maximum is not None and value > maximum
        ):
            self.problems.append(f"{name}={raw} is out of range [{minimum}, {maximum}]")
            return default
        return value

    def integer(self, name, default, minimum=None, maximum=None) -> int:
        return self._number(name, default, int, "an integer", minimum, maximum)

    def number(self, name, default, minimum=None, maximum=None) -> float:
        return self._number(name, default, float, "a number", minimum, maximum)


@dataclass(frozen=True)
class Settings:
    api_key: str = field(repr=False)
    language: str = "en-US"
    region: str = "US"
    start_year: int = 1900
    end_year: int = 2100
    concurrency: int = 6
    max_enum_pages: int = 10
    max_details: int = 100
    request_timeout: float = 30.0
    max_attempts: int = 4
    backoff_base_seconds: float = 1.0
    popularity_pages: int = TMDB_MAX_PAGE
    data_dir: Path = Path("tmdb_extraction")
    max_run_seconds: float = 0.0  # 0 disables the wall-clock deadline
    max_title_retries: int = 3
    retry_fraction: float = 0.2
    log_level: str = "INFO"
    base_url: str = BASE_URL

    # -- derived paths -----------------------------------------------------
    @property
    def raw_dir(self) -> Path:
        return self.data_dir / "raw"

    @property
    def output_dir(self) -> Path:
        return self.data_dir / "extracted"

    @property
    def state_db(self) -> Path:
        return self.data_dir / "state.sqlite3"

    @property
    def run_summary_path(self) -> Path:
        return self.data_dir / "run_summaries.jsonl"

    @property
    def lock_path(self) -> Path:
        return self.data_dir / "run.lock"

    def ensure_directories(self) -> None:
        for directory in (self.data_dir, self.raw_dir, self.output_dir):
            directory.mkdir(parents=True, exist_ok=True)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Settings":
        reader = _EnvReader(os.environ if env is None else env)

        api_key = reader.text("TMDB_API_KEY", None)
        if not api_key:
            reader.problems.append(
                "TMDB_API_KEY environment variable is required. "
                "Set it before starting the extractor."
            )

        current_year = datetime.now(timezone.utc).year
        # Default end year is next year so announced/future-dated titles are covered.
        start_year = reader.integer("TMDB_START_YEAR", 1900, 1, 9999)
        end_year = reader.integer("TMDB_END_YEAR", current_year + 1, 1, 9999)
        if start_year > end_year:
            reader.problems.append("TMDB_START_YEAR must be <= TMDB_END_YEAR")

        settings = cls(
            api_key=api_key or "",
            language=reader.text("TMDB_LANGUAGE", "en-US"),
            region=reader.text("TMDB_REGION", "US"),
            start_year=start_year,
            end_year=end_year,
            concurrency=reader.integer("TMDB_CONCURRENCY", 6, 1),
            max_enum_pages=reader.integer("TMDB_MAX_ENUM_PAGES_PER_RUN", 10, 0),
            max_details=reader.integer("TMDB_MAX_DETAILS_PER_RUN", 100, 0),
            request_timeout=reader.number("TMDB_REQUEST_TIMEOUT", 30.0, 0.1),
            max_attempts=reader.integer("TMDB_MAX_ATTEMPTS", 4, 1),
            backoff_base_seconds=reader.number("TMDB_BACKOFF_BASE_SECONDS", 1.0, 0.0),
            popularity_pages=min(
                reader.integer("TMDB_POPULARITY_PAGES", TMDB_MAX_PAGE, 1),
                TMDB_MAX_PAGE,
            ),
            data_dir=Path(reader.text("TMDB_DATA_DIR", "tmdb_extraction")),
            max_run_seconds=reader.number("TMDB_MAX_RUN_SECONDS", 0.0, 0.0),
            max_title_retries=reader.integer("TMDB_MAX_TITLE_RETRIES", 3, 1),
            retry_fraction=reader.number("TMDB_RETRY_FRACTION", 0.2, 0.0, 1.0),
            log_level=(reader.text("LOG_LEVEL", "INFO") or "INFO").upper(),
        )
        if reader.problems:
            raise ConfigError(reader.problems)
        return settings
