import { RefreshCw, type LucideIcon } from 'lucide-react'
import type { ReactNode } from 'react'

/** Render one native page action. Its owner supplies the actual command and state. */
export function PageActionButton({
  children,
  ariaLabel,
  disabled = false,
  onClick,
  tone = 'primary',
  icon,
  iconSize = 16,
  spin = false,
  iconButton = false,
}: {
  children?: ReactNode
  ariaLabel?: string
  disabled?: boolean
  onClick: () => void
  tone?: 'primary' | 'quiet'
  icon?: LucideIcon
  iconSize?: number
  spin?: boolean
  iconButton?: boolean
}) {
  const Icon = icon
  return (
    <button
      type="button"
      className={`button button-${tone}${iconButton ? ' icon-button' : ''}`}
      aria-label={ariaLabel}
      disabled={disabled}
      onClick={onClick}
    >
      {Icon && <Icon size={iconSize} className={spin ? 'spin' : ''} aria-hidden="true" />}
      {children}
    </button>
  )
}

/** Refreshes the currently displayed data; never starts a collection itself. */
export function DisplayRefreshButton({
  ariaLabel,
  onClick,
  loading = false,
  animate = true,
  iconButton = true,
}: {
  ariaLabel: string
  onClick: () => void
  loading?: boolean
  animate?: boolean
  iconButton?: boolean
}) {
  return (
    <PageActionButton
      tone="quiet"
      icon={RefreshCw}
      iconButton={iconButton}
      ariaLabel={ariaLabel}
      onClick={onClick}
      disabled={loading}
      spin={animate && loading}
    />
  )
}
