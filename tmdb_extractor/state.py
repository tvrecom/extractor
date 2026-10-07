"""Durable SQLite state: one connection, explicit transactions, small repositories."""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, ContextManager, Protocol

from .models import (
    COMPLETE, DONE, FAILED, GONE, PENDING, SPLIT,
    Window, WindowSpec, WorkItem,
)
from .utils import utc_now

SCHEMA_VERSION = 2

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY, value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS titles (
    tmdb_id INTEGER NOT NULL,
    type TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',      -- pending|done|failed|gone
    attempts INTEGER NOT NULL DEFAULT 0,
    last_stage TEXT,
    last_error TEXT,
    first_seen_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (tmdb_id, type)
);
CREATE INDEX IF NOT EXISTS idx_titles_status ON titles (status, first_seen_at);
CREATE TABLE IF NOT EXISTS enum_windows (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title_type TEXT NOT NULL,
    kind TEXT NOT NULL,                          -- popularity|date
    date_gte TEXT NOT NULL DEFAULT '',
    date_lte TEXT NOT NULL DEFAULT '',
    priority INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',      -- pending|complete|split|failed
    next_page INTEGER NOT NULL DEFAULT 1,
    total_results INTEGER,
    total_pages INTEGER,
    ids_seen INTEGER NOT NULL DEFAULT 0,
    truncated INTEGER NOT NULL DEFAULT 0,
    failures INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    parent_id INTEGER,
    updated_at TEXT NOT NULL,
    UNIQUE (title_type, kind, date_gte, date_lte)
);
"""


class UnitOfWork(Protocol):
    def transaction(self) -> ContextManager[Any]: ...


class Database:
    """Thread-safe SQLite wrapper. Re-entrant `transaction()` for atomic multi-repo writes."""

    def __init__(self, path: Path | str) -> None:
        self._conn = sqlite3.connect(
            str(path), timeout=30, check_same_thread=False, isolation_level=None
        )
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._depth = 0
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(_SCHEMA)
        self._conn.execute(
            "INSERT OR IGNORE INTO meta (key, value) VALUES ('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            nested = self._depth > 0
            if not nested:
                self._conn.execute("BEGIN IMMEDIATE")
            self._depth += 1
            try:
                yield self._conn
            except BaseException:
                self._depth -= 1
                if not nested:
                    self._conn.execute("ROLLBACK")
                raise
            else:
                self._depth -= 1
                if not nested:
                    self._conn.execute("COMMIT")

    def execute(self, sql: str, params: tuple = ()) -> int:
        with self.transaction() as conn:
            return conn.execute(sql, params).rowcount

    def executemany(self, sql: str, rows: Iterable[tuple]) -> int:
        with self.transaction() as conn:
            return conn.executemany(sql, list(rows)).rowcount

    def query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def table_exists(self, name: str) -> bool:
        return bool(
            self.query(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
            )
        )

    def close(self) -> None:
        with self._lock:
            self._conn.close()


class MetaRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    def get(self, key: str) -> str | None:
        rows = self._db.query("SELECT value FROM meta WHERE key = ?", (key,))
        return rows[0]["value"] if rows else None

    def set(self, key: str, value: str) -> None:
        self._db.execute(
            "INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value)
        )


class TitleQueue:
    """Durable work queue of (tmdb_id, type) pairs; the single source of truth."""

    def __init__(self, db: Database, clock: Callable[[], str] = utc_now) -> None:
        self._db = db
        self._clock = clock

    # -- producers -----------------------------------------------------------
    def add_pending(self, tmdb_ids: Iterable[int], title_type: str) -> int:
        """Insert unseen IDs as pending. Returns the number actually new."""
        now = self._clock()
        return self._db.executemany(
            "INSERT OR IGNORE INTO titles "
            "(tmdb_id, type, status, attempts, first_seen_at, updated_at) "
            "VALUES (?, ?, 'pending', 0, ?, ?)",
            [(i, title_type, now, now) for i in tmdb_ids],
        )

    def import_rows(
        self, rows: Iterable[tuple[int, str, str, int, str, str | None]]
    ) -> int:
        """Bulk-load (id, type, status, attempts, first_seen_at, last_error)."""
        now = self._clock()
        return self._db.executemany(
            "INSERT OR IGNORE INTO titles (tmdb_id, type, status, attempts, "
            "first_seen_at, last_error, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            [(i, t, s, a, f, e, now) for i, t, s, a, f, e in rows],
        )

    # -- consumers -----------------------------------------------------------
    def claim(self, limit: int, max_retries: int, retry_quota: int) -> list[WorkItem]:
        """
        Select work: fresh pending titles plus a reserved share of failed retries.
        Selection does not mutate rows, so a crash can never lose a title.
        """
        if limit <= 0:
            return []
        quota = min(max(retry_quota, 0), limit)
        failed = self._select(FAILED, limit, max_retries)
        pending = self._select(PENDING, limit)
        retries = failed[:quota]
        fresh = pending[: limit - len(retries)]
        spare = limit - len(retries) - len(fresh)
        if spare > 0:
            retries += failed[len(retries): len(retries) + spare]
        return retries + fresh

    def _select(self, status: str, limit: int, max_retries: int | None = None) -> list[WorkItem]:
        sql = "SELECT tmdb_id, type, attempts FROM titles WHERE status = ?"
        params: list[Any] = [status]
        if max_retries is not None:
            sql += " AND attempts < ?"
            params.append(max_retries)
        order = "updated_at" if status == FAILED else "first_seen_at"
        sql += f" ORDER BY {order}, rowid LIMIT ?"
        params.append(limit)
        return [
            WorkItem(r["tmdb_id"], r["type"], r["attempts"])
            for r in self._db.query(sql, tuple(params))
        ]

    # -- outcomes ------------------------------------------------------------
    def mark_done(self, tmdb_id: int, title_type: str) -> None:
        self._db.execute(
            "UPDATE titles SET status='done', last_stage=NULL, last_error=NULL, "
            "updated_at=? WHERE tmdb_id=? AND type=?",
            (self._clock(), tmdb_id, title_type),
        )

    def mark_failed(self, tmdb_id: int, title_type: str, stage: str, error: str) -> None:
        self._db.execute(
            "UPDATE titles SET status='failed', attempts=attempts+1, last_stage=?, "
            "last_error=?, updated_at=? WHERE tmdb_id=? AND type=?",
            (stage, error, self._clock(), tmdb_id, title_type),
        )

    def mark_gone(self, tmdb_id: int, title_type: str, stage: str, error: str) -> None:
        self._db.execute(
            "UPDATE titles SET status='gone', attempts=attempts+1, last_stage=?, "
            "last_error=?, updated_at=? WHERE tmdb_id=? AND type=?",
            (stage, error, self._clock(), tmdb_id, title_type),
        )

    # -- reporting -----------------------------------------------------------
    def counts(self) -> dict[str, int]:
        result = {PENDING: 0, DONE: 0, FAILED: 0, GONE: 0}
        for row in self._db.query("SELECT status, COUNT(*) AS n FROM titles GROUP BY status"):
            result[row["status"]] = row["n"]
        return result

    def count_exhausted(self, max_retries: int) -> int:
        row = self._db.query(
            "SELECT COUNT(*) AS n FROM titles WHERE status='failed' AND attempts >= ?",
            (max_retries,),
        )[0]
        return row["n"]

    def done_by_type(self) -> dict[str, int]:
        return {
            r["type"]: r["n"]
            for r in self._db.query(
                "SELECT type, COUNT(*) AS n FROM titles WHERE status='done' GROUP BY type"
            )
        }


class WindowRepository:
    """Enumeration windows: the resumable crawl frontier."""

    _COLUMNS = (
        "id, title_type, kind, date_gte, date_lte, priority, status, next_page, "
        "total_results, total_pages, ids_seen, truncated, failures"
    )

    def __init__(self, db: Database, clock: Callable[[], str] = utc_now) -> None:
        self._db = db
        self._clock = clock

    @staticmethod
    def _to_window(row: Any) -> Window:
        return Window(
            id=row["id"], title_type=row["title_type"], kind=row["kind"],
            date_gte=row["date_gte"], date_lte=row["date_lte"],
            priority=row["priority"], status=row["status"],
            next_page=row["next_page"], total_results=row["total_results"],
            total_pages=row["total_pages"], ids_seen=row["ids_seen"],
            truncated=bool(row["truncated"]), failures=row["failures"],
        )

    def add_missing(self, specs: Iterable[WindowSpec]) -> int:
        now = self._clock()
        return self._db.executemany(
            "INSERT OR IGNORE INTO enum_windows (title_type, kind, date_gte, date_lte, "
            "priority, parent_id, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (s.title_type, s.kind, s.date_gte, s.date_lte, s.priority, s.parent_id, now)
                for s in specs
            ],
        )

    def all(self) -> list[Window]:
        rows = self._db.query(f"SELECT {self._COLUMNS} FROM enum_windows ORDER BY id")
        return [self._to_window(r) for r in rows]

    def next_open(self) -> Window | None:
        rows = self._db.query(
            f"SELECT {self._COLUMNS} FROM enum_windows WHERE status = 'pending' "
            "ORDER BY priority, date_lte DESC, id LIMIT 1"
        )
        return self._to_window(rows[0]) if rows else None

    def record_page(
        self, window_id: int, next_page: int, status: str,
        total_results: int, total_pages: int, ids_seen_delta: int, truncated: bool,
    ) -> None:
        self._db.execute(
            "UPDATE enum_windows SET next_page=?, status=?, total_results=?, "
            "total_pages=?, ids_seen=ids_seen+?, truncated=?, failures=0, "
            "last_error=NULL, updated_at=? WHERE id=?",
            (next_page, status, total_results, total_pages, ids_seen_delta,
             int(truncated), self._clock(), window_id),
        )

    def mark_complete(self, window_id: int) -> None:
        self._db.execute(
            "UPDATE enum_windows SET status='complete', updated_at=? WHERE id=?",
            (self._clock(), window_id),
        )

    def split(
        self, window_id: int, children: Iterable[WindowSpec],
        total_results: int, total_pages: int,
    ) -> None:
        with self._db.transaction():
            self._db.execute(
                "UPDATE enum_windows SET status='split', total_results=?, total_pages=?, "
                "updated_at=? WHERE id=?",
                (total_results, total_pages, self._clock(), window_id),
            )
            self.add_missing(children)

    def record_failure(self, window_id: int, error: str, park_after: int) -> bool:
        """Count a non-transient failure; park (status=failed) once it repeats."""
        with self._db.transaction():
            self._db.execute(
                "UPDATE enum_windows SET failures=failures+1, last_error=?, updated_at=? "
                "WHERE id=?", (error, self._clock(), window_id),
            )
            row = self._db.query(
                "SELECT failures FROM enum_windows WHERE id=?", (window_id,)
            )[0]
            parked = row["failures"] >= park_after
            if parked:
                self._db.execute(
                    "UPDATE enum_windows SET status='failed' WHERE id=?", (window_id,)
                )
            return parked

    def set_progress(self, window_id: int, status: str, next_page: int) -> None:
        self._db.execute(
            "UPDATE enum_windows SET status=?, next_page=?, updated_at=? WHERE id=?",
            (status, next_page, self._clock(), window_id),
        )

    def stats(self) -> dict[str, int]:
        result = {PENDING: 0, COMPLETE: 0, SPLIT: 0, FAILED: 0}
        for row in self._db.query(
            "SELECT status, COUNT(*) AS n FROM enum_windows GROUP BY status"
        ):
            result[row["status"]] = row["n"]
        result["truncated"] = self._db.query(
            "SELECT COUNT(*) AS n FROM enum_windows WHERE truncated = 1"
        )[0]["n"]
        return result
