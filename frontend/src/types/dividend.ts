export interface DividendItem {
  method: string
  code: string
  trade_date: string
  rank: number
  name: string
  industry_code: string | null
  industry_name: string | null
  industry_level: string | null
  dividend_yield_1y: number
  dividend_ratio_3y: number
  dividend_cash_1y: number
  dividend_cash_3y: number
  pe_ttm: number
  pb: number | null
  market_cap: number
  close: number
  revenue_growth: number | null
  profit_growth: number | null
  adjusted_roe: number | null
  peg: number | null
  report_period: string | null
  publication_date: string | null
}
export interface DividendGroup {
  id: string
  name: string
  ranking_field: string
  candidates: number
  items: DividendItem[]
}
export interface DividendOverview {
  snapshot: {
    trade_date: string
    updated_at: string
    rule_version: string
    universe_count: number
    eligible_count: number
    classic_candidates: number
    value_candidates: number
    classic_count: number
    value_count: number
  } | null
  groups: DividendGroup[]
  overlap_codes: string[]
}
