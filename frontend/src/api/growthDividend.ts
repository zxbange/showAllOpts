import { apiFetch } from './http'
import type { GrowthDividendHistory, GrowthDividendStatus } from '../types/growthDividend'

async function request<T>(path: string, signal?: AbortSignal, method = 'GET'): Promise<T> {
  const response = await apiFetch('/api/v1/growth-dividend/' + path, {
    method,
    signal,
    cache: 'no-store',
  })
  if (!response.ok) {
    const result = await response.json().catch(() => null)
    throw new Error(
      typeof result?.detail === 'string'
        ? result.detail
        : `请求失败（${response.status}），请稍后重试。`,
    )
  }
  return response.json() as Promise<T>
}

export const getGrowthDividendHistory = (signal?: AbortSignal) =>
  request<GrowthDividendHistory>('history', signal)
export const getGrowthDividendStatus = (signal?: AbortSignal) =>
  request<GrowthDividendStatus>('status', signal)
export const updateGrowthDividend = (
  action: 'refresh' | 'cancel' | 'sync-joinquant',
  signal?: AbortSignal,
) => request<{ started?: boolean; cancelled?: boolean }>(action, signal, 'POST')
