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
export interface ETFMarketValue {
  date: string
  market_value: string | null
  available_funds: number
  expected_funds: number
  excluded_funds?: string[]
  quality_flags?: string[]
}
export interface ETFHistory {
  market_values?: ETFMarketValue[]
  group_options?: Array<{ id: string; name: string }>
  group: string
  group_name: string
  start_date: string | null
  end_date: string | null
  baseline_date: string | null
  observed_days: number
  available_funds: number
  funds: ETFFund[]
}

export interface ETFCollectionStatus {
  enabled?: boolean
  configured?: boolean
  cache_pending?: boolean
  manual?: ETFManualRun | null
  progress?: { phase: string; completed: number; total: number } | null
  lookback_days?: number
  batch_days?: number
  retry_exhausted_count?: number
  gap_count?: number
  gaps?: Array<{ kind: string; code: string; day: string; status: string; attempts: number }>
  status: string
  disabled_reason?: string | null
  worker_running?: boolean
  schedule?: string[]
  target_sessions?: number
  runs?: Array<{ slot: string; status: string; error?: string | null }>
}

export interface ETFCollectionConfiguration {
  revision: number
  host: string
  port: number
  database: string
  username: string
  password_set: boolean
  target_changed?: boolean
  schedule_migrated?: boolean
  enabled: boolean
  lookback_days: number
  batch_days: number
  schedule: string[]
  source: 'page' | 'environment' | 'none'
  transport_secure: boolean
  db_local?: boolean
}
export interface ETFManualRun {
  id: string
  status: string
  error?: string | null
  phase?: string
  completed?: number
  total?: number
}
