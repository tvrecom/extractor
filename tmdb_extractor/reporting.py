"""Run summaries and cumulative progress snapshots."""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from .enumeration import BaselineEstimator, EnumerationResult
from .extraction import BatchSummary
from .state import TitleQueue, WindowRepository
from .storage import JsonlFile

LOGGER = logging.getLogger(__name__)


class ProgressReporter:
    """Cumulative view across runs, derived from durable state (never reset per run)."""

    def __init__(
        self, titles: TitleQueue, windows: WindowRepository,
        baseline: BaselineEstimator, type_keys: list[str], max_retries: int,
    ) -> None:
        self._titles = titles
        self._windows = windows
        self._baseline = baseline
        self._type_keys = type_keys
        self._max_retries = max_retries

    def snapshot(self) -> dict[str, Any]:
        counts = self._titles.counts()
        exhausted = self._titles.count_exhausted(self._max_retries)
        done_by_type = self._titles.done_by_type()
        progress = {}
        for key in self._type_keys:
            done, baseline = done_by_type.get(key, 0), self._baseline.get(key)
            progress[key] = {
                "done": done,
                "estimated_total": baseline,
                "percent": round(100 * done / baseline, 3) if baseline else None,
            }
        current = self._windows.next_open()
        return {
            "backlog": {
                "pending": counts["pending"], "done": counts["done"],
                "failed_retryable": counts["failed"] - exhausted,
                "failed_exhausted": exhausted, "gone": counts["gone"],
            },
            "progress": progress,
            "checkpoint": {
                "windows": self._windows.stats(),
                "current": current.describe() if current else None,
            },
        }


@dataclass
class RunSummary:
    started_at: str
    finished_at: str = ""
    duration_seconds: float = 0.0
    status: str = "ok"            # ok | partial | error
    errors: list[str] = field(default_factory=list)
    enumeration: EnumerationResult | None = None
    extraction: BatchSummary = field(default_factory=BatchSummary)
    snapshot: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        e = self.enumeration
        return {
            "run_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_seconds": round(self.duration_seconds, 2),
            "status": self.status,
            "errors": self.errors,
            "enumeration": {
                "pages": e.pages, "new_ids": e.new_ids, "windows_split": e.splits,
                "windows_completed": e.windows_completed, "frontier_exhausted": e.complete,
                "errors": e.errors,
            } if e else None,
            "extraction": self.extraction.to_dict(),
            **self.snapshot,
        }


class SummaryWriter:
    def __init__(self, target: JsonlFile) -> None:
        self._target = target

    def write(self, summary: RunSummary) -> None:
        data = summary.to_dict()
        ext = summary.extraction
        if ext.attempted != ext.succeeded + ext.failed:  # audit invariant
            LOGGER.error("Summary invariant violated: %s", ext)
        self._target.append(data)
        LOGGER.info("Run summary: %s", json.dumps(data, ensure_ascii=False))
