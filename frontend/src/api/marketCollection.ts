import { apiFetch } from './http'
import type {
  MarketCollectionConfiguration,
  MarketCollectionInput,
  MarketCollectionRun,
  MarketCollectionStatus,
  MarketCollectionTest,
} from '../types/marketCollection'

export class MarketCollectionRequestError extends Error {
  readonly status: number
  readonly code?: string
  readonly activeRun?: MarketCollectionRun

  constructor(status: number, detail: unknown) {
    const structured =
      detail && typeof detail === 'object' ? (detail as Record<string, unknown>) : null
    super(
      typeof detail === 'string'
        ? detail
        : typeof structured?.message === 'string'
          ? structured.message
          : `操作未完成（${status}），请重试。`,
    )
    this.name = 'MarketCollectionRequestError'
    this.status = status
    this.code = typeof structured?.code === 'string' ? structured.code : undefined
    const active = structured?.active_run as Partial<MarketCollectionRun> | null | undefined
    if (
      active &&
      typeof active.id === 'string' &&
      ['queued', 'running'].includes(active.status ?? '')
    )
      this.activeRun = active as MarketCollectionRun
  }
}

async function request<T>(path: string, body?: unknown, signal?: AbortSignal): Promise<T> {
  const response = await apiFetch('/api/v1/market-collection/' + path, {
    method: body === undefined ? 'GET' : 'POST',
    signal,
    cache: 'no-store',
    headers: { 'Content-Type': 'application/json' },
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  })
  const result = await response.json().catch(() => null)
  if (!response.ok) throw new MarketCollectionRequestError(response.status, result?.detail)
  return result as T
}

export const getMarketCollectionStatus = (signal?: AbortSignal) =>
  request<MarketCollectionStatus>('status', undefined, signal)
export const getMarketCollectionConfiguration = (signal?: AbortSignal) =>
  request<MarketCollectionConfiguration>('configuration', undefined, signal)
export const saveMarketCollectionConfiguration = (
  body: MarketCollectionInput,
  signal?: AbortSignal,
) => request<MarketCollectionConfiguration>('configuration', body, signal)
export const testMarketCollectionConfiguration = (
  body: MarketCollectionInput,
  signal?: AbortSignal,
) => request<MarketCollectionTest>('test', body, signal)
export async function runMarketCollection(request_id: string, signal?: AbortSignal) {
  const result = await request<MarketCollectionRun>('run', { request_id }, signal)
  // A proxy error page or truncated/mismatched response does not prove rejection.
  if (
    !result ||
    result.id !== request_id ||
    !['queued', 'running', 'complete', 'partial', 'paused', 'blocked', 'failed'].includes(
      result.status,
    )
  )
    throw new Error('暂时无法确认采集请求是否已接收。')
  return result
}
