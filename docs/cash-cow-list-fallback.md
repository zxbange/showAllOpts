# Cash-cow stock-list availability

The production multi-source cash-cow screen uses AShareHub's listed directory
and **directly** reads Eastmoney's selector, not the legacy clist-first flow:
`https://datacenter.eastmoney.com/stock/selection/api/data/get`, report
`RPTA_APP_STOCKSELECT`. The selector is the screened universe after reconciliation.
Shared symbols must have the same listing date. A one-sided symbol with a valid
listing date is tolerated only if it is demonstrably under three years old;
unverified age-eligible coverage blocks publication.

The underlying Eastmoney-only adapter still implements its historical
`82.push2.../api/qt/clist/get` → selector fallback after bounded transport/5xx
failures, with no mixing of partial primary pages. That fallback is not the
production combined screen's source order.

It verifies the ordered six-digit identities, exchange-specific A-share code
families, names, listing dates, quote dates, page number and continuation flag.
Every continued page must be full; the last page must be nonempty and terminal.
All three markets must be present. The service has no total-count field: this
checks its pagination contract, not an independent exchange census. An upstream
silently omitted security cannot be ruled out by these checks alone.

`MAX_TRADE_DATE` is a quote date, not a membership effective date. Future listing
or quote dates, inconsistent quote dates, or quotes older than seven calendar
days fail closed. This intentionally may reject otherwise legitimate responses
during long exchange holidays; it preserves the previous successful result
rather than treating old quotes as proof of current membership. It does not
claim intraday or same-day newly listed stock coverage.

Historical cloud sample (2026-11-16, not a production run): two pages (5,000 + 579), all 5,579 distinct codes,
2,319 Shanghai / 2,905 Shenzhen / 355 Beijing, including 689009. The exact code
set matched the independently fetched Eastmoney dated report for 2026-11-13.
Quote date was 2026-11-13. The new parser accepted both real pages with listing
dates. This is not proof of production-network availability or a completed
cash-cow financial calculation.
