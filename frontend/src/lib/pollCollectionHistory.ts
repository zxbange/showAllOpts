import type { CollectionHistoryResponse } from '../api/collectionHistory'

/** Cancel both the old module request and its retry loop before changing tabs. */
export function pollCollectionHistory(
  moduleKey: string,
  fetchHistory: (key: string, signal: AbortSignal) => Promise<CollectionHistoryResponse>,
  onData: (data: CollectionHistoryResponse) => void,
  onUnavailable: () => void,
) {
  let stopped = false
  let request: AbortController | undefined
  let next: ReturnType<typeof setTimeout> | undefined
  let timeout: ReturnType<typeof setTimeout> | undefined
  async function poll() {
    request = new AbortController()
    const current = request
    timeout = setTimeout(() => current.abort(), 10000)
    try {
      const result = await fetchHistory(moduleKey, current.signal)
      if (!stopped) onData(result)
    } catch {
      if (!stopped) onUnavailable()
    } finally {
      clearTimeout(timeout)
      if (!stopped) next = setTimeout(poll, 15000)
    }
  }
  void poll()
  return () => {
    stopped = true
    request?.abort()
    clearTimeout(next)
    clearTimeout(timeout)
  }
}
