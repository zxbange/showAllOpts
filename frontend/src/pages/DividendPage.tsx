import { useSearchParams } from 'react-router-dom'
import { useCallback, useState } from 'react'
import { CalendarDays, Coins, Layers3, RefreshCw, Search, SlidersHorizontal } from 'lucide-react'
import { PageHeader } from '../components/PageHeader'
import { getDividendOverview } from '../api/client'
import { useResource } from '../hooks/useResource'
import { EmptyState, ErrorState, LoadingState } from '../components/States'
import { StatCard } from '../components/StatCard'
import { DividendYieldHistory } from '../components/DividendYieldHistory'
import '../styles/dividend.css'

const number = (v: number | null | undefined, suffix = '') =>
  v == null ? '—' : v.toFixed(2) + suffix

export function DividendPage() {
  const [params] = useSearchParams()
  const method = params.get('method') === 'bank' ? 'bank' : 'value'
  const [search, setSearch] = useState('')
  const loader = useCallback((signal: AbortSignal) => getDividendOverview(signal), [])
  const { data, error, loading, reload } = useResource(loader)
  const group = data?.groups.find((g) => g.id === method)
  const term = search.trim().toLowerCase()
  const items = (group?.items ?? []).filter((s) =>
    [s.code, s.name, s.industry_name ?? ''].some((v) => v.toLowerCase().includes(term)),
  )
  const limit = method === 'bank' ? 10 : 25
  const maxYield = Math.max(1, ...(group?.items ?? []).map((s) => s.dividend_yield_static))
  return (
    <div className="page-enter dividend-page">
      <PageHeader>
        <div>
          <h1>
            高股息低估值 <span className="heading-tag">银行与非银行</span>
          </h1>
          <p>静态股息率 &gt; 3% · 0 &lt; PE &lt; 12 · 非银行前 25 只，银行前 10 只</p>
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
      </PageHeader>
      {loading ? (
        <LoadingState />
      ) : error ? (
        <ErrorState message={error} retry={reload} />
      ) : !data?.snapshot ? (
        <EmptyState title="暂无高股息选股结果" description="请运行采集脚本，再同步本模块。" />
      ) : (
        <>
          {data.requires_refresh && (
            <div className="panel" role="status">保留上次成功结果，采用旧版选股口径；新版采集成功后更新。</div>
          )}
          <div className="stats-grid dividend-stats">
            <StatCard
              label="本组入选"
              value={group?.items.length ?? 0}
              suffix="只"
              note={`本组最多 ${limit} 只`}
              icon={<Layers3 size={18} />}
              accent
            />
            <StatCard
              label={data.requires_refresh ? "最高股息率（旧口径）" : "最高方案股息率"}
              value={number(group?.items[0]?.dividend_yield_static)}
              suffix="%"
              note={data.requires_refresh ? "旧版规则，详见数据日期" : "已公开税前每股分红 / 当日股价"}
              icon={<Coins size={18} />}
            />
            <StatCard
              label="可选股票池"
              value={data.snapshot.eligible_count}
              suffix="只"
              note="剔除 ST、退市及 4/8 开头股票"
              icon={<SlidersHorizontal size={18} />}
            />
          </div>
          <section className="panel dividend-results">
            <div className="panel-heading">
              <div>
                <h2>
                  {group?.name} · 前 {limit} 名
                </h2>
                <p>{data.requires_refresh ? "旧版股息率从高到低排序" : "方案股息率从高到低排序"}</p>
              </div>
              <span className="heading-tag">最新结果</span>
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
              <span>
                展示 {items.length} / {group?.items.length ?? 0} 只
              </span>
            </div>
            {!group?.items.length ? (
              <EmptyState title="本组暂无符合条件的股票" />
            ) : !items.length ? (
              <EmptyState search title="没有匹配的股票" description="试试其他代码、名称或行业。" />
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
                      <th>静态股息率</th>
                      <th>PE / PB</th>
                      <th>总市值 / 亿元</th>
                      <th>收盘价 / 元</th>
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
                              <small>{s.code}</small>
                            </span>
                          </div>
                        </td>
                        <td>{s.industry_name ?? '暂无行业'}</td>
                        <td>
                          <div className="dividend-yield">
                            <strong>
                              {number(s.dividend_yield_static, '%')}
                              <DividendYieldHistory history={s.dividend_yield_history} />
                            </strong>
                            <span>
                              <i
                                style={{
                                  width: `${Math.max(0, (s.dividend_yield_static / maxYield) * 100)}%`,
                                }}
                              />
                            </span>
                          </div>
                        </td>
                        <td>
                          {number(s.pe_ttm)} / {number(s.pb)}
                        </td>
                        <td>{number(s.market_cap)}</td>
                        <td>{number(s.close)}</td>
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
            </div>
          </section>
        </>
      )}
    </div>
  )
}
