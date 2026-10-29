import { afterEach, describe, expect, it, vi } from 'vitest'
import { refreshCache, saveCacheSchedule } from './client'

afterEach(() => vi.unstubAllGlobals())

describe('cache sync requests', () => {
  it('keeps daily synchronization default and sends full only when explicitly requested', async () => {
    const fetcher = vi.fn().mockResolvedValue({ ok: true })
    vi.stubGlobal('fetch', fetcher)
    await refreshCache('industry-momentum')
    await refreshCache('industry-momentum', true)
    await refreshCache(null, false)
    expect(fetcher.mock.calls.map(([url, options]) => [url, JSON.parse(options.body)])).toEqual([
      ['/api/v1/cache/refresh', { module: 'industry-momentum' }],
      ['/api/v1/cache/refresh', { module: 'industry-momentum', full: true }],
      ['/api/v1/cache/refresh', { module: null }],
    ])
    expect(fetcher.mock.calls.every(([, options]) => options.method === 'POST')).toBe(true)
  })

  it('surfaces a rejected repair without retrying implicitly', async () => {
    const fetcher = vi
      .fn()
      .mockResolvedValue({ ok: false, json: async () => ({ detail: '已有同步任务正在执行' }) })
    vi.stubGlobal('fetch', fetcher)
    await expect(refreshCache(null, true)).rejects.toThrow('已有同步任务正在执行')
    expect(fetcher).toHaveBeenCalledTimes(1)
  })
})

it('saves a clock preference separately from refresh and reports validation failures', async () => {
  const plan = { daily_time: '07:00', timezone: 'Asia/Shanghai', next_refresh_at: null }
  const fetcher = vi
    .fn()
    .mockResolvedValueOnce({ ok: true, json: async () => plan })
    .mockResolvedValueOnce({ ok: false, json: async () => ({ detail: [{ type: 'invalid' }] }) })
  vi.stubGlobal('fetch', fetcher)
  await expect(saveCacheSchedule('07:00')).resolves.toEqual(plan)
  expect(fetcher.mock.calls[0]).toEqual([
    '/api/v1/cache/schedule',
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: '{"daily_time":"07:00"}',
    },
  ])
  await expect(saveCacheSchedule('bad')).rejects.toThrow('无法保存同步时间')
  expect(fetcher).toHaveBeenCalledTimes(2)
})
