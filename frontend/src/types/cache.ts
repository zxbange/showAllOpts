export interface CacheModule {
  cancelling?: boolean
  id: string
  name: string
  ready: boolean
  updated_at: string | null
  last_attempt_at: string | null
  rows: number | null
  progress?: {
    status: string
    percent?: number
    stage?: string
    processed?: number
    elapsed_seconds?: number
  }
  sync_note?: string | null
  error: string | null
}
export interface CacheSchedule {
  daily_time: string
  timezone: string
  next_refresh_at?: string | null
}
export interface CacheStatus extends CacheSchedule {
  enabled: boolean
  run_id?: string | null
  cancelling?: boolean
  refreshing: boolean
  manual_enabled: boolean
  auto_refresh: boolean
  modules: CacheModule[]
}
