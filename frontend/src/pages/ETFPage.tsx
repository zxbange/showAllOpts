import { PageHeader } from '../components/PageHeader'
import { useCallback, useEffect, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import type { CSSProperties } from 'react'
import { CalendarDays, Info, RefreshCw } from 'lucide-react'
import { getETFHistory } from '../api/client'
import { useResource } from '../hooks/useResource'
import { ETFCollectionPanel } from '../components/ETFCollectionPanel'
import { ETFChart } from '../components/ETFChart'
import { LoadingState, ErrorState, EmptyState } from '../components/States'
import { billionShares, etfColors, signedPercent, etfGroups, marketValueSeries } from '../lib/etf'
import '../styles/etf.css'

export function ETFPage() {
  const [params, setParams] = useSearchParams()
  const group = etfGroups.some((item) => item.id === params.get('group'))
    ? params.get('group')!
    : 'hs300'
  const groupName = etfGroups.find((item) => item.id === group)!.name
  const [days, setDays] = useState(365)
  const [mode, setMode] = useState<'shares' | 'marketValue'>('shares')
  const [selected, setSelected] = useState<string | null>(null)
  useEffect(() => setSelected(null), [group])
  const loader = useCallback(
    (signal: AbortSignal) => getETFHistory(group, days, signal),
    [group, days],
  )
  const { data, error, loading, reload } = useResource(loader)
  const marketValue = mode === 'marketValue'
  const selectedFund = data?.funds.find((fund) => fund.code === selected)
  const questionableDays =
    data?.market_values?.filter((point) => point.quality_flags?.length).length ?? 0
  const hasMarketValue =
    data && marketValueSeries(data).series[0].values.some((value) => value != null)
  return (
    <div className="page-enter etf-page">
      <PageHeader>
        <div>
          <h1>
            市场情绪<span className="heading-tag">{groupName} ETF份额与市值</span>
          </h1>
          <p>切换指数，查看 ETF 总份额与所跟踪基金的合计流通市值。</p>
        </div>
        <div className="heading-actions">
          <span className="date-badge">
            <CalendarDays size={15} />
            最新记录 {data?.end_date ?? '—'}
          </span>
          <button
            className="button button-quiet icon-button"
            aria-label="刷新ETF数据"
            disabled={loading}
            onClick={() => {
              reload()
            }}
          >
            <RefreshCw size={16} className={loading ? 'spin' : ''} />
          </button>
        </div>
      </PageHeader>
      <ETFCollectionPanel onCollected={reload} />
      <div className="etf-chart-toolbar etf-controls">
        <label htmlFor="etf-group">跟踪指数</label>
        <select
          id="etf-group"
          value={group}
          onChange={(event) => {
            setSelected(null)
            const next = new URLSearchParams(params)
            next.set('group', event.target.value)
            setParams(next)
          }}
        >
          {etfGroups.map((item) => (
            <option key={item.id} value={item.id}>
              {item.name}
            </option>
          ))}
        </select>
        <div className="range-switch" aria-label="ETF曲线指标">
          <button
            className={!marketValue ? 'active' : ''}
            aria-pressed={!marketValue}
            onClick={() => setMode('shares')}
          >
            总份额
          </button>
          <button
            className={marketValue ? 'active' : ''}
            aria-pressed={marketValue}
            onClick={() => setMode('marketValue')}
          >
            流通市值
          </button>
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
      {loading ? (
        <LoadingState />
      ) : error ? (
        <ErrorState message={error} retry={reload} />
      ) : (
        data && (
          <>
            {!marketValue &&
              (!data.end_date ? (
                <EmptyState
                  title="暂无ETF份额数据"
                  description="请由管理员在本页面配置并触发采集，或切换其他指数。"
                />
              ) : (
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
                          style={
                            { '--fund-color': etfColors[index % etfColors.length] } as CSSProperties
                          }
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
                </>
              ))}
            {(marketValue || data.end_date) && (
              <section className="panel etf-trend-panel">
                <div className="panel-heading">
                  <div>
                    <div className="panel-title">
                      <span className="status-dot" />
                      <h2>
                        {marketValue
                          ? `合计流通市值 · ${groupName}`
                          : `总份额变化曲线 · ${selectedFund?.name ?? '全部'}`}
                      </h2>
                    </div>
                    <p>
                      {data.start_date ?? '—'} — {data.end_date ?? '—'} · {data.observed_days}{' '}
                      个有记录的交易日
                    </p>
                  </div>
                </div>
                <div className="etf-chart-toolbar">
                  <span>
                    {marketValue
                      ? '同日份额 × 收盘价后求和；缺数据不估算'
                      : '卡片：红色增加 · 绿色减少 · 曲线单位：亿份'}
                  </span>
                  {!marketValue && selected && (
                    <button className="button button-quiet" onClick={() => setSelected(null)}>
                      显示全部 ETF
                    </button>
                  )}
                </div>
                {marketValue && !hasMarketValue ? (
                  <EmptyState
                    title={questionableDays ? '流通市值数据待核验' : '流通市值数据未齐'}
                    description="所选区间尚无全部跟踪 ETF 同日份额与收盘价齐全的记录，不以零或估算值代替。可切换区间或稍后刷新。"
                  />
                ) : !marketValue && selectedFund && !selectedFund.points.length ? (
                  <EmptyState
                    title="该ETF在所选区间暂无数据"
                    description="可以切换到全部历史，或查看其他 ETF。"
                  />
                ) : (
                  <ETFChart
                    data={data}
                    marketValue={marketValue}
                    selected={marketValue ? null : selected}
                  />
                )}
                <div className="panel-bottom">
                  <span>拖动滑块缩放 · 缺失日期留空</span>
                  <span>
                    {marketValue
                      ? `跟踪清单 ${data.funds.length} 只 ETF · 单日数据全部齐全才显示合计`
                      : `区间内有数据 ${data.available_funds} / ${data.funds.length} 只`}
                  </span>
                </div>
              </section>
            )}
            <aside className="etf-method-note">
              <Info size={19} />
              <div>
                <strong>{marketValue ? '流通市值口径' : '份额口径'}</strong>
                <p>
                  {marketValue
                    ? '仅展示当前指数跟踪清单的 ETF 合计市值，单位为亿元人民币，不展示单只市值、不额外加权。任一 ETF 缺少同日份额或收盘价，该日曲线中断，不插值。'
                    : '日变动对比前一交易日，缺失显示“—”；“待更新”表示落后于本组最新日期，以各卡片日期为准。'}
                </p>
                {marketValue && questionableDays > 0 && (
                  <p role="status">
                    {questionableDays}{' '}
                    个交易日因疑似份额拆并或异常跳变暂不显示市值，待核验后再使用；异常也可能来自真实申赎。
                  </p>
                )}
                {marketValue && data.funds.some((fund) => fund.code.endsWith('.XSHE')) && (
                  <p>深市仅使用已核验的收盘价日期，更早历史缺价保留空白，随后按日积累。</p>
                )}
                <p>
                  清单不覆盖该指数全部
                  ETF。份额与市值变化不是资金净流入；系统会屏蔽已识别的疑似拆并与异常跳变，但尚未接入完整基金事件清单。
                </p>
              </div>
            </aside>
          </>
        )
      )}
    </div>
  )
}
