import { afterEach, expect, it, vi } from 'vitest'
import { pollCollectionOutcomes } from './pollCollectionOutcomes'
afterEach(() => vi.useRealTimers())
it('does not access the database status endpoint on overview', async () => {
  vi.useFakeTimers()
  const fetch = vi.fn().mockResolvedValue({ items: {} })
  const stop = pollCollectionOutcomes(false, fetch, vi.fn(), vi.fn())
  await vi.advanceTimersByTimeAsync(30000)
  expect(fetch).not.toHaveBeenCalled()
  stop()
})
it('starts on entering a function page, polls once, and stops on return to overview', async () => {
  vi.useFakeTimers()
  const fetch = vi.fn().mockResolvedValue({ items: {} })
  const onData = vi.fn()
  const stop = pollCollectionOutcomes(true, fetch, onData, vi.fn())
  await vi.advanceTimersByTimeAsync(0)
  expect(fetch).toHaveBeenCalledTimes(1)
  expect(onData).toHaveBeenCalledWith({ items: {} })
  await vi.advanceTimersByTimeAsync(5000)
  expect(fetch).toHaveBeenCalledTimes(2)
  stop()
  expect(fetch.mock.calls[1][0].aborted).toBe(true)
  await vi.advanceTimersByTimeAsync(30000)
  expect(fetch).toHaveBeenCalledTimes(2)
})
it('aborts an in-flight request and ignores its late result after navigation away', async () => {
  vi.useFakeTimers()
  let resolve!: (value: { items: {} }) => void
  const fetch = vi.fn(
    (_signal: AbortSignal) =>
      new Promise<{ items: {} }>((r) => {
        resolve = r
      }),
  )
  const onData = vi.fn(),
    unavailable = vi.fn()
  const stop = pollCollectionOutcomes(true, fetch, onData, unavailable)
  stop()
  expect(fetch.mock.calls[0][0].aborted).toBe(true)
  resolve({ items: {} })
  await vi.advanceTimersByTimeAsync(30000)
  expect(onData).not.toHaveBeenCalled()
  expect(unavailable).not.toHaveBeenCalled()
  expect(fetch).toHaveBeenCalledTimes(1)
})
