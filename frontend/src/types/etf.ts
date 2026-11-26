import type { CollectionErrorDetails } from '../api/collectionHistory'
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
  updated_at?: string | null
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

export interface ETFCollectionCoverage {
  collection_scope?: 'latest_completed_session'
  manual_collection_scope?: 'known_trading_session_gaps'
  manual_gap_start?: string | null
  pending_count?: number
  blocked_count?: number
  retry_exhausted_count?: number
  history_complete?: boolean
}

export interface ETFCollectionRun {
  error_details?: CollectionErrorDetails | null
  publication_recovered_at?: string | null
  id: string
  trigger: 'manual' | 'scheduled'
  status: string
  error?: string | null
  requested_at: string
  finished_at: string | null
  slot: string | null
  attempt: number | null
  time_basis: 'completed_at' | 'legacy_request_time' | 'recovered_at'
}

export interface ETFCollectionStatus extends ETFCollectionCoverage {
  enabled?: boolean
  configured?: boolean
  cache_pending?: boolean
  cache_publication?: {
    status: 'pending' | 'publishing' | 'waiting_lock' | 'failed' | 'published' | 'direct_read'
    error_code?: string | null
    latest_share_date?: string | null
    latest_price_date?: string | null
    published_at?: string | null
    last_attempt_at?: string | null
    required_through?: string | null
  } | null
  manual?: ETFManualRun | null
  progress?: { phase: string; completed: number; total: number } | null
  lookback_days?: number
  batch_days?: number
  gap_count?: number
  gaps?: Array<{ kind: string; code: string; day: string; status: string; attempts: number }>
  status: string
  disabled_reason?: string | null
  worker_running?: boolean
  schedule?: string[]
  target_sessions?: number
  recent_runs?: ETFCollectionRun[]
  latest_result?: ETFCollectionRun | null
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
export interface ETFManualRun extends ETFCollectionCoverage {
  id: string
  status: string
  error?: string | null
  phase?: string
  completed?: number
  total?: number
}
