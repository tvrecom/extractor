"""Exception hierarchy. Messages are redacted before they reach these types."""

from __future__ import annotations


class ExtractorError(Exception):
    """Base class for all extractor errors."""


class ConfigError(ExtractorError):
    """One or more environment settings are invalid."""

    def __init__(self, problems: list[str]) -> None:
        self.problems = list(problems)
        super().__init__("Invalid configuration:\n  - " + "\n  - ".join(self.problems))


class TMDBRequestError(ExtractorError):
    """A TMDB request failed permanently (non-retryable or budget exhausted)."""

    def __init__(
        self, message: str, attempts: int, status_code: int | None = None
    ) -> None:
        super().__init__(message)
        self.attempts = attempts
        self.status_code = status_code


class ValidationError(ExtractorError):
    """A record violates the canonical schema rules."""

    def __init__(self, errors: list[str]) -> None:
        self.errors = list(errors)
        super().__init__("; ".join(self.errors))


class ExtractionFailure(ExtractorError):
    """A single title failed at a named stage. Safe to persist (already redacted)."""

    def __init__(
        self,
        stage: str,
        message: str,
        attempts: int = 1,
        status_code: int | None = None,
    ) -> None:
        super().__init__(f"{stage}: {message}")
        self.stage = stage
        self.message = message
        self.attempts = attempts
        self.status_code = status_code

    @property
    def is_gone(self) -> bool:
        """The title itself no longer exists upstream (404 on the detail call)."""
        return self.stage == "detail_fetch" and self.status_code == 404
