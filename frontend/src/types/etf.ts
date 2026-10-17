export interface ETFPoint {
  date: string
  shares: string
  change_pct: number | null
}
export interface ETFFund {
  code: string
  name: string
  date: string | null
  shares: string | null
  previous_date: string | null
  shares_change: string | null
  daily_change_pct: number | null
  updated_at: string | null
  lagging: boolean
  points: ETFPoint[]
}
export interface ETFHistory {
  group: string
  group_name: string
  start_date: string | null
  end_date: string | null
  baseline_date: string | null
  observed_days: number
  available_funds: number
  funds: ETFFund[]
}
