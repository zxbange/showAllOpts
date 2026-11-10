export interface GrowthDividendPoint {
  date: string
  chinext: number | null
  dividend: number | null
  star50: number | null
  chinext_dividend_ratio: number | null
  star50_dividend_ratio: number | null
}

export interface GrowthDividendStatus {
  running: boolean
  cancelling?: boolean
  stage: string
  processed: number
  total: number | null
  percent: number | null
  last_attempt_at: string | null
  last_success_at: string | null
  error: string | null
  pending_dates: number
  pending_points: number
  daily_time: string
  timezone: string
  next_run_at: string | null
}

export interface GrowthDividendHistory {
  start_date: string | null
  end_date: string | null
  latest: GrowthDividendPoint | null
  points: GrowthDividendPoint[]
  status: GrowthDividendStatus
}
