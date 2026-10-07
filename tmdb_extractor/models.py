"""Plain data types shared by the state, enumeration and extraction layers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

# Window kinds
POPULARITY = "popularity"
DATE = "date"

# Title statuses
PENDING = "pending"
DONE = "done"
FAILED = "failed"
GONE = "gone"

# Window statuses (PENDING is shared)
COMPLETE = "complete"
SPLIT = "split"
# FAILED (parked) is shared


@dataclass(frozen=True)
class WorkItem:
    tmdb_id: int
    title_type: str
    attempts: int = 0


@dataclass(frozen=True)
class WindowSpec:
    """Definition of a unit of enumeration work."""

    title_type: str
    kind: str
    date_gte: str = ""
    date_lte: str = ""
    priority: int = 1
    parent_id: int | None = None


@dataclass(frozen=True)
class Window:
    """A persisted enumeration unit: one filter range for one title type."""

    id: int
    title_type: str
    kind: str
    date_gte: str
    date_lte: str
    priority: int
    status: str
    next_page: int
    total_results: int | None = None
    total_pages: int | None = None
    ids_seen: int = 0
    truncated: bool = False
    failures: int = 0

    @property
    def span_days(self) -> int:
        if self.kind != DATE:
            return 0
        return (date.fromisoformat(self.date_lte) - date.fromisoformat(self.date_gte)).days

    def bisect(self) -> tuple[WindowSpec, WindowSpec]:
        """Split a multi-day date window into two adjacent, non-overlapping halves."""
        if self.span_days < 1:
            raise ValueError("A single-day window cannot be bisected")
        start = date.fromisoformat(self.date_gte)
        mid = start + timedelta(days=self.span_days // 2)
        left = WindowSpec(
            self.title_type, DATE, self.date_gte, mid.isoformat(),
            self.priority, self.id,
        )
        right = WindowSpec(
            self.title_type, DATE, (mid + timedelta(days=1)).isoformat(),
            self.date_lte, self.priority, self.id,
        )
        return left, right

    def describe(self) -> str:
        if self.kind == POPULARITY:
            return f"{self.title_type}:popularity page={self.next_page}"
        return f"{self.title_type}:{self.date_gte}..{self.date_lte} page={self.next_page}"
