export interface HeatPoint {
  date: string
  code: string
  name: string
  score: number | null
  close: number | null
  annual_return: number | null
  r2: number | null
  reference_pass: boolean | null
  details: {
    status: string
    reason?: string
    checks?: Record<string, boolean | null>
    volume_ratio?: number | null
  }
}
export interface HeatFund {
  code: string
  name: string
  category: string
  target_name: string
  size_yi: string | number
  size_date: string
  size_source: string
  fund_type: string
}
export interface HeatItem extends HeatPoint {
  rank: number | null
  rank_change: number | null
  fund: HeatFund
}
export interface HeatSnapshot {
  trade_date: string | null
  updated_at: string | null
  previous_date: string | null
  items: HeatItem[]
}
export interface HeatHistory {
  code: string
  name: string
  end_date: string
  fund: HeatFund | null
  points: HeatPoint[]
}
