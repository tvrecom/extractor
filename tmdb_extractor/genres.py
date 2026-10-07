"""Genre-ID resolution, loaded once per run and cached."""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import Any

from .http_client import ApiClient

LOGGER = logging.getLogger(__name__)


class GenreCatalog:
    def __init__(self) -> None:
        self._maps: dict[str, dict[int, str]] = {}

    def load(self, client: ApiClient, type_keys: Iterable[str], language: str) -> None:
        LOGGER.info("Loading TMDB genre mappings")
        for type_key in type_keys:
            payload = client.get_json(f"genre/{type_key}/list", {"language": language})
            self._maps[type_key] = {
                int(g["id"]): g["name"]
                for g in payload.get("genres", [])
                if "id" in g and "name" in g
            }

    def resolve(self, type_key: str, payload: dict[str, Any]) -> list[str]:
        """Prefer embedded genre objects; fall back to genre_ids via the cache."""
        names = [
            g["name"] for g in payload.get("genres") or []
            if isinstance(g, dict) and g.get("name")
        ]
        if names:
            return names
        lookup = self._maps.get(type_key, {})
        return [lookup[i] for i in payload.get("genre_ids") or [] if i in lookup]
