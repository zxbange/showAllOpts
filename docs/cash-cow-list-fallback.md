# Cash-cow stock-list availability

The primary list remains Eastmoney `82.push2.../api/qt/clist/get`.
After bounded network/protocol errors or HTTP 5xx failures, only the stock list
can fall back to Eastmoney's stock-selection data service:
`https://datacenter.eastmoney.com/stock/selection/api/data/get`, report
`RPTA_APP_STOCKSELECT`. Financial, dividend and price computations do not change.
Authentication/rate denials, malformed responses and failed primary pagination
validation do not activate an alternate service.

The fallback starts from page 1; no partial primary-list records are combined.
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

2026-11-16 cloud verification: two pages (5,000 + 579), all 5,579 distinct codes,
2,319 Shanghai / 2,905 Shenzhen / 355 Beijing, including 689009. The exact code
set matched the independently fetched Eastmoney dated report for 2026-11-13.
Quote date was 2026-11-13. The new parser accepted both real pages with listing
dates. This is not proof of production-network availability or a completed
cash-cow financial calculation. A production refresh still needs verification.
