"""Canonical-schema validation. Errors fail a title; warnings flag it."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .errors import ValidationError

_TYPES = {"movie", "series", "episode"}


@dataclass(frozen=True)
class ValidationResult:
    errors: tuple[str, ...]
    warnings: tuple[str, ...]


class CanonicalValidator:
    def validate(self, record: dict[str, Any]) -> ValidationResult:
        errors: list[str] = []
        warnings: list[str] = []
        rid = record.get("id", "<no id>")
        rtype = record.get("type")

        if not record.get("id"):
            errors.append("missing id")
        if rtype not in _TYPES:
            errors.append(f"{rid}: invalid type {rtype!r}")
        tmdb_id = record.get("tmdb_id")
        if not isinstance(tmdb_id, int) or isinstance(tmdb_id, bool):
            errors.append(f"{rid}: tmdb_id must be an integer")

        if rtype == "episode":
            for key in ("season", "episode"):
                value = record.get(key)
                if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                    errors.append(f"{rid}: episode requires integer {key}")
            if not record.get("parent_id"):
                errors.append(f"{rid}: episode requires parent_id")
            if not record.get("title"):
                warnings.append(f"{rid}: missing title")
        elif rtype in _TYPES:
            for key in ("season", "episode", "parent_id"):
                if record.get(key) is not None:
                    errors.append(f"{rid}: {key} is forbidden for type {rtype}")
            if not record.get("title"):
                errors.append(f"{rid}: missing title")
            if not record.get("cast"):
                warnings.append(f"{rid}: empty cast")
            if not record.get("genres"):
                warnings.append(f"{rid}: empty genres")

        if not record.get("synopsis"):
            warnings.append(f"{rid}: empty synopsis")
        if not record.get("release_date"):
            warnings.append(f"{rid}: missing release_date")
        return ValidationResult(tuple(errors), tuple(warnings))

    def require_valid(self, records: list[dict[str, Any]]) -> tuple[str, ...]:
        """Raise on any error across the batch; otherwise return all warnings."""
        errors: list[str] = []
        warnings: list[str] = []
        for record in records:
            result = self.validate(record)
            errors.extend(result.errors)
            warnings.extend(result.warnings)
        if errors:
            raise ValidationError(errors[:10])
        return tuple(warnings)
