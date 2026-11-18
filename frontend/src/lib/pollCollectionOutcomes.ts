import type { CollectionOutcomes } from '../api/collectionOutcomes'

/** One lifecycle per enabled function-page visit; overview never starts a request. */
export function pollCollectionOutcomes(
  enabled: boolean,
  fetchOutcomes: (signal: AbortSignal) => Promise<CollectionOutcomes>,
  onData: (data: CollectionOutcomes) => void,
  onUnavailable: () => void,
) {
  if (!enabled) return () => {}
  let stopped = false
  let request: AbortController | undefined
  let timer: ReturnType<typeof setTimeout> | undefined
  let timeout: ReturnType<typeof setTimeout> | undefined
  async function poll() {
    request = new AbortController()
    const current = request
    timeout = setTimeout(() => current.abort(), 10000)
    try {
      const next = await fetchOutcomes(current.signal)
      if (!stopped) onData(next)
    } catch {
      if (!stopped) onUnavailable()
    } finally {
      clearTimeout(timeout)
      if (!stopped) timer = setTimeout(poll, 5000)
    }
  }
  void poll()
  return () => {
    stopped = true
    request?.abort()
    clearTimeout(timer)
    clearTimeout(timeout)
  }
}
