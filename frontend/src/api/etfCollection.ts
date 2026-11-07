import { apiFetch } from './http'
import type { ETFCollectionConfiguration, ETFManualRun } from '../types/etf'
export type CollectionInput = Pick<
  ETFCollectionConfiguration,
  | 'revision'
  | 'host'
  | 'port'
  | 'database'
  | 'username'
  | 'enabled'
  | 'lookback_days'
  | 'batch_days'
  | 'schedule'
  | 'db_tls'
> & { password?: string; db_transport_acknowledged?: boolean }
async function call<T>(path: string, body?: unknown, signal?: AbortSignal): Promise<T> {
  const response = await apiFetch('/api/v1/etf-shares/' + path, {
    method: body === undefined ? 'GET' : 'POST',
    signal,
    cache: 'no-store',
    headers: { 'Content-Type': 'application/json' },
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  })
  const result = await response.json().catch(() => null)
  if (!response.ok)
    throw new Error(
      typeof result?.detail === 'string'
        ? result.detail
        : `操作未完成（${response.status}），请重试。`,
    )
  return result as T
}
export const getCollectionConfiguration = (signal?: AbortSignal) =>
  call<ETFCollectionConfiguration>('collection-configuration', undefined, signal)
export const saveCollectionConfiguration = (body: CollectionInput) =>
  call<ETFCollectionConfiguration>('collection-configuration', body)
export const testCollectionConfiguration = (body: CollectionInput) =>
  call<{ message: string; permissions_verified: boolean }>('collection-test', body)
export const runCollection = (request_id: string, retryExhausted = false) =>
  call<ETFManualRun>('collection-run', {
    request_id,
    ...(retryExhausted ? { retry_exhausted: true } : {}),
  })
