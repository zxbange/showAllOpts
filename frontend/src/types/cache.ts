export interface CacheModule {
  id: string
  name: string
  ready: boolean
  updated_at: string | null
  last_attempt_at: string | null
  rows: number | null
  progress?: { status: string; stage?: string; processed?: number; elapsed_seconds?: number }
  error: string | null
}
export interface CacheStatus {
  enabled: boolean
  refreshing: boolean
  manual_enabled: boolean
  auto_refresh: boolean
  daily_time: string
  timezone: string
  modules: CacheModule[]
}
