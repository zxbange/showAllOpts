import { afterEach, expect, it, vi } from 'vitest'
import { getCollectionOutcomes } from './collectionOutcomes'
vi.mock('./http', () => ({ apiFetch: vi.fn() }))
import { apiFetch } from './http'
afterEach(() => vi.resetAllMocks())
it('reads one authenticated outcome summary with cancellation support', async () => {
  const controller = new AbortController()
  vi.mocked(apiFetch).mockResolvedValue(
    new Response(JSON.stringify({ items: {} }), { status: 200 }),
  )
  expect(await getCollectionOutcomes(controller.signal)).toEqual({ items: {} })
  expect(apiFetch).toHaveBeenCalledWith('/api/v1/collection-outcomes', {
    signal: controller.signal,
    cache: 'no-store',
  })
})
it('rejects unavailable or malformed responses instead of retaining a false green', async () => {
  vi.mocked(apiFetch).mockResolvedValue(new Response('{}', { status: 503 }))
  await expect(getCollectionOutcomes()).rejects.toThrow('暂不可用')
  vi.mocked(apiFetch).mockResolvedValue(new Response('{"items":[]}', { status: 200 }))
  await expect(getCollectionOutcomes()).rejects.toThrow('格式无效')
})
