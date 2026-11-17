import { createContext, useContext, useMemo, useState, type ReactNode } from 'react'
import { createPortal } from 'react-dom'

const ActionContext = createContext<{
  target: HTMLDivElement | null
  setTarget: (target: HTMLDivElement | null) => void
} | null>(null)

/** Scope the destination to the current page, never to a global DOM selector. */
export function PageHeaderActionsProvider({ children }: { children: ReactNode }) {
  const [target, setTarget] = useState<HTMLDivElement | null>(null)
  const value = useMemo(() => ({ target, setTarget }), [target])
  return <ActionContext.Provider value={value}>{children}</ActionContext.Provider>
}

export function PageHeaderActionSlot() {
  const context = useContext(ActionContext)
  return <div ref={context?.setTarget} className="page-header-actions" aria-label="数据采集操作" />
}

/** Keep command state in its existing owner; only move its rendered controls. */
export function PageHeaderActions({ children }: { children: ReactNode }) {
  const context = useContext(ActionContext)
  if (!context) return <>{children}</>
  return context.target ? createPortal(children, context.target) : null
}
