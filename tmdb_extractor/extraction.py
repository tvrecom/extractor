"""Per-title extraction (TitleExtractor) and bounded concurrent execution (BatchRunner)."""

from __future__ import annotations

import logging
from collections import deque
from collections.abc import Mapping, Sequence
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import asdict, dataclass
from typing import Protocol

from .budget import RunBudget
from .errors import ExtractionFailure
from .genres import GenreCatalog
from .http_client import ApiClient, redact
from .models import WorkItem
from .profiles import ChildPayload, TitleProfile
from .records import TitleRecordBuilder
from .storage import RawStore, RecordSink, Streams
from .utils import utc_now
from .validation import CanonicalValidator

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class TitleResult:
    episodes: int
    warnings: tuple[str, ...] = ()


class TitleExtractor:
    """
    Extracts one title all-or-nothing: fetch everything, validate everything,
    then emit in one batch. A failure at any stage leaves no output records.
    Orchestration only; type differences live in the TitleProfile.
    """

    def __init__(
        self, client: ApiClient, raw_store: RawStore, sink: RecordSink,
        profiles: Mapping[str, TitleProfile], genres: GenreCatalog,
        title_builder: TitleRecordBuilder, validator: CanonicalValidator, language: str,
    ) -> None:
        self._client = client
        self._raw = raw_store
        self._sink = sink
        self._profiles = profiles
        self._genres = genres
        self._titles = title_builder
        self._validator = validator
        self._language = language

    def extract(self, item: WorkItem) -> TitleResult:
        stage = "detail_fetch"
        try:
            profile = self._profiles[item.title_type]
            payload = self._client.get_json(
                profile.detail_endpoint(item.tmdb_id),
                {"language": self._language,
                 "append_to_response": profile.append_to_response},
            )
            # Raw is persisted before any parsing.
            raw_ref = self._raw.save_title(item.title_type, item.tmdb_id, payload)

            stage = "child_fetch"
            children = []
            for ref in profile.child_refs(payload):
                child = self._client.get_json(ref.endpoint, {"language": self._language})
                child_ref = self._raw.save_child(
                    item.title_type, item.tmdb_id, ref.raw_name, child
                )
                children.append(ChildPayload(ref, child, child_ref))

            stage = "build"
            genres = self._genres.resolve(profile.type_key, payload)
            record = self._titles.build(
                profile, item.tmdb_id, payload, genres,
                profile.runtime_minutes(payload, children), raw_ref,
            )
            child_records = profile.build_child_records(
                record["id"], payload, genres, children
            )

            stage = "validate"
            warnings = self._validator.require_valid(
                [record, *(r for _, r in child_records)]
            )

            stage = "emit"
            self._sink.emit_batch([(profile.output_stream, record), *child_records])
            return TitleResult(len(child_records), warnings)
        except ExtractionFailure:
            raise
        except Exception as exc:
            raise ExtractionFailure(
                stage, redact(repr(exc)),
                attempts=getattr(exc, "attempts", 1),
                status_code=getattr(exc, "status_code", None),
            ) from None


class TitleOutcomeStore(Protocol):
    def mark_done(self, tmdb_id: int, title_type: str) -> None: ...
    def mark_failed(self, tmdb_id: int, title_type: str, stage: str, error: str) -> None: ...
    def mark_gone(self, tmdb_id: int, title_type: str, stage: str, error: str) -> None: ...


@dataclass
class BatchSummary:
    attempted: int = 0
    succeeded: int = 0
    failed: int = 0      # includes `gone`
    gone: int = 0
    flagged: int = 0     # succeeded, but with schema warnings
    episodes: int = 0
    retried: int = 0     # attempted titles that had failed before

    def to_dict(self) -> dict[str, int]:
        return asdict(self)


class BatchRunner:
    """Bounded worker pool; stops dispatching when the run budget expires."""

    def __init__(
        self, extractor: TitleExtractor, outcomes: TitleOutcomeStore,
        failure_sink: RecordSink, workers: int,
    ) -> None:
        self._extractor = extractor
        self._outcomes = outcomes
        self._failures = failure_sink
        self._workers = workers

    def run(self, items: Sequence[WorkItem], budget: RunBudget) -> BatchSummary:
        summary = BatchSummary()
        queue = deque(items)
        in_flight: dict[Future, WorkItem] = {}
        LOGGER.info("Detail extraction: titles=%s workers=%s", len(queue), self._workers)

        with ThreadPoolExecutor(max_workers=self._workers, thread_name_prefix="tmdb") as pool:
            while queue or in_flight:
                while queue and len(in_flight) < self._workers and not budget.expired():
                    item = queue.popleft()
                    in_flight[pool.submit(self._extractor.extract, item)] = item
                if not in_flight:
                    break  # budget expired; remaining titles stay queued for next run
                done, _ = wait(in_flight, return_when=FIRST_COMPLETED)
                for future in done:
                    self._record(in_flight.pop(future), future, summary)
        return summary

    def _record(self, item: WorkItem, future: Future, summary: BatchSummary) -> None:
        summary.attempted += 1
        summary.retried += item.attempts > 0
        try:
            result: TitleResult = future.result()
        except ExtractionFailure as failure:
            self._record_failure(item, failure, summary)
            return
        except Exception as exc:  # defensive: extractor should only raise ExtractionFailure
            LOGGER.error("Unexpected extractor error", exc_info=exc)
            self._record_failure(
                item, ExtractionFailure("unexpected", redact(repr(exc))), summary
            )
            return

        self._outcomes.mark_done(item.tmdb_id, item.title_type)
        summary.succeeded += 1
        summary.episodes += result.episodes
        if result.warnings:
            summary.flagged += 1
            LOGGER.warning("Flagged %s %s: %s", item.title_type, item.tmdb_id,
                           "; ".join(result.warnings[:3]))
        LOGGER.info("Extracted %s tmdb_id=%s episodes=%s",
                    item.title_type, item.tmdb_id, result.episodes)

    def _record_failure(
        self, item: WorkItem, failure: ExtractionFailure, summary: BatchSummary
    ) -> None:
        summary.failed += 1
        if failure.is_gone:
            summary.gone += 1
            self._outcomes.mark_gone(item.tmdb_id, item.title_type, failure.stage, failure.message)
        else:
            self._outcomes.mark_failed(item.tmdb_id, item.title_type, failure.stage, failure.message)
        self._failures.emit_batch([(Streams.FAILURES, {
            "tmdb_id": item.tmdb_id, "type": item.title_type, "stage": failure.stage,
            "error": failure.message, "attempts": failure.attempts,
            "status_code": failure.status_code, "gone": failure.is_gone,
            "title_attempt": item.attempts + 1, "last_updated": utc_now(),
        })])
        LOGGER.warning("Failed %s tmdb_id=%s stage=%s: %s", item.title_type,
                       item.tmdb_id, failure.stage, failure.message)
