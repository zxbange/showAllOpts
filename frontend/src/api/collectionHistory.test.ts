import { afterEach, expect, it, vi } from 'vitest'
import { getCollectionHistory, getPageCollectionHistory } from './collectionHistory'
vi.mock('./http', () => ({ apiFetch: vi.fn() }))
import { apiFetch } from './http'
afterEach(() => vi.resetAllMocks())
it('requests only five records and bounds an oversized server response', async () => {
  const signal = new AbortController().signal
  vi.mocked(apiFetch).mockResolvedValue(
    new Response(
      JSON.stringify({ items: Array.from({ length: 8 }, (_, i) => ({ run_key: String(i) })) }),
    ),
  )
  expect((await getCollectionHistory('cash-cow-3', signal)).items).toHaveLength(5)
  expect(apiFetch).toHaveBeenCalledWith(
    '/api/v1/collection-outcomes/history?module=cash-cow-3&limit=5',
    { signal, cache: 'no-store' },
  )
})
it('does not convert network or invalid payload failures into empty history', async () => {
  vi.mocked(apiFetch).mockResolvedValue(new Response('{}', { status: 503 }))
  await expect(getCollectionHistory('cash-cow')).rejects.toThrow()
  vi.mocked(apiFetch).mockResolvedValue(new Response('{"items":{}}'))
  await expect(getCollectionHistory('cash-cow')).rejects.toThrow()
})

it('combines social sources into five total records while retaining source and failure identity', async () => {
  vi.mocked(apiFetch).mockImplementation(async (url) => {
    const weibo = String(url).includes('social-weibo')
    return new Response(
      JSON.stringify({
        items: Array.from({ length: 5 }, (_, i) => ({
          run_key: String(i),
          outcome: i ? 'failed' : 'success',
          finished_at: `2026-11-18T${18 - i}:00:${weibo ? '00' : '01'}+08:00`,
          trigger: 'scheduled',
          kind: 'collection',
        })),
      }),
    )
  })
  const result = await getPageCollectionHistory('social-feed')
  expect(result.items).toHaveLength(5)
  expect(result.items[0].source_name).toBe('雪球')
  expect(result.items[1].source_name).toBe('微博')
  expect(result.items[2].outcome).toBe('failed')
  expect(new Set(result.items.map((item) => item.run_key)).size).toBe(5)
})
it('does not pass off one successful social source as a complete history when the other fails', async () => {
  vi.mocked(apiFetch).mockImplementation(async (url) =>
    String(url).includes('social-weibo')
      ? new Response('{"items":[]}')
      : new Response('{}', { status: 503 }),
  )
  await expect(getPageCollectionHistory('social-feed')).rejects.toThrow()
})
