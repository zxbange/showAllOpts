import type { CSSProperties } from 'react'
import type { ETFHistory } from '../types/etf'
import { billionShares, etfColors, recentShareRows } from '../lib/etf'

export function ETFShareTable({
  data,
  selected,
  onSelect,
}: {
  data: ETFHistory
  selected: string | null
  onSelect: (code: string | null) => void
}) {
  const { dates, rows } = recentShareRows(data)
  return (
    <section className="panel etf-share-panel" aria-labelledby="etf-recent-shares-heading">
      <div className="etf-section-label">
        <h2 id="etf-recent-shares-heading">{data.group_name} · 近5日份额</h2>
        <span>单位：亿份</span>
      </div>
      <div className="etf-table-scroll" tabIndex={0} role="region" aria-label="ETF近5个交易日份额">
        <table className="etf-share-table">
          <thead>
            <tr>
              <th scope="col">ETF</th>
              {dates.map((date, index) => (
                <th scope="col" key={index}>
                  {index === 4 ? 'T日' : `T-${4 - index}`}
                  <span className="etf-column-date">{date ?? '—'}</span>
                </th>
              ))}
              <th scope="col">
                份额增减<span className="etf-column-date">T日 − T-1</span>
              </th>
            </tr>
          </thead>
          <tbody>
            {rows.map(({ fund, shares, change }, index) => (
              <tr
                key={fund.code}
                className={selected === fund.code ? 'selected' : ''}
                style={{ '--fund-color': etfColors[index % etfColors.length] } as CSSProperties}
              >
                <th scope="row">
                  <button
                    className="etf-fund-button"
                    aria-pressed={selected === fund.code}
                    aria-label={`查看${fund.name}曲线`}
                    onClick={() => onSelect(selected === fund.code ? null : fund.code)}
                  >
                    <span className="etf-fund-name">
                      <span className="etf-fund-dot" />
                      {fund.name}
                    </span>
                    <span className="etf-fund-code">
                      {fund.code.split('.')[0]} · {fund.code.endsWith('XSHG') ? '沪' : '深'}
                    </span>
                  </button>
                </th>
                {shares.map((value, day) => (
                  <td key={day}>{billionShares(value)}</td>
                ))}
                <td
                  className={
                    change == null || change === '0'
                      ? ''
                      : BigInt(change) > 0n
                        ? 'increase'
                        : 'decrease'
                  }
                >
                  {change != null && BigInt(change) > 0n ? '+' : ''}
                  {billionShares(change)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  )
}
