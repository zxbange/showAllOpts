# Tushare credential configuration

Administrators can edit **Tushare Token** under **系统配置 → 接口凭证**（数据接口与消息凭证）. The default is read from `TUSHARE_TOKEN` in the active application configuration file. Saving an edit writes back through the same atomic, permission-preserving configuration-file mechanism as AShareHub.

The browser receives only `token_set`, never the saved token. Leaving the field untouched retains it. Clear takes effect only after saving. The token is stored in the active private application configuration, which must be protected as a secret; logs do not include the token. An environment-variable override takes precedence and blocks conflicting page edits, rather than silently saving an ineffective value.

The current 奶牛股 collector uses Tushare Pro `daily` for historical closing prices; it also requires AShareHub credentials. Saving the token does not start collection or grant endpoint permission. An empty, limited or unauthorized daily data response can stop the collection; existing results are retained when the run fails. Never commit a real token to source control.

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
