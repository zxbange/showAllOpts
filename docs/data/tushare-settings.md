# Tushare credential configuration

Administrators can edit **Tushare Token** under **System configuration → Data and messaging credentials**. The default is read from `TUSHARE_TOKEN` in the active application configuration file. Saving an edit writes back through the same atomic, permission-preserving configuration-file mechanism as AShareHub.

The browser receives only `token_set`, never the saved token. Leaving the field untouched retains it. Clear takes effect only after saving. The encrypted local configuration, startup logging and runtime logging follow the existing credential protections. An environment-variable override takes precedence and blocks conflicting page edits, rather than silently saving an ineffective value.

This adds credential management only. It does not activate a new collector, change data sources, or imply that the token has any particular Tushare permission. Never commit a real token to source control.

## Diagnosing unsuccessful saves

A `PUT /api/v1/system-configuration` HTTP 409 means the save was rejected,
not that a credential was successfully persisted. The API returns a safe reason
in `detail` and logs `System configuration rejected operation=... reason=...`.
The page keeps unsaved credentials and displays the safe error detail in a dismissible modal dialog. Common reasons
include a stale revision, process-environment overrides, invalid configuration,
file/directory permissions or collection locks.

An explicitly submitted integration credential is compared to the actual file,
not only its effective process value. If the process override already equals the
requested value but the file differs, the write is rejected with the override name
instead of silently succeeding. Remove the override and reload before saving.
Successful saves log the revision, actual file path and changed key names only;
credential values are never logged.

Saved credential inputs display a fixed `********` placeholder, never the secret
or its length. This mask is not an input value or draft and is never submitted.
Unedited fields retain their saved values; explicit edits and clear actions submit
only the intended replacement or empty string.
