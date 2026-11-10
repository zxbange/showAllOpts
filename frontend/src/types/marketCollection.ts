export interface MarketPointSource {
  source?: 'joinquant' | 'sina'
  // New daily records follow the original JoinQuant qfq/eligible methodology.
  // Explicit older bfq/universe metadata remains readable without relabeling its values.
  price_basis?: 'qfq' | 'bfq'
  denominator?: 'eligible' | 'universe'
}

export interface MarketCollectionStatus {
  enabled: boolean
  configured: boolean
  ready?: boolean
  worker_disabled?: boolean
  status: string
  timezone: string
  schedule: string[]
  trade_date: string | null
  run_id?: string | null
  total: number
  completed: number
  pending: number
  above_count: number
  valid_count?: number
  insufficient_history_count?: number
  no_trade_count?: number
  heat_status: string
  crowding_status: string
  cache_pending: boolean
  updated_at: string | null
  error: string | null
  progress?: { phase: string; completed: number; total: number; pending?: number } | null
}

export interface MarketCollectionConfiguration {
  revision: number
  username: string
  password_set: boolean
  host: string
  port: number
  database: string
  enabled: boolean
  schedule: string[]
  source: 'page' | 'environment' | 'etf' | 'none'
  transport_secure: boolean
  target_changed?: boolean
  https_required?: boolean
}

export type MarketCollectionInput = Pick<
  MarketCollectionConfiguration,
  'revision' | 'username' | 'enabled'
> & { password?: string }

export interface MarketCollectionTest {
  ok: boolean
  permissions_verified: boolean
  message: string
}
