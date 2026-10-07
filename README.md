# TMDB Catalog Extractor

A resumable crawler that walks TMDB's catalog and writes raw, canonical-schema-shaped
metadata for movies, TV series and episodes as JSONL. It is the extraction stage of a
catalog ingestion pipeline: it gets data out of TMDB faithfully and leaves normalization,
licensing, transcripts, embeddings and the pgvector load to later stages.

It is built to run for months as a cron job. Each invocation does a small, bounded amount
of work, saves its position, and exits. Pacing is deliberately gentle on TMDB's API.

## What it does

1. **Enumerates** TMDB through `/discover/movie` and `/discover/tv`, with no starting list of titles.
2. **Deduplicates** `(tmdb_id, type)` pairs across all passes in a persistent queue.
3. **Extracts details** with one bundled call per title (`append_to_response`), plus one call per season for TV.
4. **Saves the raw TMDB response** to disk before any parsing.
5. **Emits records** to JSONL streams: `movies`, `series`, `episodes`, `failures`.
6. **Retries** HTTP 429 and 5xx responses with exponential backoff; failed titles are re-attempted on later runs.
7. **Isolates failures**: one bad title never stops the batch.
8. **Writes a run summary** after every invocation, including cumulative progress.

## Quick start

```bash
pip install -r requirements.txt
export TMDB_API_KEY=your_key
python -m tmdb_extractor
```

Run it repeatedly (cron). A typical crontab entry:

```cron
*/30 * * * *  cd /srv/catalog && TMDB_API_KEY=... python -m tmdb_extractor >> extractor.log 2>&1
```

Overlapping invocations are safe: a second run finds the lock held and exits `0` immediately.

Exit codes: `0` run completed (per-title failures are normal and reported in the summary),
`1` a pipeline phase failed, `2` invalid configuration.

## How a run works

```
lock → validate config → seed windows → record baseline totals → load genres
     → enumerate (bounded pages) → claim titles from queue → extract (bounded titles)
     → write run summary
```

A phase that fails is recorded and the remaining phases still run, so the summary is always written.

### Enumeration

TMDB returns 20 results per page and refuses any page above 500, so a single query can reach
at most 10,000 titles. The crawler works through persisted **windows** instead:

- **Popularity pass** (first): the top pages by `popularity.desc` for each type, so well-known titles appear early.
- **Release-year sweep**: every year from `TMDB_END_YEAR` down to `TMDB_START_YEAR`, for each type, sorted by release date ascending (a stable order between runs).
- If a window reports more than 500 pages, it is **split in half by date**, recursively. A single day that still exceeds 500 pages is marked `truncated`, so the gap is visible in the summary rather than silent.

Each page is committed in one database transaction covering both the discovered IDs and the
window's cursor. A crash can cause at most one page to be fetched again; it cannot lose or duplicate IDs.

### Extraction

Enumeration only adds titles to a durable queue. Extraction claims work from that queue
(`pending` titles first, plus a reserved share for retrying `failed` ones), so the per-run
detail cap never discards anything.

Each title is processed all-or-nothing: fetch the detail and all seasons, validate every record,
then write them together. If any season fails, no records for that title are written, and the retry
produces exactly one clean set.

| Title status | Meaning |
|---|---|
| `pending` | discovered, not yet extracted |
| `done` | extracted and emitted |
| `failed` | last attempt failed; retried until `TMDB_MAX_TITLE_RETRIES` is reached |
| `gone` | TMDB returned 404 for the detail call; never retried |

Delivery is **at-least-once**: a crash between writing records and marking the title done can
duplicate that one title's records. Downstream consumers should dedupe on `id`.

### Rate limiting and retries

- Controlled concurrency (`TMDB_CONCURRENCY`, default 6), well under TMDB's ~50 req/s ceiling.
- 429 and 5xx responses are retried with exponential backoff and jitter, honouring `Retry-After` (capped at 60 s).
- A 429 pauses **all** worker threads through a shared cooldown, not just the one that was throttled.
- Other 4xx responses are not retried.

## Output

Everything lives under `TMDB_DATA_DIR` (default `./tmdb_extraction`):

```
state.sqlite3                      queue, enumeration windows, metadata
run.lock                           overlap guard
run_summaries.jsonl                one line per invocation
extracted/
    movies.jsonl
    series.jsonl
    episodes.jsonl
    failures.jsonl
raw/
    movie/<id // 1000>/<id>.json
    tv/<id // 1000>/<id>.json
    tv/<id // 1000>/<id>/season_<n>.json
```

Raw files are untouched TMDB responses, sharded so no directory grows unbounded.

### Record shape

Records follow the canonical catalog schema, with one extra provenance field:

```json
{
  "id": "title_movie_550",
  "tmdb_id": 550,
  "imdb_id": "tt0137523",
  "type": "movie",
  "parent_id": null,
  "title": "Fight Club",
  "synopsis": "...",
  "genres": ["Drama"],
  "cast": [{"name": "...", "role": "..."}],
  "director": ["..."],
  "release_date": "1999-10-15",
  "season": null,
  "episode": null,
  "runtime_minutes": 139,
  "maturity_rating": "R",
  "language": "en",
  "license_expiry": null,
  "transcript_ref": null,
  "status": "active",
  "content_hash": "sha256:...",
  "source_system": "tmdb",
  "last_updated": "2026-10-07T00:00:00Z",
  "_source": {"raw_response": "raw/movie/0000/550.json"}
}
```

- `type` is `movie`, `series` or `episode`.
- IDs are namespaced by type, because TMDB's movie and TV ID spaces overlap: `title_movie_<id>`, `title_tv_<id>`, and `title_tv_<id>_s<season>_e<episode>`.
- Episodes set `parent_id`, `season` and `episode`; no other type may.
- Season 0 (specials) is kept.
- `content_hash` covers only synopsis, cast and genres, the fields that affect retrieval relevance.
- `maturity_rating` uses `TMDB_REGION`, preferring a theatrical certification for movies.
- TV `runtime_minutes` is the mean of `episode_run_time`, falling back to fetched episode runtimes, then the last aired episode.
- `licensing`, `transcript_ref` and `license_expiry` are always null here; later stages fill them.
- `_source.raw_response` is a path relative to the data directory.

### Validation

Records are checked against the canonical schema rules before they are written. A **hard error**
(missing title, malformed episode numbering, invalid type) fails the title and writes a line to
`failures.jsonl`. A **warning** (empty synopsis, no cast, no release date) still emits the record
and counts it as `flagged` in the summary.

### Run summary

Each line of `run_summaries.jsonl` contains, among other fields:

```json
{
  "run_at": "...", "status": "ok",
  "enumeration": {"pages": 10, "new_ids": 187, "windows_split": 1, "frontier_exhausted": false},
  "extraction": {"attempted": 100, "succeeded": 97, "failed": 3, "gone": 1, "flagged": 4, "episodes": 812, "retried": 5},
  "backlog": {"pending": 4120, "done": 9033, "failed_retryable": 2, "failed_exhausted": 1, "gone": 6},
  "progress": {"movie": {"done": 6100, "estimated_total": 1100000, "percent": 0.555}},
  "checkpoint": {"windows": {"pending": 210, "complete": 40, "split": 3, "failed": 0, "truncated": 0}, "current": "movie:2019-01-01..2019-12-31 page=4"}
}
```

`attempted = succeeded + failed` always holds. Progress is cumulative across runs, measured against
TMDB's unfiltered discover totals recorded on the first run. `status` is `ok`, `partial` (some titles
failed) or `error` (a phase failed).

## Configuration

`TMDB_API_KEY` is required. The extractor checks it first and exits with a clear error before doing
any file or network work. The key is never written to logs, the database, or output files.

| Variable | Default | Purpose |
|---|---|---|
| `TMDB_API_KEY` | required | TMDB v3 API key |
| `TMDB_REGION` | `US` | region for maturity ratings |
| `TMDB_LANGUAGE` | `en-US` | response language |
| `TMDB_START_YEAR` | `1900` | oldest release year to sweep |
| `TMDB_END_YEAR` | current year + 1 | newest release year to sweep |
| `TMDB_CONCURRENCY` | `6` | parallel detail requests |
| `TMDB_MAX_ENUM_PAGES_PER_RUN` | `10` | discover pages per invocation |
| `TMDB_MAX_DETAILS_PER_RUN` | `100` | titles extracted per invocation |
| `TMDB_MAX_RUN_SECONDS` | `0` | wall-clock deadline (`0` = none) |
| `TMDB_POPULARITY_PAGES` | `500` | depth of the popularity pass (max 500) |
| `TMDB_REQUEST_TIMEOUT` | `30` | seconds per request |
| `TMDB_MAX_ATTEMPTS` | `4` | HTTP attempts per request |
| `TMDB_BACKOFF_BASE_SECONDS` | `1.0` | first backoff delay |
| `TMDB_MAX_TITLE_RETRIES` | `3` | run-level retries before a title is parked |
| `TMDB_RETRY_FRACTION` | `0.2` | share of each run reserved for retries |
| `TMDB_DATA_DIR` | `tmdb_extraction` | output and state location |
| `LOG_LEVEL` | `INFO` | logging verbosity |

Invalid values are all reported together in a single error.

### Sizing a schedule

The defaults give each run about 200 enumerated IDs and 100 detail extractions. TV series cost one
extra request per season. Raise the per-run caps and `TMDB_CONCURRENCY` for a faster backfill, or
lower them to stay quiet. Because a run stops at its caps (or `TMDB_MAX_RUN_SECONDS`), the schedule
interval and the caps together set the overall pace.

## Project layout

```
tmdb_extractor/
    cli.py            entry point and the only place objects are wired together
    pipeline.py       one invocation: phases, error isolation, summary
    config.py         environment settings and validation
    http_client.py    TMDB client: retries, shared rate gate, secret redaction
    state.py          SQLite: title queue, enumeration windows, metadata
    enumeration.py    window seeding, discover paging, window splitting, baseline totals
    extraction.py     per-title extractor and the bounded worker pool
    profiles.py       movie and TV differences (endpoints, fields, ratings, runtime, seasons)
    parsers.py        pure functions that read TMDB payloads
    records.py        builders for title and episode records
    validation.py     canonical-schema checks
    genres.py         genre-ID lookup, cached once per run
    storage.py        raw response store and JSONL sinks
    reporting.py      run summary and cumulative progress
    budget.py         per-run work and time limits
tests/                fake TMDB server plus tests for each stage
```

Movie and TV behaviour lives behind a small `TitleProfile` interface, so the enumerator and extractor
contain no per-type branching.

## Testing

```bash
python -m pytest
```

The suite uses an in-process fake TMDB with realistic paginated `/discover` behaviour, so it needs no
network or API key. It covers resumable enumeration (no duplicates, exact page resume), window
splitting and truncation, retry and backoff, golden records for a movie and a series with specials,
partial-failure handling, cumulative run summaries, run locking, and secret redaction.

## Scope

In scope: enumeration, detail and episode extraction, raw capture, JSONL handoff, run auditing.

Out of scope, by design: CMS integration, licensing data, transcripts, embeddings, and writing the
final canonical or pgvector database. Those belong to later pipeline stages that consume the JSONL output.

## Known limitations

- Single-process: one run at a time, enforced by a file lock on one host.
- A single release day with more than 10,000 titles is truncated at TMDB's page cap (and reported).
- Titles with no release date are not reached by the release-year sweep; they appear only if the popularity pass finds them.
- Episode-level cast is limited to guest stars, which is what TMDB's season endpoint provides.
- TMDB updates are not tracked yet: a title is extracted once. Refreshing existing titles is future work.
