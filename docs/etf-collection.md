# ETF exchange collection worker

The FastAPI lifespan owns this automatically scheduled worker. This is implemented scheduling, not
an instruction to install cron. No service, cron job, or production write was
performed while developing this feature.

## Configuration

Set these in the service environment (never commit credentials):

```
ETF_COLLECTION_ENABLED=true
ETF_COLLECTION_WRITER_URL=mysql+pymysql://ETF_WRITER:REPLACE_ME@DB_HOST/showallopts?charset=utf8mb4
ETF_COLLECTION_LOOKBACK_DAYS=300
ETF_COLLECTION_BATCH_DAYS=5
```

Collection is enabled by default; set `ETF_COLLECTION_ENABLED=false` to explicitly opt out. A valid independent writer connection is still required, and is never fabricated or provisioned automatically. A missing writer URL reports
`missing_writer_configuration` with `ready=false`, logs a generic startup warning, and does not start a thread or fall back to the
application's readonly account. The writer URL must target exactly the same driver, host, port and database as the read connection; only credentials may differ. A mismatch or malformed URL disables the worker before network activity. The independently provisioned writer needs only
ETF table privileges (including initial table creation). Use the same shared
persistent LOG_DIR for every process on a host. The portalocker lock and SQLite
ledger prevent overlapping work and duplicate successful slots across these
processes. This is not a distributed lock across hosts with separate disks: run
the enabled collector on one host, with the others disabled.

## Timing and reliability

All times use Asia/Shanghai, unaffected by server timezone. 18:30 collects the
latest verified closed session's shares and prices. 09:30 and 11:30 only recheck
shares, never promote a morning intraday quote into a daily close. These are
bounded verification opportunities, **not a claim that the exchange publishes
final shares by either time**. Official dates and values are authoritative.
No immediate startup collection runs; the first future slot is eligible. At the first timer check, an already recorded running/failed/partial same-day slot can resume if its retry allowance and 30-minute delay permit. Unfinished prior-day slots become abandoned while their observation gaps remain. A
minute timer only checks local state. The unchanged 07:00 MySQL-to-cache sync
continues. Each slot permits at most two attempts, separated by 30 minutes;
request-level retries are also bounded by the exchange client. Writer connections have a 10-second connect timeout and 60-second read/write timeouts. Missing latest-session data marks a slot partial even when available rows were committed, enabling the same delayed retry. Each ETF price history is fetched at most once per run and reused across backfill dates. Interrupted
running slots can retry under the same bounds after the process lock releases.

Each run maintains a 300-session requested coverage window and processes at most
five old pending dates plus the latest session. Older persisted gaps remain
eligible even when outside the rolling window. Morning batches only include
share gaps; evening batches include prices too. Initial 300-session backfill is
therefore gradual, not promised complete on the first day. Every ETF, date and
kind has a durable state; no MAX(date) watermark hides gaps. Each day/kind/ETF source request is isolated so a later source failure does not discard earlier successful observations. Attempt budgets are settled only after the writer transaction succeeds; a writer failure consumes no observation budget. Shared SSE daily disclosures and per-ETF price responses (including source failures) are reused within each run. A particular gap has
at most six settled attempts before `retry_exhausted`, requiring operator investigation
and a deliberate ledger reset after fixing the cause. Schedules also run on weekends and holidays for share verification and gradual historical backfill. The latest confirmed closed session remains eligible for a verified Shenzhen close, including Friday on a weekend. Shenzhen unverified older
prices are `blocked_unverified_history` and not requested indefinitely. Existing
completed values survive missing responses. Failed transactions do not mark any
observations complete. A crash after the MySQL transaction and before ledger
commit leads only to an idempotent replay.

Only the ETF cache is refreshed after a successful transaction. Lock contention
or a failed cache publish leaves `cache_pending` durable, retried no faster than
five minutes without re-fetching exchanges. The authenticated read-only `GET /api/v1/etf-shares/collection-status` exposes schedule,
coverage, recent slots and all incomplete ETF/date observations. Errors retain
exception types only, never database URLs or response payloads.

Writer failures are classified safely as `writer_permission_denied` (including
MySQL 1044/1045/1142/1143), `writer_table_missing`, `writer_database_missing`,
`writer_connection_failed`, or `writer_write_failed`. Status reports `ready=false`
until a subsequent successful write. No account creation, privilege changes, or
readonly-credential fallback is attempted. Deployment without a configured and
authorized writer remains **not ready**, not a successful automatic collection.
