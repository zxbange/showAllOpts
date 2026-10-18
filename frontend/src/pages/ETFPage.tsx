import { useCallback, useState } from 'react'
import type { CSSProperties } from 'react'
import { CalendarDays, ChartNoAxesCombined, Info, RefreshCw } from 'lucide-react'
import { getETFHistory } from '../api/client'
import { useResource } from '../hooks/useResource'
import { ETFChart } from '../components/ETFChart'
import { LoadingState, ErrorState, EmptyState } from '../components/States'
import { billionShares, etfColors, signedPercent } from '../lib/etf'
import '../styles/etf.css'

export function ETFPage() {
  const [group, setGroup] = useState('hs300')
  const [days, setDays] = useState(365)
  const [mode, setMode] = useState<'shares' | 'relative'>('shares')
  const [selected, setSelected] = useState<string | null>(null)
  const loader = useCallback(
    (signal: AbortSignal) => getETFHistory(group, days, signal),
    [group, days],
  )
  const { data, error, loading, reload } = useResource(loader)
  const relative = mode === 'relative' && !!data?.baseline_date
  const selectedFund = data?.funds.find((fund) => fund.code === selected)
  return (
    <div className="page-enter etf-page">
      <div className="page-heading">
        <div>
          <span className="section-eyebrow">ETF SHARE OBSERVATORY</span>
          <h1>
            ETF份额仪表盘<span className="heading-tag">申赎观察</span>
          </h1>
          <p>沿着份额的增减，观察指数基金的规模变化。</p>
        </div>
        <div className="heading-actions">
          <span className="date-badge">
            <CalendarDays size={15} />
            最新记录 {data?.end_date ?? '—'}
          </span>
          <button
            className="button button-quiet icon-button"
            aria-label="刷新ETF份额数据"
            disabled={loading}
            onClick={reload}
          >
            <RefreshCw size={16} className={loading ? 'spin' : ''} />
          </button>
        </div>
      </div>
      <div className="etf-group-switch" aria-label="ETF指数分组">
        {[
          ['hs300', '沪深300 ETF', '大盘视角'],
          ['zz500', '中证500 ETF', '中盘视角'],
        ].map(([id, name, note]) => (
          <button
            key={id}
            aria-pressed={group === id}
            className={group === id ? 'active' : ''}
            onClick={() => {
              setGroup(id)
              setSelected(null)
            }}
          >
            <ChartNoAxesCombined size={22} />
            <span>
              <strong>{name}</strong>
              <small>{note} · 跟踪 5 只</small>
            </span>
          </button>
        ))}
      </div>
      {loading ? (
        <LoadingState />
      ) : error ? (
        <ErrorState message={error} retry={reload} />
      ) : data && !data.end_date ? (
        <EmptyState
          title="暂无ETF份额数据"
          description="请先在聚宽运行 ETF份额_数据库版.py，将份额数据写入同一数据库，再在「数据同步」中更新本地快照。"
        />
      ) : (
        data && (
          <>
            <div className="etf-section-label">
              <span>{data.group_name} · 最新份额</span>
              <small>点击卡片聚焦单只 ETF，再次点击恢复全部</small>
            </div>
            <div className="etf-cards">
              {data.funds.map((fund, index) => {
                const change = fund.shares_change == null ? null : Number(fund.shares_change)
                return (
                  <button
                    key={fund.code}
                    className={`etf-card ${selected === fund.code ? 'selected' : ''}`}
                    style={{ '--fund-color': etfColors[index % etfColors.length] } as CSSProperties}
                    aria-pressed={selected === fund.code}
                    aria-label={`查看${fund.name}曲线`}
                    onClick={() => setSelected(selected === fund.code ? null : fund.code)}
                  >
                    <div className="etf-card-top">
                      <span className="etf-fund-dot" />
                      <span>{fund.code.split('.')[0]}</span>
                      <span>{fund.code.endsWith('XSHG') ? '沪' : '深'}</span>
                    </div>
                    <h2>{fund.name}</h2>
                    <div className="etf-shares">
                      {billionShares(fund.shares)}
                      <small>亿份</small>
                    </div>
                    <div
                      className={`etf-change ${change == null || change === 0 ? '' : change > 0 ? 'increase' : 'decrease'}`}
                    >
                      {change == null
                        ? '日变动 —'
                        : `${change > 0 ? '+' : ''}${billionShares(fund.shares_change)} 亿份`}
                      <span>（{signedPercent(fund.daily_change_pct)}）</span>
                    </div>
                    <div className="etf-card-date">
                      {fund.date ?? '暂无记录'}
                      {fund.lagging && <em>待更新</em>}
                    </div>
                  </button>
                )
              })}
            </div>
            <section className="panel etf-trend-panel">
              <div className="panel-heading">
                <div>
                  <div className="panel-title">
                    <span className="status-dot" />
                    <h2>
                      {relative ? '份额变化率' : '总份额变化曲线'} · {selectedFund?.name ?? '全部'}
                    </h2>
                  </div>
                  <p>
                    {data.start_date} — {data.end_date} · {data.observed_days} 个有记录的交易日
                  </p>
                </div>
                <div className="range-switch" aria-label="ETF历史范围">
                  {[
                    [30, '近1月'],
                    [90, '近3月'],
                    [180, '近半年'],
                    [365, '近1年'],
                    [0, '全部'],
                  ].map(([value, label]) => (
                    <button
                      key={value}
                      aria-pressed={days === value}
                      className={days === value ? 'active' : ''}
                      onClick={() => setDays(Number(value))}
                    >
                      {label}
                    </button>
                  ))}
                </div>
              </div>
              <div className="etf-chart-toolbar">
                <div className="range-switch" aria-label="ETF曲线指标">
                  <button
                    className={!relative ? 'active' : ''}
                    aria-pressed={!relative}
                    onClick={() => setMode('shares')}
                  >
                    总份额
                  </button>
                  <button
                    className={relative ? 'active' : ''}
                    aria-pressed={relative}
                    disabled={!data.baseline_date}
                    onClick={() => setMode('relative')}
                  >
                    份额变化率
                  </button>
                </div>
                <span>
                  {relative
                    ? `共同基准日 ${data.baseline_date} · 该日 = 0%`
                    : '卡片：红色增加 · 绿色减少 · 曲线单位：亿份'}
                </span>
                {selected && (
                  <button className="button button-quiet" onClick={() => setSelected(null)}>
                    显示全部 ETF
                  </button>
                )}
              </div>
              {selectedFund && !selectedFund.points.length ? (
                <EmptyState
                  title="该ETF在所选区间暂无数据"
                  description="可以切换到全部历史，或查看其他 ETF。"
                />
              ) : (
                <ETFChart data={data} relative={relative} selected={selected} />
              )}
              <div className="panel-bottom">
                <span>拖动滑块缩放 · 缺失日期留空</span>
                <span>
                  区间内有数据 {data.available_funds} / {data.funds.length} 只
                </span>
              </div>
              {!data.baseline_date && (
                <p className="etf-baseline-note">
                  所选区间没有共同的正份额基准日，暂不计算份额变化率。
                </p>
              )}
            </section>
            <aside className="etf-method-note">
              <Info size={19} />
              <div>
                <strong>读懂份额，而非价格</strong>
                <p>
                  卡片日变动对比前一交易日；缺少前一日记录显示“—”。曲线变化率以区间内有数据基金的共同首个正份额日为基准。不同
                  ETF 的数据可能不同步，以卡片日期为准；“待更新”表示落后于本组最新日期。
                </p>
                <p>
                  跟踪清单并非该指数的全部
                  ETF。份额变化不等于价格涨跌或资金净流入；基金份额拆分等事件也可能造成跳变。首次采集最近
                  300 个交易日，历史数据持续积累。
                </p>
              </div>
            </aside>
          </>
        )
      )}
    </div>
  )
}
