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

type CollectionMember = { key: string; name: string }

export function CollectionOutcomeLight({
  moduleKey,
  name,
  members,
}: {
  moduleKey: string
  name?: string
  members?: CollectionMember[]
}) {
  const { data, unavailable } = useContext(CollectionOutcomeContext)
  const single = collectionOutcomePresentation(data.items[moduleKey], unavailable)
  const group = members?.map(({ key, name }) => ({
    ...collectionOutcomePresentation(data.items[key], unavailable),
    name,
  }))
  // A platform with a reported problem must not be hidden behind a different
  // platform's success. All members must report success to display green.
  const { tone, symbol } =
    group?.find((entry) => entry.tone === 'failed') ??
    group?.find((entry) => entry.tone === 'running') ??
    group?.find((entry) => entry.tone === 'unknown') ??
    group?.[0] ??
    single
  const label = group?.length
    ? group.map((entry) => `${entry.name}：${entry.label}`).join('；')
    : name
      ? `${name}：${single.label}`
      : single.label
  return (
    <span
      className={`collection-outcome collection-outcome--${tone}`}
      role="img"
      aria-label={label}
      title={label}
      data-collection-key={moduleKey}
      data-symbol={symbol}
    />
  )
}
