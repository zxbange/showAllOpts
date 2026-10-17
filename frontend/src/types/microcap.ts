export interface MicrocapPoint {
  date: string
  micro_median: number
  all_median: number
  relative_ratio: number
  crowding_ratio: number
  amount_crowding_ratio: number
  pe_median: number
  pb_median: number
  avg_np_growth: number | null
  cap_pct: number
  relative_pct: number
  crowding_pct: number
  amount_crowding_pct: number
  score: number
  micro_index: number
  period_return: number
}

export interface MicrocapHistory {
  start_date: string | null
  end_date: string | null
  latest:
    | (MicrocapPoint & {
        micro_num: number
        sample_step: number
        sample_count: number
        universe_count: number
        profit_growth_count: number
        updated_at: string
      })
    | null
  summary: {
    observed_samples: number
    average_score: number | null
    peak_score: number | null
    low_score: number | null
    score_change: number | null
    index_change_pct: number | null
  }
  points: MicrocapPoint[]
}
