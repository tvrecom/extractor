"""Composition root and entry point. The only module that builds concrete objects."""

from __future__ import annotations

import logging
import sys
from collections.abc import Mapping
from typing import Any

from .budget import RunBudget
from .config import Settings
from .enumeration import BaselineEstimator, DiscoverQueryBuilder, Enumerator, WindowSeeder
from .errors import ConfigError
from .extraction import BatchRunner, TitleExtractor
from .genres import GenreCatalog
from .http_client import ApiClient, RetryPolicy, TMDBClient
from .logging_setup import configure_logging
from .migration import LegacyStateMigrator
from .pipeline import Pipeline
from .profiles import default_profiles
from .records import TitleRecordBuilder
from .reporting import ProgressReporter, SummaryWriter
from .state import Database, MetaRepository, TitleQueue, WindowRepository
from .storage import JsonlFile, JsonlSink, RawStore
from .validation import CanonicalValidator

LOGGER = logging.getLogger("tmdb-extractor")

try:  # POSIX only; on other platforms overlapping runs are not prevented
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]


class RunLock:
    """Non-blocking exclusive lock so overlapping cron invocations exit cleanly."""

    def __init__(self, path) -> None:
        self._path = path
        self._handle = None

    def acquire(self) -> bool:
        if fcntl is None:  # pragma: no cover
            return True
        handle = open(self._path, "a+")
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            return False
        self._handle = handle
        return True

    def release(self) -> None:
        if self._handle is not None:
            self._handle.close()  # closing drops the flock
            self._handle = None


def build_pipeline(
    settings: Settings, client: ApiClient | None = None
) -> tuple[Pipeline, Database]:
    settings.ensure_directories()
    db = Database(settings.state_db)
    meta, titles, windows = MetaRepository(db), TitleQueue(db), WindowRepository(db)

    profiles = default_profiles()
    client = client or TMDBClient(
        settings.api_key, settings.base_url, settings.request_timeout,
        RetryPolicy(settings.max_attempts, settings.backoff_base_seconds),
    )
    genres = GenreCatalog()
    raw_store = RawStore(settings.raw_dir, settings.data_dir)
    sink = JsonlSink.for_directory(settings.output_dir)
    extractor = TitleExtractor(
        client, raw_store, sink, profiles, genres,
        TitleRecordBuilder(settings.region), CanonicalValidator(), settings.language,
    )
    baseline = BaselineEstimator(client, meta, profiles, settings.language)
    keys = list(profiles)

    pipeline = Pipeline(
        client=client,
        seeder=WindowSeeder(windows, profiles, settings.start_year, settings.end_year),
        migrator=LegacyStateMigrator(db, meta, titles, windows, settings.raw_dir),
        baseline=baseline, genres=genres, type_keys=keys, language=settings.language,
        enumerator=Enumerator(
            client, windows, titles, db, profiles,
            DiscoverQueryBuilder(settings.language), settings.popularity_pages,
        ),
        queue=titles,
        runner=BatchRunner(extractor, titles, sink, settings.concurrency),
        reporter=ProgressReporter(titles, windows, baseline, keys, settings.max_title_retries),
        writer=SummaryWriter(JsonlFile(settings.run_summary_path)),
        budget_factory=lambda: RunBudget(
            settings.max_enum_pages, settings.max_details, settings.max_run_seconds
        ),
        max_title_retries=settings.max_title_retries,
        retry_fraction=settings.retry_fraction,
    )
    return pipeline, db


def main(env: Mapping[str, str] | None = None, client: Any = None) -> int:
    try:  # fail fast, before any file or network I/O
        settings = Settings.from_env(env)
    except ConfigError as exc:
        print(exc, file=sys.stderr)
        return 2

    configure_logging(settings.log_level, settings.api_key)
    settings.ensure_directories()
    lock = RunLock(settings.lock_path)
    if not lock.acquire():
        LOGGER.warning("Another extractor run holds %s; exiting", settings.lock_path)
        return 0
    try:
        LOGGER.info("Starting TMDB extraction language=%s region=%s data_dir=%s",
                    settings.language, settings.region, settings.data_dir)
        pipeline, db = build_pipeline(settings, client)
        try:
            summary = pipeline.run()
        finally:
            db.close()
        return 1 if summary.status == "error" else 0
    finally:
        lock.release()
