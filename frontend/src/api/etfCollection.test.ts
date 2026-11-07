import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import {
  getCollectionConfiguration,
  runCollection,
  saveCollectionConfiguration,
  testCollectionConfiguration,
} from './etfCollection'
import { setCsrf } from './http'
beforeEach(() => {
  setCsrf('etf-session')
  vi.stubGlobal('window', {
    location: { href: 'https://site.example/', origin: 'https://site.example' },
    dispatchEvent: vi.fn(),
  })
})
afterEach(() => vi.unstubAllGlobals())
it('keeps configuration uncached and uses abort signal', async () => {
  const fetcher = vi.fn().mockResolvedValue({ ok: true, json: async () => ({ revision: 2 }) })
  vi.stubGlobal('fetch', fetcher)
  const controller = new AbortController()
  expect(await getCollectionConfiguration(controller.signal)).toEqual({ revision: 2 })
  expect(fetcher.mock.calls[0][1]).toMatchObject({
    cache: 'no-store',
    method: 'GET',
    signal: controller.signal,
  })
})
it('uses CSRF for all mutations and never invents a replacement password', async () => {
  const fetcher = vi.fn().mockResolvedValue({ ok: true, json: async () => ({ ok: true }) })
  vi.stubGlobal('fetch', fetcher)
  const body = {
    revision: 1,
    username: 'writer',
    host: 'db.example',
    port: 3306,
    database: 'market',
    enabled: false,
    lookback_days: 400,
    batch_days: 4,
    schedule: ['17:00'],
    db_tls: true,
  }
  await saveCollectionConfiguration(body)
  await testCollectionConfiguration(body)
  await runCollection('1234567890abcdef')
  for (const [, options] of fetcher.mock.calls) {
    expect(options.method).toBe('POST')
    expect(options.headers.get('X-CSRF-Token')).toBe('etf-session')
    expect(options.headers.get('X-App-Client')).toBe('browser')
    expect(options.credentials).toBe('same-origin')
  }
  expect(JSON.parse(fetcher.mock.calls[0][1].body)).not.toHaveProperty('password')
  expect(JSON.parse(fetcher.mock.calls[2][1].body)).toEqual({ request_id: '1234567890abcdef' })
})
it('returns sanitized backend errors without exposing submitted data', async () => {
  vi.stubGlobal(
    'fetch',
    vi.fn().mockResolvedValue({
      ok: false,
      status: 409,
      json: async () => ({ detail: '配置已经变更，请重新读取' }),
    }),
  )
  await expect(runCollection('1234567890abcdef')).rejects.toThrow('配置已经变更，请重新读取')
})

it('sends the explicit exhausted-retry choice with the idempotency key', async () => {
  const fetcher = vi.fn().mockResolvedValue({ ok: true, json: async () => ({ status: 'queued' }) })
  vi.stubGlobal('fetch', fetcher)
  await runCollection('1234567890abcdef', true)
  expect(JSON.parse(fetcher.mock.calls[0][1].body)).toEqual({
    request_id: '1234567890abcdef',
    retry_exhausted: true,
  })
})
