export interface EarningsStock {
  report_period: string
  code: string
  name: string
  industry_code: string | null
  industry_name: string | null
  industry_level: string | null
  signal_date: string
  announcement_date: string
  signal_source: string
  profit_basis: string
  profit_yoy: number
  net_profit: number | null
  open_gap_pct: number
  intraday_pct: number
  market_cap: number | null
  circulating_market_cap: number | null
  pe_ttm: number | null
  pb: number | null
  valuation_date: string
  analysis: string
  analysis_basis: string
}
export interface EarningsEvent {
  code: string
  kind: string
  publication_date: string
  report_period: string
  net_profit: number | null
  profit_yoy: number | null
  profit_yoy_max: number | null
  profit_basis: string
  eligible: boolean
  reason: string | null
  reason_field: string | null
  raw: Record<string, unknown>
  signal: { date: string; open_gap_pct: number; intraday_pct: number } | null
}
export interface EarningsDetail extends EarningsStock {
  events: EarningsEvent[]
  prices: {
    date: string
    open: number | null
    close: number | null
    high: number | null
    low: number | null
  }[]
}
export interface EarningsOverview {
  target_period: string
  periods: string[]
  run: null | {
    report_period: string
    as_of_date: string
    updated_at: string
    universe_count: number
    matched_count: number
    rule_version: string
  }
  industries: { code: string; name: string }[]
  total: number
  page: number
  page_size: number
  items: EarningsStock[]
}
