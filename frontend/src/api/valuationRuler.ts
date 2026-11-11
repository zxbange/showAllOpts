import { apiFetch } from './http'
import type { ValuationHistory, ValuationStatus } from '../types/valuationRuler'
export type ValuationAction = 'refresh' | 'sync-joinquant' | 'cancel'
async function request<T>(path: string, method: 'GET' | 'POST', signal?: AbortSignal): Promise<T> {
  const response = await apiFetch('/api/v1/valuation-ruler/' + path, {
    method,
    signal,
    cache: 'no-store',
    headers: { Accept: 'application/json' },
  })
  if (!response.ok) {
    const body = await response.json().catch(() => null)
    throw new Error(
      typeof body?.detail === 'string' ? body.detail : `估值数据请求失败（${response.status}）`,
    )
  }
  return response.json() as Promise<T>
}
export const getValuationHistory = (signal?: AbortSignal) =>
  request<ValuationHistory>('history', 'GET', signal)
export const getValuationStatus = (signal?: AbortSignal) =>
  request<ValuationStatus>('status', 'GET', signal)
// Never retry mutations automatically: a lost response may still have started a job.
export const updateValuation = (action: ValuationAction) =>
  request<{ started?: boolean; cancelled?: boolean }>(action, 'POST')
