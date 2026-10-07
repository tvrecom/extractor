"""One cron invocation: bootstrap, enumerate, extract, summarise (always)."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from math import ceil
from typing import TypeVar

from .budget import RunBudget
from .enumeration import BaselineEstimator, Enumerator, WindowSeeder
from .extraction import BatchRunner
from .genres import GenreCatalog
from .http_client import ApiClient, redact
from .migration import LegacyStateMigrator
from .reporting import ProgressReporter, RunSummary, SummaryWriter
from .state import TitleQueue
from .utils import utc_now

LOGGER = logging.getLogger(__name__)
T = TypeVar("T")


class Pipeline:
    def __init__(
        self, *, client: ApiClient, seeder: WindowSeeder, migrator: LegacyStateMigrator,
        baseline: BaselineEstimator, genres: GenreCatalog, type_keys: list[str],
        language: str, enumerator: Enumerator, queue: TitleQueue, runner: BatchRunner,
        reporter: ProgressReporter, writer: SummaryWriter,
        budget_factory: Callable[[], RunBudget],
        max_title_retries: int, retry_fraction: float,
    ) -> None:
        self._client = client
        self._seeder = seeder
        self._migrator = migrator
        self._baseline = baseline
        self._genres = genres
        self._type_keys = type_keys
        self._language = language
        self._enumerator = enumerator
        self._queue = queue
        self._runner = runner
        self._reporter = reporter
        self._writer = writer
        self._budget_factory = budget_factory
        self._max_retries = max_title_retries
        self._retry_fraction = retry_fraction

    def run(self) -> RunSummary:
        started = time.monotonic()
        summary = RunSummary(started_at=utc_now())
        try:
            budget = self._budget_factory()
            self._guard(summary, "bootstrap", self._bootstrap)
            self._guard(summary, "baseline", self._baseline.ensure)
            self._guard(summary, "genres", lambda: self._genres.load(
                self._client, self._type_keys, self._language))
            summary.enumeration = self._guard(
                summary, "enumeration", lambda: self._enumerator.run(budget))
            items = self._guard(summary, "claim", lambda: self._claim(budget)) or []
            batch = self._guard(summary, "extraction", lambda: self._runner.run(items, budget))
            if batch is not None:
                summary.extraction = batch
        except BaseException as exc:  # e.g. KeyboardInterrupt: still leave an audit record
            summary.errors.append(f"aborted: {type(exc).__name__}")
            raise
        finally:
            self._finish(summary, time.monotonic() - started)
        return summary

    def _bootstrap(self) -> None:
        self._seeder.seed()
        self._migrator.run()

    def _claim(self, budget: RunBudget):
        quota = ceil(budget.max_details * self._retry_fraction)
        return self._queue.claim(budget.max_details, self._max_retries, quota)

    def _guard(self, summary: RunSummary, name: str, step: Callable[[], T]) -> T | None:
        """Run one phase; a failure is recorded and later phases still run."""
        try:
            return step()
        except Exception as exc:
            message = f"{name}: {redact(repr(exc))}"
            LOGGER.error("Phase failed: %s", message, exc_info=exc)
            summary.errors.append(message)
            return None

    def _finish(self, summary: RunSummary, duration: float) -> None:
        summary.duration_seconds = duration
        summary.finished_at = utc_now()
        try:
            summary.snapshot = self._reporter.snapshot()
        except Exception as exc:
            summary.errors.append(f"snapshot: {redact(repr(exc))}")
        if summary.errors:
            summary.status = "error"
        elif summary.extraction.failed or (
            summary.enumeration and summary.enumeration.errors
        ):
            summary.status = "partial"
        self._writer.write(summary)
