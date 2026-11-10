import type { CashCowPeriod } from '../lib/cashCow'
import { apiFetch } from './http'
import type { CashCowOverview, CashCowStatus } from '../types/cashCow'

async function request<T>(path: string, signal?: AbortSignal, method = 'GET'): Promise<T> {
  const response = await apiFetch('/api/v1/cash-cow/' + path, { method, signal })
  if (!response.ok) {
    const body = await response.json().catch(() => null)
    throw new Error(
      typeof body?.detail === 'string'
        ? body.detail
        : `请求失败（${response.status}），请稍后重试。`,
    )
  }
  return response.json() as Promise<T>
}
export const getCashCowLatest = (signal?: AbortSignal, years: CashCowPeriod = 10) =>
  request<CashCowOverview>(`latest?years=${years}`, signal)
export const getCashCowStatus = (signal?: AbortSignal, years: CashCowPeriod = 10) =>
  request<CashCowStatus>(`status?years=${years}`, signal)
export const updateCashCow = (
  action: 'refresh' | 'cancel',
  signal?: AbortSignal,
  years: CashCowPeriod = 10,
) => request<{ started?: boolean; cancelled?: boolean }>(`${action}?years=${years}`, signal, 'POST')
