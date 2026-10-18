import { useCallback, useState } from 'react'
import {
  CalendarDays,
  Coins,
  Info,
  Layers3,
  RefreshCw,
  Search,
  SlidersHorizontal,
} from 'lucide-react'
import { getDividendOverview } from '../api/client'
import { useResource } from '../hooks/useResource'
import { EmptyState, ErrorState, LoadingState } from '../components/States'
import { StatCard } from '../components/StatCard'
import type { DividendItem } from '../types/dividend'
import '../styles/dividend.css'

const number = (v: number | null | undefined, suffix = '') =>
  v == null ? '—' : v.toFixed(2) + suffix

export function DividendPage() {
  const [method, setMethod] = useState('classic')
  const [search, setSearch] = useState('')
  const [overlapOnly, setOverlapOnly] = useState(false)
  const loader = useCallback((signal: AbortSignal) => getDividendOverview(signal), [])
  const { data, error, loading, reload } = useResource(loader)
  const group = data?.groups.find((g) => g.id === method)
  const overlap = new Set(data?.overlap_codes ?? [])
  const term = search.trim().toLowerCase()
  const items = (group?.items ?? []).filter(
    (s) =>
      (!overlapOnly || overlap.has(s.code)) &&
      [s.code, s.name, s.industry_name ?? ''].some((v) => v.toLowerCase().includes(term)),
  )
  const classic = method === 'classic'
  const metric = (s: DividendItem) => (classic ? s.dividend_ratio_3y : s.dividend_yield_1y)
  const maxYield = Math.max(1, ...(group?.items ?? []).map(metric))
  return (
    <div className="page-enter dividend-page">
      <div className="page-heading">
        <div>
          <span className="section-eyebrow">DIVIDEND SELECTION</span>
          <h1>
            高股息选股 <span className="heading-tag">双策略 · TOP 25</span>
          </h1>
          <p>把分红回报、估值与盈利质量，放在同一张清单中观察。</p>
        </div>
        <div className="heading-actions">
          <span className="date-badge">
            <CalendarDays size={15} />
            数据日期 {data?.snapshot?.trade_date ?? '—'}
          </span>
          <button
            className="button button-quiet icon-button"
            aria-label="刷新高股息选股"
            disabled={loading}
            onClick={reload}
          >
            <RefreshCw size={16} className={loading ? 'spin' : ''} />
          </button>
        </div>
      </div>
      {loading ? (
        <LoadingState />
      ) : error ? (
        <ErrorState message={error} retry={reload} />
      ) : !data?.snapshot ? (
        <EmptyState
          title="暂无高股息选股结果"
          description="请先在聚宽运行高股息选股_数据库版.py，或新版批量采集脚本，完成入库后刷新。"
        />
      ) : (
        <>
          <div className="dividend-methods" aria-label="选择高股息筛选方式">
            {[
              {
                id: 'classic',
                name: '纯高股息',
                en: 'DIVIDEND & QUALITY',
                detail: '三年分红前10% · PEG · 盈利增长',
              },
              {
                id: 'value',
                name: '高股息低估值',
                en: 'YIELD & VALUE',
                detail: '近一年股息率 > 4% · 0 < PE < 8',
              },
            ].map((m) => (
              <button
                key={m.id}
                className={'dividend-method ' + (method === m.id ? 'selected' : '')}
                aria-pressed={method === m.id}
                onClick={() => setMethod(m.id)}
              >
                <Coins size={26} />
                <span>
                  <small>{m.en}</small>
                  <strong>{m.name}</strong>
                  <em>{m.detail}</em>
                </span>
                <b>
                  {data.groups.find((g) => g.id === m.id)?.items.length ?? 0}
                  <small> / 25 只</small>
                </b>
              </button>
            ))}
          </div>
          <div className="stats-grid">
            <StatCard
              label="本组入选"
              value={group?.items.length ?? 0}
              suffix="只"
              note={'符合全部条件 ' + (group?.candidates ?? 0) + ' 只'}
              icon={<Layers3 size={18} />}
              accent
            />
            <StatCard
              label={classic ? '最高三年累计分红比例' : '最高近一年股息率'}
              value={number(group?.items[0] ? metric(group.items[0]) : null)}
              suffix="%"
              note={classic ? '累计比例，未年化' : '已除息现金分红 / 总市值'}
              icon={<Coins size={18} />}
            />
            <StatCard
              label="两组共同入选"
              value={data.overlap_codes.length}
              suffix="只"
              note="同一股票可分别进入两组"
              icon={<Layers3 size={18} />}
            />
            <StatCard
              label="可选股票池"
              value={data.snapshot.eligible_count}
              suffix="只"
              note="统一剔除 ST、停牌及开盘涨跌停等"
              icon={<SlidersHorizontal size={18} />}
            />
          </div>
          <section className="panel dividend-results">
            <div className="panel-heading">
              <div>
                <span className="section-eyebrow">RANKED SELECTION</span>
                <h2>{group?.name} · 前 25 名</h2>
                <p>
                  {classic ? '按三年累计分红比例降序' : '按近一年股息率降序'}，同值按代码排序。不足
                  25 只不放宽条件。
                </p>
              </div>
              <span className="heading-tag">仅保留最新结果</span>
            </div>
            <div className="dividend-toolbar">
              <label className="dividend-search">
                <Search size={17} />
                <input
                  aria-label="搜索高股息股票"
                  value={search}
                  onChange={(e) => setSearch(e.target.value)}
                  placeholder="搜索股票代码、名称或行业"
                />
              </label>
              <label className="dividend-overlap">
                <input
                  type="checkbox"
                  checked={overlapOnly}
                  onChange={(e) => setOverlapOnly(e.target.checked)}
                />
                只看两组共同入选
              </label>
              <span>
                展示 {items.length} / {group?.items.length ?? 0} 只
              </span>
            </div>
            {!group?.items.length ? (
              <EmptyState
                title="本组暂无符合条件的股票"
                description="采集已完成；当前没有满足全部条件的股票，不使用其他股票补足。"
              />
            ) : !items.length ? (
              <EmptyState
                search
                title="没有匹配的股票"
                description="试试其他代码、名称或行业，或取消共同入选筛选。"
              />
            ) : (
              <div
                className="dividend-table-wrap"
                tabIndex={0}
                role="region"
                aria-label="高股息股票明细，可左右滚动"
              >
                <table className="dividend-table">
                  <thead>
                    <tr>
                      <th>排名 / 股票</th>
                      <th>细分行业</th>
                      <th>{classic ? '三年累计分红比例' : '近一年股息率'}</th>
                      <th>{classic ? '近一年股息率' : '三年累计分红比例'}</th>
                      <th>PE / PB</th>
                      <th>总市值 / 亿元</th>
                      <th>收盘价 / 元</th>
                      <th>PEG</th>
                      <th>营收同比</th>
                      <th>净利润同比</th>
                      <th>扣非 ROE</th>
                      <th>财报 / 公告日期</th>
                    </tr>
                  </thead>
                  <tbody>
                    {items.map((s) => (
                      <tr key={s.code}>
                        <td>
                          <div className="dividend-stock">
                            <b className={'dividend-rank ' + (s.rank <= 3 ? 'top' : '')}>
                              {s.rank}
                            </b>
                            <span>
                              <strong>{s.name}</strong>
                              <small>
                                {s.code}
                                {overlap.has(s.code) && <em>共同入选</em>}
                              </small>
                            </span>
                          </div>
                        </td>
                        <td>{s.industry_name ?? '暂无行业'}</td>
                        <td>
                          <div className="dividend-yield">
                            <strong>{number(metric(s), '%')}</strong>
                            <span>
                              <i
                                style={{ width: `${Math.max(0, (metric(s) / maxYield) * 100)}%` }}
                              />
                            </span>
                          </div>
                        </td>
                        <td>{number(classic ? s.dividend_yield_1y : s.dividend_ratio_3y, '%')}</td>
                        <td>
                          {number(s.pe_ttm)} / {number(s.pb)}
                        </td>
                        <td>{number(s.market_cap)}</td>
                        <td>{number(s.close)}</td>
                        <td>{number(s.peg)}</td>
                        <td>{number(s.revenue_growth, '%')}</td>
                        <td>{number(s.profit_growth, '%')}</td>
                        <td>{number(s.adjusted_roe, '%')}</td>
                        <td>
                          <span className="dividend-dates">
                            {s.report_period ?? '—'}
                            <small>{s.publication_date ?? '—'}</small>
                          </span>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
            <div className="panel-bottom">
              <span>
                更新时间 {data.snapshot.updated_at.replace('T', ' ').slice(0, 19)} · 北京时间
              </span>
              <span>左右滑动查看完整指标 · 网页刷新仅读取数据库</span>
            </div>
          </section>
          <section className="method-card">
            <span className="method-icon">
              <Info size={19} />
            </span>
            <div>
              <h3>两种筛选口径</h3>
              <p>
                纯高股息：按股权登记日统计近 1095 天累计分红，在可选池内有分红记录的股票中取比例前
                10%，再要求 0.1 &lt; PEG &lt; 2、营收同比 &gt; 3%、净利润同比 &gt; 8%、扣非 ROE &gt;
                4.5%。最后按三年累计分红比例取前 25。
              </p>
              <p>
                高股息低估值：按除息日统计近 365 天现金分红，要求股息率 &gt; 4%、0 &lt; PE &lt;
                8，再按股息率取前
                25；不附加第一组的成长条件。分红比例均以所选交易日总市值为分母，三年累计比例未年化。
              </p>
              <span>
                两个方法共用过滤后的股票池；不下单，不沿用持仓豁免。每次采集重算最近完整交易日，两组结果一次事务替换；失败保留旧结果。财务指标使用各股票截至当日已披露数据，报告期可能不同；缺失指标显示
                —。
              </span>
            </div>
          </section>
        </>
      )}
    </div>
  )
}
