import type { ReactNode } from 'react'

export function StatCard({
  label,
  value,
  suffix,
  note,
  icon,
  accent = false,
}: {
  label: string
  value: string | number
  suffix?: string
  note: ReactNode
  icon: ReactNode
  accent?: boolean
}) {
  return (
    <div className={`stat-card ${accent ? 'stat-accent' : ''}`}>
      <div className="stat-label">
        {label}
        <span>{icon}</span>
      </div>
      <div className="stat-value">
        {value}
        <small>{suffix}</small>
      </div>
      <div className="stat-note">{note}</div>
    </div>
  )
}
