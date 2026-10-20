import type { DividendOverview } from '../types/dividend'
import type { BreadthHistory } from '../types/breadth'
import type { EarningsOverview, EarningsDetail } from '../types/earnings'
import type { ETFHistory } from '../types/etf'
import type { MicrocapHistory } from '../types/microcap'
import type { CrowdingHistory } from '../types/crowding'
import type { IndustryHistory, Module, Snapshot } from '../types/momentum'

const API_BASE = '/api/v1'

async function request<T>(path: string, signal?: AbortSignal): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    signal,
    headers: { Accept: 'application/json' },
  })
  if (!response.ok) {
    const body = await response.json().catch(() => null)
    throw new Error(
      typeof body?.detail === 'string'
        ? body.detail
        : `数据请求失败（${response.status}），请稍后重试。`,
    )
  }
  return response.json() as Promise<T>
}

export const getModules = (signal?: AbortSignal) =>
  request<{ modules: Module[] }>('/modules', signal)
export const getSnapshot = (signal?: AbortSignal) =>
  request<Snapshot>('/industry-momentum/latest', signal)
export function getHistory(
  code: string,
  days: number,
  endDate: string | null,
  signal?: AbortSignal,
) {
  const query = new URLSearchParams({ days: String(days) })
  if (endDate) query.set('end_date', endDate)
  return request<IndustryHistory>(
    `/industry-momentum/industries/${encodeURIComponent(code)}/history?${query}`,
    signal,
  )
}

export const getCrowdingHistory = (days: number, signal?: AbortSignal) =>
  request<CrowdingHistory>(`/market-crowding/history?days=${days}`, signal)

export const getMicrocapHistory = (days: number, signal?: AbortSignal) =>
  request<MicrocapHistory>(`/microcap-temperature/history?days=${days}`, signal)

export const getETFHistory = (group: string, days: number, signal?: AbortSignal) =>
  request<ETFHistory>(
    `/etf-shares/history?${new URLSearchParams({ group, days: String(days) })}`,
    signal,
  )

export const getEarningsOverview = (query: string, signal?: AbortSignal) =>
  request<EarningsOverview>('/earnings-gap/stocks?' + query, signal)
export const getEarningsDetail = (code: string, period: string, signal?: AbortSignal) =>
  request<EarningsDetail>(
    '/earnings-gap/stocks/' + encodeURIComponent(code) + '?' + new URLSearchParams({ period }),
    signal,
  )

export const getBreadthHistory = (days: number, signal?: AbortSignal) =>
  request<BreadthHistory>(`/microcap-breadth/history?days=${days}`, signal)

export const getDividendOverview = (signal?: AbortSignal) =>
  request<DividendOverview>('/dividend-selection/latest', signal)

export const getCacheStatus = (signal?: AbortSignal) =>
  request<import('../types/cache').CacheStatus>('/cache/status', signal)

export async function refreshCache(module: string | null) {
  const response = await fetch(`${API_BASE}/cache/refresh`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ module }),
  })
  if (!response.ok) {
    const body = await response.json().catch(() => null)
    throw new Error(typeof body?.detail === 'string' ? body.detail : '无法启动同步，请稍后重试。')
  }
}
