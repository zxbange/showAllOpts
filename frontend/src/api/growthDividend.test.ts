import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import {
  getGrowthDividendHistory,
  getGrowthDividendStatus,
  updateGrowthDividend,
} from './growthDividend'
import { setCsrf } from './http'

beforeEach(() =>
  vi.stubGlobal('window', {
    location: { href: 'https://test.example/', origin: 'https://test.example' },
  }),
)
afterEach(() => {
  vi.unstubAllGlobals()
  setCsrf('')
})

it('fetches the full history without a five-year API filter, forwarding cancellation', async () => {
  const fetcher = vi.fn().mockResolvedValue({ ok: true, json: async () => ({}) })
  vi.stubGlobal('fetch', fetcher)
  const controller = new AbortController()
  await getGrowthDividendHistory(controller.signal)
  await getGrowthDividendStatus(controller.signal)
  expect(fetcher.mock.calls.map(([url, options]) => [url, options.method, options.signal])).toEqual(
    [
      ['/api/v1/growth-dividend/history', 'GET', controller.signal],
      ['/api/v1/growth-dividend/status', 'GET', controller.signal],
    ],
  )
})

it('uses the existing CSRF session for mutations and does not retry accepted commands', async () => {
  setCsrf('csrf-test')
  const fetcher = vi.fn().mockResolvedValue({ ok: true, json: async () => ({ started: true }) })
  vi.stubGlobal('fetch', fetcher)
  await updateGrowthDividend('refresh')
  await updateGrowthDividend('cancel')
  expect(
    fetcher.mock.calls.map(([url, options]) => [
      url,
      options.method,
      options.headers.get('X-CSRF-Token'),
    ]),
  ).toEqual([
    ['/api/v1/growth-dividend/refresh', 'POST', 'csrf-test'],
    ['/api/v1/growth-dividend/cancel', 'POST', 'csrf-test'],
  ])
  fetcher.mockResolvedValue({
    ok: false,
    status: 503,
    json: async () => ({ detail: '行情接口未就绪' }),
  })
  await expect(updateGrowthDividend('refresh')).rejects.toThrow('行情接口未就绪')
  expect(fetcher).toHaveBeenCalledTimes(3)
})

it('sends JoinQuant history sync once with the current CSRF token', async () => {
  setCsrf('sync-token')
  const fetcher = vi.fn().mockResolvedValue({ ok: true, json: async () => ({ started: false }) })
  vi.stubGlobal('fetch', fetcher)
  expect(await updateGrowthDividend('sync-joinquant')).toEqual({ started: false })
  expect(fetcher).toHaveBeenCalledTimes(1)
  expect(fetcher.mock.calls[0][0]).toBe('/api/v1/growth-dividend/sync-joinquant')
  expect(fetcher.mock.calls[0][1].headers.get('X-CSRF-Token')).toBe('sync-token')
})
