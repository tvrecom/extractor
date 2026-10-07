#  Phase 2

### Changing to `postgres`

Only the persistence layer changes. Enumeration, extraction, profiles, parsers, records, validation, reporting and pipeline never touch SQL, so they stay as they are. The edits are in `state.py`, `migration.py`, `config.py`, the wiring in `cli.py`, and the tests.

## 1. `state.py`: the database class and SQL dialect

Add a small `Database` protocol (`transaction()`, `execute`, `executemany`, `query`, `table_exists`, `close`) with a Postgres implementation. Use `psycopg` 3 with `dict_row`, so rows still support `row["name"]`.

The SQL changes, because the repositories have inline SQLite syntax:

| SQLite (now) | Postgres |
|---|---|
| `?` placeholders | `%s` |
| `INSERT OR IGNORE` | `INSERT ... ON CONFLICT DO NOTHING` |
| `INSERT OR REPLACE` (meta) | `ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value` |
| `INTEGER PRIMARY KEY AUTOINCREMENT` | `BIGINT GENERATED ALWAYS AS IDENTITY` |
| `ORDER BY ..., rowid` in `claim` | add an explicit `seq BIGINT GENERATED ALWAYS AS IDENTITY` column to `titles` and order by it |
| `truncated INTEGER` | `BOOLEAN` |
| `sqlite_master` lookup | `to_regclass('schema.table')` |
| `PRAGMA ...`, `executescript` | removed; apply the DDL as statements |
| TEXT timestamps | `TIMESTAMPTZ` (or keep TEXT to avoid touching `utc_now`) |

Two things need extra care:

- **Bulk inserts.** Don't rely on `executemany` rowcounts. Use one statement, `INSERT ... SELECT unnest(%s::bigint[]), %s, ... ON CONFLICT DO NOTHING`. It is faster, and the rowcount is the number of genuinely new IDs, which `new_ids` depends on.
- **Transactions.** The current single connection behind an `RLock` still works, because only the main thread writes. Keep it for the first version. A connection pool would mean tracking the nested-transaction depth per thread.

If you only ever want Postgres, replace the SQLite code. If you want both, put the SQL in a small dialect object and run the repository tests against each backend.

## 2. Locking and multi-worker (the real benefit)

- Replace the `flock` run lock with `pg_try_advisory_lock(<constant>)`. That works across hosts, and a crashed process releases the lock when its connection drops.
- To run several workers in parallel instead of one at a time, `claim()` has to stop being non-destructive. It would need `SELECT ... FOR UPDATE SKIP LOCKED`, an `in_progress` status, and a lease column (`claimed_at` plus a timeout) so work claimed by a crashed worker is reclaimed. The enumerator's `next_open()` needs the same treatment for windows. That is a design change, not a dialect change, so I'd treat it as phase two.

## 3. Config and secrets

- Add `TMDB_DATABASE_URL` (or a `DATABASE_URL` DSN) and drop `state_db`.
- Extend the redaction filter and `redact()` to scrub the DSN password the same way as the API key, since connection errors often print the DSN.
- Use `sslmode=require` outside local development.
- Replace `CREATE TABLE IF NOT EXISTS` with real versioned migrations (Alembic, or numbered `.sql` files). Postgres will outlive this script, so it needs an upgrade path.

## 4. Legacy migration

`LegacyStateMigrator` currently reads old v1 tables from the same database file. With Postgres, the v1 data is still in a SQLite file. Turn it into a one-off import: open the old `state.sqlite3` read-only and write into Postgres. The mapping logic stays the same.

## 5. Tests

Keep the fake-TMDB tests. Run the state and pipeline tests against a real Postgres, using `testcontainers` or `pytest-postgresql`, and run the repository contract tests against both backends. The atomic-commit and rollback tests matter most, since Postgres behaves differently under failed statements. After an error, the whole transaction is aborted until rollback.

## 6. What Postgres won't fix

`RawStore` and `JsonlSink` still write to the local filesystem. If workers run on several hosts, they need shared storage, such as object storage for raw responses and per-worker JSONL files. A single shared JSONL file isn't safe across processes. Both classes sit behind small protocols, so swapping them is contained. You could also write the extracted records into Postgres tables instead of JSONL, which would line up with the pgvector stage downstream. That changes the handoff format the extraction plan defines, so I'd treat it as a separate decision.

I can implement it as the first step: Postgres `Database`, the dialect-converted repositories, advisory lock, DSN redaction, SQLite-to-Postgres import, and a backend-parametrized test suite. The multi-worker `SKIP LOCKED` queue would be a second step. Do you want it Postgres-only, or both backends?

