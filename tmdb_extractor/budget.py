"""Per-invocation work budget (page count, detail count, wall clock)."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field


@dataclass
class RunBudget:
    max_enum_pages: int
    max_details: int
    max_seconds: float = 0.0  # 0 = unlimited
    clock: Callable[[], float] = time.monotonic
    _started: float = field(init=False)

    def __post_init__(self) -> None:
        self._started = self.clock()

    def elapsed(self) -> float:
        return self.clock() - self._started

    def expired(self) -> bool:
        return self.max_seconds > 0 and self.elapsed() >= self.max_seconds
