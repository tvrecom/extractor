"""
One-time upgrade of a v1 state database (seen_titles + enumeration_checkpoint).

- IDs whose raw data is complete become `done`; everything else is re-queued, which
  recovers IDs the v1 run marked seen but never extracted.
- Completed enumeration windows are preserved. An in-progress release-year window
  restarts at page 1 because the sort order changed (dedupe makes this safe).
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

from .models import COMPLETE, DONE, FAILED, PENDING, POPULARITY
from .state import Database, MetaRepository, TitleQueue, WindowRepository

LOGGER = logging.getLogger(__name__)
FLAG = "legacy_migrated"


@dataclass
class MigrationReport:
    done: int = 0
    requeued: int = 0
    failed: int = 0
    windows_completed: int = 0


class LegacyStateMigrator:
    def __init__(
        self, db: Database, meta: MetaRepository, titles: TitleQueue,
        windows: WindowRepository, raw_root: Path,
    ) -> None:
        self._db = db
        self._meta = meta
        self._titles = titles
        self._windows = windows
        self._raw_root = raw_root

    def run(self) -> MigrationReport | None:
        if self._meta.get(FLAG) or not self._db.table_exists("seen_titles"):
            return None
        report = MigrationReport()
        with self._db.transaction():
            self._migrate_titles(report)
            self._migrate_windows(report)
            self._meta.set(FLAG, "1")
        LOGGER.info("Migrated legacy state: %s", report)
        return report

    # -- titles ---------------------------------------------------------------
    def _legacy_done(self, type_key: str, tmdb_id: int) -> bool:
        path = self._raw_root / type_key / f"{tmdb_id}.json"
        if not path.is_file():
            return False
        if type_key != "tv":
            return True
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        for season in payload.get("seasons") or []:
            number = season.get("season_number")
            if isinstance(number, int) and not (
                self._raw_root / "tv" / str(tmdb_id) / f"season_{number}.json"
            ).is_file():
                return False  # partially extracted series: redo it
        return True

    def _migrate_titles(self, report: MigrationReport) -> None:
        failures: dict[tuple[int, str], tuple[int, str]] = {}
        if self._db.table_exists("failures"):
            for r in self._db.query(
                "SELECT tmdb_id, type, MAX(attempts) AS a, MAX(error) AS e "
                "FROM failures WHERE tmdb_id IS NOT NULL GROUP BY tmdb_id, type"
            ):
                failures[(r["tmdb_id"], r["type"])] = (r["a"], r["e"])

        rows = []
        for r in self._db.query("SELECT tmdb_id, type, first_seen_at FROM seen_titles"):
            key = (r["tmdb_id"], r["type"])
            if key in failures:
                status, (attempts, error) = FAILED, failures[key]
                report.failed += 1
            elif self._legacy_done(r["type"], r["tmdb_id"]):
                status, attempts, error = DONE, 0, None
                report.done += 1
            else:
                status, attempts, error = PENDING, 0, None
                report.requeued += 1
            rows.append((r["tmdb_id"], r["type"], status, attempts, r["first_seen_at"], error))
        self._titles.import_rows(rows)

    # -- windows --------------------------------------------------------------
    @staticmethod
    def _cursor_rank(phase: str, year: int | None) -> tuple:
        order = {"popularity_movie": (0,), "popularity_tv": (1,), "complete": (3,)}
        if phase == "popularity":  # initial v1 checkpoint
            return (1,) if year is not None else (0,)
        if phase in order:
            return order[phase]
        if phase in ("release_movie", "release_tv") and year is not None:
            return (2, -year, 0 if phase == "release_movie" else 1)
        return (-1,)  # unknown: change nothing

    @staticmethod
    def _window_rank(title_type: str, kind: str, date_gte: str) -> tuple:
        sub = 0 if title_type == "movie" else 1
        if kind == POPULARITY:
            return (sub,)
        return (2, -int(date_gte[:4]), sub)

    def _migrate_windows(self, report: MigrationReport) -> None:
        if not self._db.table_exists("enumeration_checkpoint"):
            return
        rows = self._db.query("SELECT phase, year, page FROM enumeration_checkpoint WHERE id = 1")
        if not rows:
            return
        phase, year, page = rows[0]["phase"], rows[0]["year"], rows[0]["page"]
        cursor = self._cursor_rank(phase, year)
        for window in self._windows.all():
            rank = self._window_rank(window.title_type, window.kind, window.date_gte)
            if rank < cursor:
                self._windows.set_progress(window.id, COMPLETE, window.next_page)
                report.windows_completed += 1
            elif rank == cursor and window.kind == POPULARITY:
                self._windows.set_progress(window.id, PENDING, max(int(page or 1), 1))
