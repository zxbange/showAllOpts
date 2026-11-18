import { createContext, useContext, useEffect, useState, type ReactNode } from 'react'
import { getCollectionOutcomes, type CollectionOutcomes } from '../api/collectionOutcomes'
import { collectionOutcomePresentation } from '../lib/collectionOutcomes'
import '../styles/collection-outcome.css'

export const CollectionOutcomeContext = createContext<{
  data: CollectionOutcomes
  unavailable: boolean
}>({
  data: { items: {} },
  unavailable: false,
})

/** A single authenticated poll shared across labels, including navigation changes. */
export function CollectionOutcomesProvider({ children }: { children: ReactNode }) {
  const [data, setData] = useState<CollectionOutcomes>({ items: {} })
  const [unavailable, setUnavailable] = useState(false)
  useEffect(() => {
    let stopped = false
    let request: AbortController | undefined
    let timer: ReturnType<typeof setTimeout> | undefined
    async function poll() {
      request = new AbortController()
      const timeout = setTimeout(() => request?.abort(), 10000)
      try {
        const next = await getCollectionOutcomes(request.signal)
        if (!stopped) {
          setData(next)
          setUnavailable(false)
        }
      } catch {
        if (!stopped) setUnavailable(true)
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
    }
  }, [])
  return (
    <CollectionOutcomeContext.Provider value={{ data, unavailable }}>
      {children}
    </CollectionOutcomeContext.Provider>
  )
}

export function CollectionOutcomeLight({ moduleKey, name }: { moduleKey: string; name?: string }) {
  const { data, unavailable } = useContext(CollectionOutcomeContext)
  const { tone, symbol, label } = collectionOutcomePresentation(data.items[moduleKey], unavailable)
  return (
    <span
      className={`collection-outcome collection-outcome--${tone}`}
      role="img"
      aria-label={name ? `${name}：${label}` : label}
      title={name ? `${name}：${label}` : label}
      data-collection-key={moduleKey}
    >
      {symbol}
    </span>
  )
}
