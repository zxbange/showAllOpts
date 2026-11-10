import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { getCashCowLatest, getCashCowStatus, updateCashCow } from './cashCow'
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
it('reads saved results and status without triggering collection, forwarding cancellation', async () => {
  const fetcher = vi.fn().mockResolvedValue({ ok: true, json: async () => ({}) })
  vi.stubGlobal('fetch', fetcher)
  const controller = new AbortController()
  await getCashCowLatest(controller.signal)
  await getCashCowStatus(controller.signal)
  expect(fetcher.mock.calls.map(([url, options]) => [url, options.method, options.signal])).toEqual(
    [
      ['/api/v1/cash-cow/latest?years=10', 'GET', controller.signal],
      ['/api/v1/cash-cow/status?years=10', 'GET', controller.signal],
    ],
  )
})
it('uses authenticated single POSTs for start and cancel', async () => {
  setCsrf('csrf-test')
  const fetcher = vi.fn().mockResolvedValue({ ok: true, json: async () => ({ started: true }) })
  vi.stubGlobal('fetch', fetcher)
  await updateCashCow('refresh')
  await updateCashCow('cancel')
  expect(
    fetcher.mock.calls.map(([url, options]) => [
      url,
      options.method,
      options.headers.get('X-CSRF-Token'),
    ]),
  ).toEqual([
    ['/api/v1/cash-cow/refresh', 'POST', 'csrf-test'],
    ['/api/v1/cash-cow/cancel', 'POST', 'csrf-test'],
  ])
})
it('reports server errors without retrying a possibly accepted command', async () => {
  const fetcher = vi
    .fn()
    .mockResolvedValue({ ok: false, status: 503, json: async () => ({ detail: '公开接口不可用' }) })
  vi.stubGlobal('fetch', fetcher)
  await expect(updateCashCow('refresh')).rejects.toThrow('公开接口不可用')
  expect(fetcher).toHaveBeenCalledTimes(1)
})

it('scopes reads to the selected period but mutations to the shared job', async () => {
  const fetcher = vi.fn().mockResolvedValue({ ok: true, json: async () => ({}) })
  vi.stubGlobal('fetch', fetcher)
  await getCashCowLatest(undefined, 3)
  await getCashCowStatus(undefined, 5)
  await updateCashCow('refresh')
  await updateCashCow('cancel')
  expect(fetcher.mock.calls.map(([url]) => url)).toEqual([
    '/api/v1/cash-cow/latest?years=3',
    '/api/v1/cash-cow/status?years=5',
    '/api/v1/cash-cow/refresh',
    '/api/v1/cash-cow/cancel',
  ])
})
