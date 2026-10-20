export type ReportGroup = 'annual' | 'q1' | 'q2' | 'q3'
export interface GrowthReport {
  report_period: string
  publication_date: string
  net_profit: number
  eps: number
  profit_growth: number | null
  eps_growth: number | null
}
export interface GrowthItem {
  code: string
  name: string
  rank: number
  industry_name: string | null
  industry_level: string | null
  pe_ttm: number | null
  pb: number | null
  market_cap: number | null
  annual_profit_growth: number
  reports: Record<ReportGroup, GrowthReport[]>
}
export interface GrowthOverview {
  snapshot: {
    as_of_date: string
    valuation_date: string
    updated_at: string
    universe_count: number
    evaluated_count: number
    matched_count: number
    rule_version: string
  } | null
  items: GrowthItem[]
}
