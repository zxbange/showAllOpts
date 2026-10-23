import type { CSSProperties } from 'react'
import { ArrowUpRight } from 'lucide-react'
import { Link } from 'react-router-dom'
import type { Industry } from '../types/momentum'
import { displayName, formatScore, heatColor } from '../lib/format'

export function Heatmap({
  industries,
  maximum,
  date,
}: {
  industries: Industry[]
  maximum: number
  date: string
}) {
  const columns = Math.max(1, Math.min(8, Math.ceil(Math.sqrt(industries.length))))
  const tabletColumns = Math.min(6, columns)
  const mobileColumns = Math.min(3, columns)
  const rowStart = (index: number, width: number) => {
    const remainder = industries.length % width
    return remainder && index > 0 && index === industries.length - remainder
      ? width - remainder + 1
      : 'auto'
  }
  return (
    <div
      className={`heatmap-grid ${industries.length < 16 ? 'heatmap-compact' : ''}`}
      style={
        {
          '--columns': columns,
          '--tablet-columns': tabletColumns,
          '--mobile-columns': mobileColumns,
        } as CSSProperties
      }
      aria-label="行业动量热力图，按热力值从高到低逐行排列"
    >
      {industries.map((industry, index) => {
        const color = heatColor(industry.momentum, maximum)
        return (
          <Link
            className="heat-cell"
            key={industry.code}
            to={`/industry-momentum/industries/${encodeURIComponent(industry.code)}?date=${date}`}
            aria-label={`${displayName(industry.name)}，热力值 ${formatScore(industry.momentum)}，排名 ${industry.rank}，查看趋势`}
            title={`${displayName(industry.name)} · ${industry.code}\n热力值 ${formatScore(industry.momentum)} · 达标股票 ${industry.front_stock_count}/${industry.industry_stock_count}`}
            style={
              {
                '--cell-bg': color.background,
                '--cell-fg': color.color,
                '--delay': `${Math.min(index * 5, 420)}ms`,
                '--desktop-start': rowStart(index, columns),
                '--tablet-start': rowStart(index, tabletColumns),
                '--mobile-start': rowStart(index, mobileColumns),
              } as CSSProperties
            }
          >
            <div className="cell-top">
              <span>{String(industry.rank).padStart(2, '0')}</span>
              <ArrowUpRight size={12} />
            </div>
            <span className="cell-name">{displayName(industry.name)}</span>
            <strong className="cell-value">{formatScore(industry.momentum)}</strong>
          </Link>
        )
      })}
    </div>
  )
}
