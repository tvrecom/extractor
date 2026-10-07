"""TMDB HTTP access: retry policy, shared rate gate, secret redaction."""

from __future__ import annotations

import logging
import math
import random
import re
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Protocol

import requests

from .errors import TMDBRequestError

LOGGER = logging.getLogger(__name__)

_KEY_PARAM = re.compile(r"(api_key=)[^&\s'\"]+", re.IGNORECASE)


def redact(text: str, secret: str | None = None) -> str:
    """Remove the API key (query-string form and literal value) from text."""
    text = _KEY_PARAM.sub(r"\1***", text)
    if secret:
        text = text.replace(secret, "***")
    return text


class ApiClient(Protocol):
    """The only HTTP surface the rest of the code depends on."""

    def get_json(
        self, path: str, params: Mapping[str, Any] | None = None
    ) -> dict[str, Any]: ...


def parse_retry_after(value: str | None, now: datetime | None = None) -> float | None:
    """Parse a Retry-After header (delta-seconds or HTTP-date). None if unusable."""
    if not value:
        return None
    try:
        seconds = float(value)
    except ValueError:
        try:
            when = parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        seconds = (when - (now or datetime.now(timezone.utc))).total_seconds()
        return max(seconds, 0.0)
    if not math.isfinite(seconds) or seconds < 0:
        return None
    return seconds


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int = 4
    base_seconds: float = 1.0
    cap_seconds: float = 60.0
    jitter: float = 0.25

    @staticmethod
    def is_retryable(status_code: int) -> bool:
        return status_code == 429 or status_code >= 500

    def delay(
        self,
        attempt: int,
        retry_after: str | None = None,
        rng: random.Random | None = None,
    ) -> float:
        hinted = parse_retry_after(retry_after)
        if hinted is not None:
            return min(hinted, self.cap_seconds)
        rng = rng or random
        backoff = self.base_seconds * (2 ** (attempt - 1))
        backoff *= 1 + rng.uniform(0, self.jitter)
        return min(backoff, self.cap_seconds)


class RateGate:
    """Shared cooldown: one 429 pauses every worker thread, not just the caller."""

    def __init__(
        self,
        clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self._clock = clock
        self._sleep = sleeper
        self._lock = threading.Lock()
        self._until = 0.0

    def penalize(self, seconds: float) -> None:
        with self._lock:
            self._until = max(self._until, self._clock() + seconds)

    def wait(self) -> None:
        while True:
            with self._lock:
                remaining = self._until - self._clock()
            if remaining <= 0:
                return
            self._sleep(remaining)


class TMDBClient:
    """requests-based client with per-thread sessions and bounded retries."""

    def __init__(
        self,
        api_key: str,
        base_url: str,
        timeout: float,
        policy: RetryPolicy,
        gate: RateGate | None = None,
        session_factory: Callable[[], Any] | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        rng: random.Random | None = None,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._policy = policy
        self._sleep = sleeper
        self._gate = gate or RateGate(sleeper=sleeper)
        self._rng = rng
        self._session_factory = session_factory or self._default_session
        self._local = threading.local()

    @staticmethod
    def _default_session() -> requests.Session:
        session = requests.Session()
        session.headers.update(
            {"Accept": "application/json", "User-Agent": "catalog-ingestion/tmdb-extractor"}
        )
        return session

    def _session(self) -> Any:
        session = getattr(self._local, "session", None)
        if session is None:
            session = self._local.session = self._session_factory()
        return session

    def _clean(self, text: str) -> str:
        return redact(text, self._api_key)

    def get_json(
        self, path: str, params: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        query = dict(params or {})
        query["api_key"] = self._api_key
        url = f"{self._base_url}/{path.lstrip('/')}"
        max_attempts = self._policy.max_attempts

        for attempt in range(1, max_attempts + 1):
            self._gate.wait()
            try:
                response = self._session().get(url, params=query, timeout=self._timeout)
                status = response.status_code
                payload = response.json() if status == 200 else None
            except (requests.RequestException, ValueError) as exc:
                # `from None`: the chained exception text embeds the key-bearing URL.
                detail = self._clean(f"{type(exc).__name__}: {exc}")
                if attempt >= max_attempts:
                    raise TMDBRequestError(
                        f"TMDB request failed after {attempt} attempts: {path}: {detail}",
                        attempts=attempt,
                    ) from None
                self._backoff(path, attempt, None, detail, None)
                continue

            if status == 200:
                if not isinstance(payload, dict):
                    raise TMDBRequestError(
                        f"Unexpected non-object response from {path}",
                        attempts=attempt, status_code=200,
                    )
                return payload

            body = self._clean(response.text[:500])
            if not self._policy.is_retryable(status) or attempt >= max_attempts:
                raise TMDBRequestError(
                    f"TMDB request failed: {path} HTTP {status} "
                    f"after {attempt} attempt(s): {body}",
                    attempts=attempt, status_code=status,
                )
            self._backoff(
                path, attempt, status, f"HTTP {status}", response.headers.get("Retry-After")
            )

        raise TMDBRequestError(  # pragma: no cover - loop always returns or raises
            f"TMDB request exhausted retry budget: {path}", attempts=max_attempts
        )

    def _backoff(
        self, path: str, attempt: int, status: int | None, detail: str,
        retry_after: str | None,
    ) -> None:
        delay = self._policy.delay(attempt, retry_after, self._rng)
        LOGGER.warning(
            "TMDB retryable failure: path=%s status=%s attempt=%s/%s sleeping=%.2fs (%s)",
            path, status, attempt, self._policy.max_attempts, delay, detail,
        )
        if status == 429:
            self._gate.penalize(delay)  # the next gate.wait() sleeps, for all threads
        else:
            self._sleep(delay)
