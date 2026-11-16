export interface AnnualYield {
  fiscal_year: number
  yield_pct: number | null
}

export interface DividendItem {
  method: string
  code: string
  trade_date: string
  rank: number
  name: string
  industry_code: string | null
  industry_name: string | null
  industry_level: string | null
  dividend_yield_static: number
  dividend_yield_history?: AnnualYield[] | null
  dividend_cash_annual: number | null
  dividend_per_share?: number | null
  dividend_fiscal_year?: number | null
  pe_ttm: number
  pb: number | null
  market_cap: number
  close: number
}
export interface DividendGroup {
  id: string
  name: string
  ranking_field: string
  candidates: number | null
  items: DividendItem[]
}
export interface DividendOverview {
  requires_refresh?: boolean
  snapshot: {
    trade_date: string
    updated_at: string
    rule_version: string
    universe_count: number
    eligible_count: number
    value_candidates: number
    value_count: number
  } | null
  groups: DividendGroup[]
}
