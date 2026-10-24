export interface SentimentPoint {
  date: string
  ratio: number
  above_count: number
  eligible_count: number
  universe_count: number
  state: 'cold' | 'normal' | 'hot'
}
export interface SentimentHistory {
  start_date: string | null
  end_date: string | null
  latest: (SentimentPoint & { updated_at: string }) | null
  summary: {
    observed_days: number
    cold_days: number
    hot_days: number
    low_ratio: number | null
    peak_ratio: number | null
    average_ratio: number | null
    change_pp: number | null
  }
  points: SentimentPoint[]
}
