export interface BreadthPeriod {
  period: number
  high_count: number
  low_count: number
  high_ratio: number
  low_ratio: number
  spread_pp: number
  dominant_days: number
  observed_days: number
  dominance_pct: number
}
export interface BreadthPoint {
  date: string
  sample_count: number
  periods: BreadthPeriod[]
  strength: number
  sentiment: number
  change_6d: number | null
  change_12d: number | null
  market_state: string
}
export interface BreadthHistory {
  series_id: string
  start_date: string | null
  end_date: string | null
  latest: (BreadthPoint & { updated_at: string }) | null
  observed_days: number
  points: BreadthPoint[]
}
