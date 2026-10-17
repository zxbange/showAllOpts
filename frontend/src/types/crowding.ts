export interface CrowdingHistory {
  top_pct: number
  start_date: string | null
  end_date: string | null
  latest: {
    date: string
    ratio: number
    stock_count: number
    top_stock_count: number
    top_money: number
    total_money: number
    updated_at: string
  } | null
  summary: {
    observed_days: number
    average_ratio: number | null
    peak_ratio: number | null
    low_ratio: number | null
    change_pp: number | null
  }
  points: { date: string; ratio: number; smoothed_ratio: number }[]
}
