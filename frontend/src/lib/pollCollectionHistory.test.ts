import { afterEach, expect, it, vi } from 'vitest'
import { pollCollectionHistory } from './pollCollectionHistory'
import type { CollectionHistoryResponse } from '../api/collectionHistory'
afterEach(() => vi.useRealTimers())
it('aborts old module requests and never publishes late history onto the next page', async () => {
  vi.useFakeTimers()
  let resolve!: (data: CollectionHistoryResponse) => void
  const fetch = vi.fn(
    (_module: string, _signal: AbortSignal) =>
      new Promise<CollectionHistoryResponse>((r) => {
        resolve = r
      }),
  )
  const data = vi.fn(),
    error = vi.fn()
  const stop = pollCollectionHistory('commodities-a00', fetch, data, error)
  expect(fetch.mock.calls[0][0]).toBe('commodities-a00')
  stop()
  expect(fetch.mock.calls[0][1].aborted).toBe(true)
  resolve({ items: [] })
  await vi.advanceTimersByTimeAsync(30000)
  expect(data).not.toHaveBeenCalled()
  expect(error).not.toHaveBeenCalled()
  expect(fetch).toHaveBeenCalledTimes(1)
})
it('distinguishes reading failure from no records and retries without overlapping requests', async () => {
  vi.useFakeTimers()
  const fetch = vi.fn().mockRejectedValueOnce(new Error('offline')).mockResolvedValue({ items: [] })
  const data = vi.fn(),
    error = vi.fn()
  const stop = pollCollectionHistory('growth-dividend', fetch, data, error)
  await vi.advanceTimersByTimeAsync(0)
  expect(error).toHaveBeenCalledTimes(1)
  expect(data).not.toHaveBeenCalled()
  await vi.advanceTimersByTimeAsync(15000)
  expect(data).toHaveBeenCalledWith({ items: [] })
  stop()
})
