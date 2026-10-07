"""File artifacts: raw TMDB responses and JSONL record streams."""

from __future__ import annotations

import os
import threading
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any, Protocol

from .utils import compact_json


class Streams:
    MOVIES = "movies"
    SERIES = "series"
    EPISODES = "episodes"
    FAILURES = "failures"

    ALL = (MOVIES, SERIES, EPISODES, FAILURES)


class RawStore:
    """
    Persists untouched TMDB responses, sharded by id//1000 so no directory
    grows unbounded. Paths returned to callers are relative to `base_dir`.
    """

    def __init__(self, root: Path, base_dir: Path) -> None:
        self._root = root
        self._base = base_dir

    @property
    def root(self) -> Path:
        return self._root

    def _shard(self, title_type: str, tmdb_id: int) -> Path:
        return self._root / title_type / f"{tmdb_id // 1000:04d}"

    def title_path(self, title_type: str, tmdb_id: int) -> Path:
        return self._shard(title_type, tmdb_id) / f"{tmdb_id}.json"

    def child_path(self, title_type: str, tmdb_id: int, name: str) -> Path:
        return self._shard(title_type, tmdb_id) / str(tmdb_id) / f"{name}.json"

    def relative(self, path: Path) -> str:
        return path.relative_to(self._base).as_posix()

    def save_title(self, title_type: str, tmdb_id: int, payload: Mapping[str, Any]) -> str:
        path = self.title_path(title_type, tmdb_id)
        self._write(path, payload)
        return self.relative(path)

    def save_child(
        self, title_type: str, tmdb_id: int, name: str, payload: Mapping[str, Any]
    ) -> str:
        path = self.child_path(title_type, tmdb_id, name)
        self._write(path, payload)
        return self.relative(path)

    @staticmethod
    def _write(path: Path, payload: Mapping[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        tmp.write_text(compact_json(payload), encoding="utf-8")
        tmp.replace(path)


class RecordSink(Protocol):
    def emit_batch(self, items: Iterable[tuple[str, dict[str, Any]]]) -> None: ...


class JsonlFile:
    """Thread-safe appender for one JSONL file."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()

    def append_many(self, records: Iterable[Mapping[str, Any]]) -> None:
        text = "".join(compact_json(r) + "\n" for r in records)
        if not text:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock, self.path.open("a", encoding="utf-8") as handle:
            handle.write(text)

    def append(self, record: Mapping[str, Any]) -> None:
        self.append_many([record])


class JsonlSink:
    """Routes records to named streams; each batch is one locked write per stream."""

    def __init__(self, files: Mapping[str, JsonlFile]) -> None:
        self._files = dict(files)

    @classmethod
    def for_directory(cls, directory: Path) -> "JsonlSink":
        return cls({s: JsonlFile(directory / f"{s}.jsonl") for s in Streams.ALL})

    def emit_batch(self, items: Iterable[tuple[str, dict[str, Any]]]) -> None:
        grouped: dict[str, list[dict[str, Any]]] = {}
        for stream, record in items:
            grouped.setdefault(stream, []).append(record)
        for stream, records in grouped.items():
            self._files[stream].append_many(records)

    def emit(self, stream: str, record: dict[str, Any]) -> None:
        self.emit_batch([(stream, record)])
