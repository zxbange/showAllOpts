import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { getDividendStatus, updateDividend } from './dividend'
import { setCsrf } from './http'

beforeEach(() => vi.stubGlobal('window', { location: { href: 'https://test.example/', origin: 'https://test.example' } }))
afterEach(() => { vi.unstubAllGlobals(); setCsrf('') })
it('uses a shared authenticated task for both dividend groups', async () => {
  setCsrf('test-csrf')
  const fetcher = vi.fn().mockResolvedValue({ ok: true, json: async () => ({ started: false }) })
  vi.stubGlobal('fetch', fetcher)
  const signal = new AbortController().signal
  await getDividendStatus(signal)
  expect(await updateDividend('refresh', signal)).toEqual({ started: false })
  await updateDividend('cancel', signal)
  expect(fetcher.mock.calls.map(([url, options]) => [url, options.method, options.signal])).toEqual([
    ['/api/v1/dividend-selection/status', 'GET', signal],
    ['/api/v1/dividend-selection/refresh', 'POST', signal],
    ['/api/v1/dividend-selection/cancel', 'POST', signal],
  ])
  expect(fetcher.mock.calls[1][1].headers.get('X-CSRF-Token')).toBe('test-csrf')
})
it('shows server rejection without retrying the command', async () => {
  const fetcher = vi.fn().mockResolvedValue({ ok: false, status: 409, json: async () => ({ detail: '当日数据须在16:00后采集' }) })
  vi.stubGlobal('fetch', fetcher)
  await expect(updateDividend('refresh')).rejects.toThrow('当日数据须在16:00后采集')
  expect(fetcher).toHaveBeenCalledTimes(1)
})
