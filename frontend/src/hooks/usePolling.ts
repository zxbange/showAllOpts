import { useCallback, useEffect, useRef } from 'react'

/** Poll after a request settles; only the latest request may publish a result. */
export function usePolling<T>(
  loader: (signal: AbortSignal) => Promise<T>,
  onData: (data: T) => void,
  onError: (error: unknown) => void,
  interval: number,
) {
  const handlers = useRef({ loader, onData, onError, interval })
  handlers.current = { loader, onData, onError, interval }
  const mounted = useRef(false)
  const active = useRef<AbortController | null>(null)
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined)
  const cancel = useCallback(() => {
    clearTimeout(timer.current)
    active.current?.abort()
    active.current = null
  }, [])
  const refresh = useCallback(async (): Promise<void> => {
    cancel()
    if (!mounted.current) return
    const controller = new AbortController()
    active.current = controller
    const load = handlers.current.loader
    try {
      const data = await load(controller.signal)
      if (!controller.signal.aborted && mounted.current && handlers.current.loader === load)
        handlers.current.onData(data)
    } catch (error) {
      if (!controller.signal.aborted && mounted.current && handlers.current.loader === load)
        throw error
    } finally {
      if (mounted.current && active.current === controller) {
        active.current = null
        timer.current = setTimeout(() => {
          void refresh().catch((error: unknown) => handlers.current.onError(error))
        }, handlers.current.interval)
      }
    }
  }, [cancel])

  useEffect(() => {
    mounted.current = true
    void refresh().catch((error: unknown) => handlers.current.onError(error))
    return () => {
      mounted.current = false
      cancel()
    }
  }, [loader, refresh, cancel])

  useEffect(() => {
    if (active.current) return
    clearTimeout(timer.current)
    timer.current = setTimeout(() => {
      void refresh().catch((error: unknown) => handlers.current.onError(error))
    }, interval)
    return () => clearTimeout(timer.current)
  }, [interval, refresh])

  return { refresh, cancel }
}
