export interface MarginPoint {
  date: string
  ratio: number
  source_url: string
}

export interface MarginStatus {
  running: boolean
  cancelling?: boolean
  stage: string
  processed: number
  total: number | null
  percent: number | null
  last_attempt_at: string | null
  last_success_at: string | null
  error: string | null
  source_corrections?: {
    report_date: string
    observation_date: string
    source_url: string
    content_sha256: string
    reason: string
  }[]
  daily_time: string
  timezone: string
  next_run_at: string | null
}

export interface MarginHistory {
  start_date: string | null
  end_date: string | null
  latest: (MarginPoint & { updated_at: string }) | null
  summary: {
    observed_days: number
    change_pp: number | null
    previous_date?: string | null
    percentile: number | null
    below_count: number
    low_ratio: number | null
    low_date: string | null
    peak_ratio: number | null
    peak_date: string | null
    distance_pp: number | null
  }
  points: MarginPoint[]
  status: MarginStatus
}
