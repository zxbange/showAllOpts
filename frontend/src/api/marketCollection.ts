import { apiFetch } from './http'
import type {
  MarketCollectionConfiguration,
  MarketCollectionInput,
  MarketCollectionStatus,
  MarketCollectionTest,
} from '../types/marketCollection'

async function request<T>(path: string, body?: unknown, signal?: AbortSignal): Promise<T> {
  const response = await apiFetch('/api/v1/market-collection/' + path, {
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
export const runMarketCollection = (request_id: string, signal?: AbortSignal) =>
  request<unknown>('run', { request_id }, signal)
