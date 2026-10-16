export interface Industry {
  code: string
  name: string
  momentum: number
  rank: number
  front_stock_count: number
  industry_stock_count: number
  breadth: number
  previous_momentum: number | null
  change: number | null
}

export interface Snapshot {
  industry_type: string
  trade_date: string | null
  previous_trade_date: string | null
  updated_at: string | null
  summary: {
    industry_count: number
    active_count: number
    average_momentum: number
    max_momentum: number
  }
  industries: Industry[]
}

export interface HistoryPoint {
  date: string
  momentum: number | null
  rank: number | null
  front_stock_count: number | null
  industry_stock_count: number | null
}

export interface IndustryHistory {
  code: string
  name: string
  industry_type: string
  start_date: string
  end_date: string
  summary: {
    latest_momentum: number | null
    latest_date: string | null
    peak_momentum: number | null
    peak_date: string | null
    average_momentum: number | null
    change: number | null
    observed_days: number
    missing_days: number
  }
  points: HistoryPoint[]
}

export interface Module {
  id: string
  name: string
  description: string
  path: string
  status: string
}
