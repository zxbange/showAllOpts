import type { ReactNode } from 'react'

/** Shared heading layout; badges, descriptions and actions remain module-owned. */
export function TrendPanelHeading({
  title,
  description,
  badge,
  actions,
  statusDot = true,
}: {
  title: ReactNode
  description?: ReactNode
  badge?: ReactNode
  actions?: ReactNode
  statusDot?: boolean
}) {
  return (
    <div className="panel-heading">
      <div>
        {statusDot || badge ? (
          <div className="panel-title">
            {statusDot && <span className="status-dot" aria-hidden="true" />}
            <h2>{title}</h2>
            {badge}
          </div>
        ) : (
          <h2>{title}</h2>
        )}
        {description != null && <p>{description}</p>}
      </div>
      {actions}
    </div>
  )
}
