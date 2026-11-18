import { createContext, useContext, useEffect, useState, type ReactNode } from 'react'
import { getCollectionOutcomes, type CollectionOutcomes } from '../api/collectionOutcomes'
import { collectionOutcomePresentation } from '../lib/collectionOutcomes'
import '../styles/collection-outcome.css'
import { pollCollectionOutcomes } from '../lib/pollCollectionOutcomes'

export const CollectionOutcomeContext = createContext<{
  data: CollectionOutcomes
  unavailable: boolean
}>({
  data: { items: {} },
  unavailable: false,
})

/** A single authenticated poll shared across labels, including navigation changes. */
export function CollectionOutcomesProvider({
  children,
  enabled = true,
}: {
  children: ReactNode
  enabled?: boolean
}) {
  const [data, setData] = useState<CollectionOutcomes>({ items: {} })
  const [unavailable, setUnavailable] = useState(false)
  useEffect(
    () =>
      pollCollectionOutcomes(
        enabled,
        getCollectionOutcomes,
        (next) => {
          setData(next)
          setUnavailable(false)
        },
        () => setUnavailable(true),
      ),
    [enabled],
  )
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
      data-symbol={symbol}
    />
  )
}
