import { apiFetch } from './http'

export interface CollectionOutcome {
  outcome: 'success' | 'failed' | 'partial' | 'cancelled' | 'interrupted' | 'unknown'
  finished_at: string | null
  trigger: 'manual' | 'scheduled' | null
  kind?: 'collection' | 'database_sync'
  running?: boolean
}
export interface CollectionOutcomes {
  items: Record<string, CollectionOutcome>
}
export async function getCollectionOutcomes(signal?: AbortSignal): Promise<CollectionOutcomes> {
  const response = await apiFetch('/api/v1/collection-outcomes', { signal, cache: 'no-store' })
  if (!response.ok) throw new Error('采集状态暂不可用')
  const value = await response.json()
  if (!value || !value.items || typeof value.items !== 'object' || Array.isArray(value.items))
    throw new Error('采集状态格式无效')
  return value
}
