import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { getValuationHistory, getValuationStatus, updateValuation } from './valuationRuler'
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
it('loads unfiltered full history and cancellable fresh status', async () => {
  const fetcher = vi.fn().mockResolvedValue({ ok: true, json: async () => ({}) })
  vi.stubGlobal('fetch', fetcher)
  const controller = new AbortController()
  await getValuationHistory(controller.signal)
  await getValuationStatus(controller.signal)
  expect(
    fetcher.mock.calls.map(([url, options]) => [
      url,
      options.method,
      options.signal,
      options.cache,
    ]),
  ).toEqual([
    ['/api/v1/valuation-ruler/history', 'GET', controller.signal, 'no-store'],
    ['/api/v1/valuation-ruler/status', 'GET', controller.signal, 'no-store'],
  ])
})
it('sends CSRF-protected mutations exactly once without automatic retry', async () => {
  setCsrf('csrf-test')
  const fetcher = vi.fn().mockResolvedValue({ ok: true, json: async () => ({ started: true }) })
  vi.stubGlobal('fetch', fetcher)
  for (const action of ['refresh', 'sync-joinquant', 'cancel'] as const)
    await updateValuation(action)
  expect(
    fetcher.mock.calls.map(([url, options]) => [
      url,
      options.method,
      options.headers.get('X-CSRF-Token'),
    ]),
  ).toEqual([
    ['/api/v1/valuation-ruler/refresh', 'POST', 'csrf-test'],
    ['/api/v1/valuation-ruler/sync-joinquant', 'POST', 'csrf-test'],
    ['/api/v1/valuation-ruler/cancel', 'POST', 'csrf-test'],
  ])
  fetcher.mockRejectedValue(new Error('connection lost'))
  await expect(updateValuation('refresh')).rejects.toThrow('connection lost')
  expect(fetcher).toHaveBeenCalledTimes(4)
})
it('preserves server validation explanations', async () => {
  vi.stubGlobal(
    'fetch',
    vi.fn().mockResolvedValue({
      ok: false,
      status: 409,
      json: async () => ({ detail: '已有采集运行中' }),
    }),
  )
  await expect(updateValuation('refresh')).rejects.toThrow('已有采集运行中')
})
