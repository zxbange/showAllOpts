import { useLayoutEffect, useRef, type ReactNode, type KeyboardEvent } from 'react'
import type { LucideIcon } from 'lucide-react'
import { NavLink, useLocation } from 'react-router-dom'
import '../styles/page-header.css'

export interface ModuleOption<T extends string = string> {
  id: T
  name: string
  description: string
  icon: LucideIcon
  domId?: string
  controls?: string
  href?: string
  count?: ReactNode
  countLabel?: string
}

interface ModuleSwitchProps<T extends string> {
  label: string
  items: ModuleOption<T>[]
  value?: T
  onChange?: (id: T) => void
  tabs?: boolean
}

/** 页面级模块入口；链接用于独立路由，按钮用于页内筛选。 */
export function ModuleSwitch<T extends string>({
  label,
  items,
  value,
  onChange,
  tabs = false,
}: ModuleSwitchProps<T>) {
  const root = useRef<HTMLElement | null>(null)
  const location = useLocation()
  const reveal = (element: HTMLElement) => {
    const container = root.current
    if (!container) return
    const bounds = container.getBoundingClientRect()
    const item = element.getBoundingClientRect()
    if (item.left < bounds.left) container.scrollLeft += item.left - bounds.left - 4
    else if (item.right > bounds.right) container.scrollLeft += item.right - bounds.right + 4
  }
  useLayoutEffect(() => {
    const active = root.current?.querySelector<HTMLElement>('.active')
    if (active) reveal(active)
  }, [value, location.pathname, location.search])
  const navigation = items.every((item) => item.href)
  const Container = navigation ? 'nav' : 'div'
  const moveTab = (event: KeyboardEvent<HTMLElement>, index: number) => {
    const target =
      event.key === 'ArrowRight'
        ? (index + 1) % items.length
        : event.key === 'ArrowLeft'
          ? (index + items.length - 1) % items.length
          : event.key === 'Home'
            ? 0
            : event.key === 'End'
              ? items.length - 1
              : -1
    if (target < 0) return
    event.preventDefault()
    if (tabs) onChange?.(items[target].id)
    event.currentTarget.parentElement
      ?.querySelectorAll<HTMLElement>('.module-card')
      [target]?.focus()
  }
  return (
    <Container
      ref={(element) => {
        root.current = element
      }}
      className="module-card-switch"
      aria-label={label}
      role={tabs ? 'tablist' : navigation ? undefined : 'group'}
    >
      {items.map((item, index) => {
        const Icon = item.icon
        const content = (
          <>
            <Icon size={16} aria-hidden="true" />
            <span className="module-card-text">
              <strong>{item.name}</strong>
            </span>
            {item.count != null && (
              <span className="module-card-count">
                <b>{item.count}</b>
                <small>{item.countLabel ?? '只'}</small>
              </span>
            )}
          </>
        )
        return item.href ? (
          <NavLink
            key={item.id}
            to={item.href}
            aria-label={item.name}
            title={item.description}
            onFocus={(event) => reveal(event.currentTarget)}
            onKeyDown={(event) => moveTab(event, index)}
            aria-current={value !== undefined ? (value === item.id ? 'page' : false) : undefined}
            className={({ isActive }) =>
              'module-card' +
              ((value !== undefined ? value === item.id : isActive) ? ' active' : '')
            }
          >
            {content}
          </NavLink>
        ) : (
          <button
            key={item.id}
            id={item.domId}
            aria-controls={item.controls}
            type="button"
            className={'module-card' + (value === item.id ? ' active' : '')}
            aria-label={item.name}
            title={item.description}
            onFocus={(event) => reveal(event.currentTarget)}
            onKeyDown={(event) => moveTab(event, index)}
            aria-pressed={tabs ? undefined : value === item.id}
            role={tabs ? 'tab' : undefined}
            aria-selected={tabs ? value === item.id : undefined}
            tabIndex={tabs && value !== item.id ? -1 : 0}
            onClick={() => onChange?.(item.id)}
          >
            {content}
          </button>
        )
      })}
    </Container>
  )
}
