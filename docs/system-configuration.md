# System configuration

Administrators manage one shared MySQL database, all automatic schedules, AShareHub and Telegram credentials in 系统配置. Other pages retain manual collection/cancellation and data displays, not duplicate configuration forms. All times use Beijing time.

The actual APP_ENV_FILE (normally config/app.config) is authoritative for DB_* and integration credentials. Page saves update that same file atomically, preserving unrelated text, owner, group and permissions. Environment-variable overrides and insufficient permissions produce explicit errors. Secrets are write-only: unchanged values are retained and explicit clearing is separate. Existing encrypted Telegram values are migration defaults only when file keys are absent, never when explicitly cleared.

Database changes do not copy or delete business data. Old encrypted module configurations remain recoverable. Different legacy targets and unreadable optional configurations require administrator review; the affected collector is disabled instead of silently mixing sources. The configured account needs the read/write permissions required by enabled features.

Running synchronization/collection blocks database edits. New operations observe the shared configuration revision; old holdings reads cannot commit after a change. Database-target or holdings-table changes establish a fresh baseline without synthetic buy/sell events. Password-only rotation reconnects without resetting the baseline. Cache snapshots are source-bound; changing target requires a full refresh and never merges old-source rows. A persistent namespace keeps workers and restarted services on the same synchronization lock.

Defaults: holdings every day 09:25–15:10 at 15-second intervals; market heat/crowding 00:01; growth/dividend every day at 18:00. A00 retains two daily slots, and cash-cow screening runs combined 3/5/10-year periods on the last calendar day of each month. Existing bounded gap-filling and retry rules remain. Non-trading dates do not fabricate market observations.

Eight scheduled collector groups have individual Telegram switches, off by default. Only actual published business-data changes trigger these notifications. No-change successes and failures are silent; partial failures with writes may notify. Manual runs do not send scheduled notifications. Existing social notification controls remain separate.

Internal SQLite files for accounts, configuration metadata, job state and snapshots remain local; they are not additional configurable business databases.

## Runtime controls

The administrator page also manages log level/retention, database snapshot read timeout and maximum attempts (including the first attempt), and per-platform HTTP request inactivity timeouts. Database snapshot timeout changes rebuild the background connection pool before the next connection. Already-running operations retain their current limits. Existing overall download budgets and source rate limits remain enforced; a request inactivity timeout is not the maximum duration of an entire collection job.

Business workers apply log settings within five seconds without removing redaction or access-log filters. Log retention is enforced by normal log rotation, not an immediate destructive purge. The launcher keeps its startup log settings. Source status lists configuration and credential presence only; it explicitly says availability has not been checked and does not claim a live health test. Telegram, Feishu and social-login request limits are not changed by financial data-source timeout settings.
