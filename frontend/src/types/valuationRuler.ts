export interface ValuationPoint {
  date: string
  index_close: number | null
  mean_pe_ttm: number | null
  member_count: number | null
  positive_count: number | null
  loss_count: number | null
  source: string | null
  gap?: boolean
}
export interface ValuationHistory {
  points: ValuationPoint[]
  latest: ValuationPoint | null
  method: string
  benchmarks: {
    p20: number | null
    p80: number | null
    sample_days: number
    start_date: string | null
    end_date: string | null
  }
  coverage: {
    requested_start: string
    first_date: string | null
    last_date: string | null
    missing_count: number | null
    known_missing_count: number
    calendar_complete: boolean
  }
  gap_dates: string[]
  sources: string[]
}
export interface ValuationStatus {
  database_syncing?: boolean
  sync_message?: string | null
  status: 'idle' | 'running' | 'failed'
  phase: string | null
  target_date: string | null
  updated_at: string | null
  error: string | null
  completed_days: number
  failed_days: number
  schedule: string
  source_configured: boolean
  automatic: boolean
  recent_failures: {
    trade_date: string
    code: string
    reason: string
    attempts: number
    updated_at: string
  }[]
  cancelled: boolean
  comparison_status: 'compared' | 'not_compared'
  comparison: null | {
    date: string
    published_source: string
    candidate_source: string
    compared_at: string
    match: boolean
    added_members: string[]
    removed_members: string[]
    changed_member_count: number
    max_abs_pe_delta: string
    mean_pe_delta: string
    index_close_delta: string
    differences: Record<string, unknown>
  }
  comparison_error: string | null
  comparison_attempted_date: string | null
  joinquant_imported: number | null
  joinquant_conflicts: number
  last_success_at: string | null
}
