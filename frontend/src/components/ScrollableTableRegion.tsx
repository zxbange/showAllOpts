import type { ReactNode } from 'react'

/** Keep native table markup and module CSS; share the accessible scroll shell. */
export function ScrollableTableRegion({
  className,
  label,
  children,
}: {
  className: string
  label: string
  children: ReactNode
}) {
  return (
    <div className={className} tabIndex={0} role="region" aria-label={label}>
      {children}
    </div>
  )
}
