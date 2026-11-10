import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { setCsrf } from './http'
import {
  getMarketCollectionConfiguration,
  getMarketCollectionStatus,
  runMarketCollection,
  saveMarketCollectionConfiguration,
  testMarketCollectionConfiguration,
} from './marketCollection'

beforeEach(() => {
  setCsrf('market-session')
  vi.stubGlobal('window', {
    location: { href: 'https://site.example/', origin: 'https://site.example' },
    dispatchEvent: vi.fn(),
  })
})
afterEach(() => vi.unstubAllGlobals())

it('reads status and configuration without caching and supports cancellation', async () => {
  const fetcher = vi.fn().mockResolvedValue({ ok: true, json: async () => ({ revision: 1 }) })
  vi.stubGlobal('fetch', fetcher)
  const controller = new AbortController()
  await getMarketCollectionStatus(controller.signal)
  await getMarketCollectionConfiguration(controller.signal)
  expect(fetcher.mock.calls.map(([url]) => url)).toEqual([
    '/api/v1/market-collection/status',
    '/api/v1/market-collection/configuration',
  ])
  for (const [, options] of fetcher.mock.calls)
    expect(options).toMatchObject({ method: 'GET', cache: 'no-store', signal: controller.signal })
})

it('sends only the fixed-target configuration fields and preserves an omitted password', async () => {
  const fetcher = vi.fn().mockResolvedValue({ ok: true, json: async () => ({ ok: true }) })
  vi.stubGlobal('fetch', fetcher)
  const body = { revision: 3, username: 'writer', enabled: true }
  await saveMarketCollectionConfiguration(body)
  await testMarketCollectionConfiguration(body)
  for (const [, options] of fetcher.mock.calls) {
    expect(JSON.parse(options.body)).toEqual(body)
    expect(JSON.parse(options.body)).not.toHaveProperty('password')
    expect(JSON.parse(options.body)).not.toHaveProperty('host')
    expect(JSON.parse(options.body)).not.toHaveProperty('schedule')
    expect(options.headers.get('X-CSRF-Token')).toBe('market-session')
    expect(options.headers.get('X-App-Client')).toBe('browser')
    expect(options).toMatchObject({ method: 'POST', credentials: 'same-origin', cache: 'no-store' })
  }
})

it('passes an explicit replacement password and stable run id without inferring returned state', async () => {
  const fetcher = vi.fn().mockResolvedValue({ ok: true, json: async () => ({ status: 'queued' }) })
  vi.stubGlobal('fetch', fetcher)
  await saveMarketCollectionConfiguration({
    revision: 1,
    username: 'other',
    password: 'fixture-only',
    enabled: false,
  })
  const requestId = '7b83fe58-3c40-4ef8-8d2a-83e778dcb96a'
  await runMarketCollection(requestId)
  await runMarketCollection(requestId)
  expect(JSON.parse(fetcher.mock.calls[0][1].body).password).toBe('fixture-only')
  expect(fetcher.mock.calls.slice(1).map(([, options]) => JSON.parse(options.body))).toEqual([
    { request_id: requestId },
    { request_id: requestId },
  ])
  expect(fetcher.mock.calls[1][0]).toBe('/api/v1/market-collection/run')
  expect(fetcher.mock.calls[1][1].headers.get('X-CSRF-Token')).toBe('market-session')
})

it('keeps safe backend errors and uses a generic message for non-JSON failures', async () => {
  const fetcher = vi
    .fn()
    .mockResolvedValueOnce({
      ok: false,
      status: 409,
      json: async () => ({ detail: '配置已变更，请重新读取' }),
    })
    .mockResolvedValueOnce({
      ok: false,
      status: 503,
      json: async () => {
        throw new Error('unavailable')
      },
    })
  vi.stubGlobal('fetch', fetcher)
  await expect(runMarketCollection('same-id')).rejects.toThrow('配置已变更，请重新读取')
  await expect(getMarketCollectionStatus()).rejects.toThrow('操作未完成（503），请重试。')
})
