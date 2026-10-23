import { useCallback, useEffect, useState } from 'react'

export function useResource<T>(loader: (signal: AbortSignal) => Promise<T>) {
  const [data, setData] = useState<T | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const [revision, setRevision] = useState(0)
  const [request, setRequest] = useState({ loader, revision })
  const reload = useCallback(() => setRevision((value) => value + 1), [])
  useEffect(() => {
    window.addEventListener('data-cache-updated', reload)
    return () => window.removeEventListener('data-cache-updated', reload)
  }, [reload])
  useEffect(() => {
    const controller = new AbortController()
    setRequest({ loader, revision })
    setLoading(true)
    setError(null)
    setData(null)
    loader(controller.signal)
      .then((result) => {
        if (!controller.signal.aborted) setData(result)
      })
      .catch((reason: unknown) => {
        if (!controller.signal.aborted) {
          setError(
            reason instanceof Error && reason.message !== 'Failed to fetch'
              ? reason.message
              : '暂时无法连接数据服务，请确认后端已启动后重试。',
          )
        }
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false)
      })
    return () => controller.abort()
  }, [loader, revision])
  // Effects run after render: never expose the previous query's data under new filters.
  const current = request.loader === loader && request.revision === revision
  return {
    data: current ? data : null,
    error: current ? error : null,
    loading: !current || loading,
    reload,
  }
}
