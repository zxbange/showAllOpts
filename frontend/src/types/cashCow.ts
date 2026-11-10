export interface CashCowStatus {
  periods: [3, 5, 10]
  collection_years: 10
  run_id: string | null
  next_run_at?: string | null
  schedule_time?: string
  running: boolean
  cancelling: boolean
  stage: string
  processed: number
  total: number | null
  last_attempt_at: string | null
  last_success_at: string | null
  error: string | null
}
export interface CashCowYear {
  year: number
  dividend_per_share: number
  cash_dividend: number
  adjusted_profit: number
  net_profit: number
  operating_cashflow: number
  year_average_close: number
  price_date: string
  payout_ratio: number
  cashflow_coverage: number
  dividend_yield: number
}
export interface CashCowStock {
  code: string
  name: string
  exchange: string
  current_yield: number | null
  latest_price: number | null
  latest_price_date: string | null
  dividend_ttm_per_share: number | null
  average_yield: number
  payout_min: number
  payout_max: number
  coverage_min: number
  years: CashCowYear[]
}
export interface CashCowOverview {
  snapshot: {
    as_of: string
    updated_at: string
    previous_updated_at?: string | null
    previous_previous_updated_at?: string | null
    start_year: number
    end_year: number
    universe_count: number
    evaluated_count: number
    excluded_count: number
    insufficient_count: number
    matched_count: number
    source: string
  } | null
  items: CashCowStock[]
  status: CashCowStatus
}
