import { dataUpdateInstant } from '../lib/dataUpdatedAt'
import { apiFetch } from './http'
import type { CollectionOutcome } from './collectionOutcomes'

export interface CollectionErrorDetails {
  code: string
  message: string
  stage?: string
  mysql_errno?: number
  sqlite_errno?: number
  http_status?: number
  trade_date?: string
  start_date?: string
  end_date?: string
}

export interface CollectionHistoryEntry extends CollectionOutcome {
  error_details?: CollectionErrorDetails | null
  source_name?: string
  run_key: string
}
export interface CollectionHistoryResponse {
  items: CollectionHistoryEntry[]
}
export async function getCollectionHistory(
  moduleKey: string,
  signal?: AbortSignal,
): Promise<CollectionHistoryResponse> {
  const query = new URLSearchParams({ module: moduleKey, limit: '5' })
  const response = await apiFetch(`/api/v1/collection-outcomes/history?${query}`, {
    signal,
    cache: 'no-store',
  })
  if (!response.ok) throw new Error('暂时无法读取最新采集记录')
  const value = await response.json()
  if (!value || !Array.isArray(value.items)) throw new Error('采集记录格式无效')
  return { items: value.items.slice(0, 5) }
}

/** The feed has two sources, but its footer still shows five records in total. */
export async function getPageCollectionHistory(
  moduleKey: string,
  signal?: AbortSignal,
): Promise<CollectionHistoryResponse> {
  if (moduleKey !== 'social-feed') return getCollectionHistory(moduleKey, signal)
  const sources = [
    ['social-weibo', '微博'],
    ['social-xueqiu', '雪球'],
  ]
  // Do not report one available source as the complete cross-platform history.
  const results = await Promise.all(
    sources.map(async ([key, name]) => {
      const result = await getCollectionHistory(key, signal)
      return result.items.map((item) => ({
        ...item,
        source_name: name,
        run_key: `${key}:${item.run_key}`,
      }))
    }),
  )
  return {
    items: results
      .flat()
      .sort(
        (a, b) =>
          (dataUpdateInstant(b.finished_at) ?? -Infinity) -
          (dataUpdateInstant(a.finished_at) ?? -Infinity),
      )
      .slice(0, 5),
  }
}
