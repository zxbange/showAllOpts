import { useCallback } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { ArrowUpRight, CalendarDays, FileSearch, Layers3, RefreshCw, Search } from 'lucide-react'
import { getEarningsOverview } from '../api/client'
import { useResource } from '../hooks/useResource'
import { EmptyState, ErrorState, LoadingState } from '../components/States'
import { StatCard } from '../components/StatCard'
import { decimal, percent, quarter, sourceNames } from '../lib/earnings'
import '../styles/earnings.css'

export function EarningsPage() {
  const [params, setParams] = useSearchParams()
  const query = params.toString()
  const loader = useCallback((signal: AbortSignal) => getEarningsOverview(query, signal), [query])
  const { data, error, loading, reload } = useResource(loader)
  function change(key: string, value: string) {
    setParams((previous) => {
      const next = new URLSearchParams(previous)
      if (value) next.set(key, value)
      else next.delete(key)
      if (key !== 'page') next.delete('page')
      if (key === 'period') {
        next.delete('industry')
        next.delete('source')
      }
      return next
    })
  }
  const periods = [
    ...new Set([data?.target_period, ...(data?.periods ?? [])].filter(Boolean)),
  ] as string[]
  return (
    <div className="page-enter earnings-page">
      <div className="page-heading">
        <div>
          <span className="section-eyebrow">EARNINGS GAP OBSERVATORY</span>
          <h1>
            净利润断层<span className="heading-tag">业绩 × 价格</span>
          </h1>
          <p>把业绩增长、价格信号与公告证据，放在一起观察。</p>
        </div>
        <div className="heading-actions">
          <span className="date-badge">
            <CalendarDays size={15} />
            数据截止 {data?.run?.as_of_date ?? '—'}
          </span>
          <button
            className="button button-quiet icon-button"
            aria-label="刷新净利润断层"
            disabled={loading}
            onClick={reload}
          >
            <RefreshCw size={16} className={loading ? 'spin' : ''} />
          </button>
        </div>
      </div>
      <div className="earnings-intro">
        <div>
          <span className="section-eyebrow">THE QUARTER IN FOCUS</span>
          <h2>{data ? quarter(data.target_period) : '最新已结束季度'}</h2>
          <p>归母净利润同比 ≥ 20% · 公告对应交易日高开 &gt; 3% · 收盘 ≥ 开盘</p>
        </div>
        <FileSearch size={52} strokeWidth={1} />
      </div>
      <div className="earnings-filters">
        <label>
          报告季度
          <select
            aria-label="报告季度"
            value={params.get('period') ?? ''}
            onChange={(e) => change('period', e.target.value)}
          >
            <option value="">最新已结束季度</option>
            {periods.map((p) => (
              <option key={p} value={p}>
                {quarter(p)}
              </option>
            ))}
          </select>
        </label>
        <label>
          细分行业
          <select
            aria-label="细分行业"
            value={params.get('industry') ?? ''}
            onChange={(e) => change('industry', e.target.value)}
          >
            <option value="">全部行业</option>
            {data?.industries.map((i) => (
              <option key={i.code} value={i.code}>
                {i.name}
              </option>
            ))}
          </select>
        </label>
        <label>
          信号来源
          <select
            aria-label="信号来源"
            value={params.get('source') ?? ''}
            onChange={(e) => change('source', e.target.value)}
          >
            <option value="">全部公告</option>
            {Object.entries(sourceNames).map(([v, n]) => (
              <option key={v} value={v}>
                {n}
              </option>
            ))}
          </select>
        </label>
        <label>
          排序
          <select
            aria-label="排序"
            value={params.get('sort') ?? 'signal_date'}
            onChange={(e) => change('sort', e.target.value)}
          >
            <option value="signal_date">信号日期</option>
            <option value="profit_yoy">利润同比</option>
            <option value="open_gap_pct">高开幅度</option>
            <option value="market_cap">总市值</option>
            <option value="pe_ttm">PE（TTM）</option>
            <option value="pb">PB</option>
          </select>
        </label>
        <label>
          顺序
          <select
            aria-label="排序方向"
            value={params.get('direction') ?? 'desc'}
            onChange={(e) => change('direction', e.target.value)}
          >
            <option value="desc">从高到低</option>
            <option value="asc">从低到高</option>
          </select>
        </label>
        <form
          className="earnings-search"
          onSubmit={(e) => {
            e.preventDefault()
            change('search', String(new FormData(e.currentTarget).get('search') ?? '').trim())
          }}
        >
          <label htmlFor="earnings-search">股票搜索</label>
          <div>
            <input
              key={params.get('search') ?? ''}
              id="earnings-search"
              name="search"
              maxLength={64}
              placeholder="代码或名称"
              defaultValue={params.get('search') ?? ''}
            />
            <button className="button button-primary icon-button" aria-label="搜索股票">
              <Search size={16} />
            </button>
          </div>
        </form>
      </div>
      {loading ? (
        <LoadingState />
      ) : error ? (
        <ErrorState message={error} retry={reload} />
      ) : (
        data && (
          <>
            {data.run && (
              <div className="stats-grid">
                <StatCard
                  label="本期入选"
                  value={data.run.matched_count}
                  suffix="只"
                  note="全部命中，不限名额"
                  icon={<FileSearch size={17} />}
                  accent
                />
                <StatCard
                  label="扫描股票"
                  value={data.run.universe_count}
                  suffix="只"
                  note="聚宽提供的 A 股股票池"
                  icon={<Search size={17} />}
                />
                <StatCard
                  label="覆盖行业"
                  value={data.industries.length}
                  suffix="个"
                  note="优先使用申万三级行业"
                  icon={<Layers3 size={17} />}
                />
                <StatCard
                  label="当前筛选"
                  value={data.total}
                  suffix="只"
                  note="点击股票查看公告与分析"
                  icon={<ArrowUpRight size={17} />}
                />
              </div>
            )}
            {!data.run ? (
              <EmptyState
                title="本季度尚未采集"
                description="请在聚宽运行 净利润断层_数据库版.py，写入同一数据库后刷新。不以旧季度数据代替本季度。"
              />
            ) : !data.items.length ? (
              <EmptyState
                search
                title={data.run.matched_count ? '没有匹配的股票' : '本季度暂未出现符合条件的股票'}
                description="可调整筛选条件，或待新公告披露后重新运行聚宽采集程序。"
              />
            ) : (
              <section className="panel earnings-results">
                <div className="panel-heading">
                  <div>
                    <h2>本期观察名单</h2>
                    <p>利润增幅取触发信号的公告；预告显示增幅下限，估值为截止日快照。</p>
                  </div>
                  <span className="heading-tag">{data.total} 只</span>
                </div>
                <div
                  className="earnings-table-scroll"
                  tabIndex={0}
                  role="region"
                  aria-label="净利润断层股票表，可横向滚动"
                >
                  <table className="earnings-table">
                    <thead>
                      <tr>
                        <th>股票 / 行业</th>
                        <th>信号日期 / 来源</th>
                        <th>归母净利同比</th>
                        <th>高开幅度</th>
                        <th>总市值 / 流通市值（亿）</th>
                        <th>PE（TTM） / PB</th>
                        <th>断层原因分析</th>
                      </tr>
                    </thead>
                    <tbody>
                      {data.items.map((s) => (
                        <tr key={s.code}>
                          <td>
                            <Link
                              className="earnings-stock-link"
                              to={`/earnings-gap/stocks/${encodeURIComponent(s.code)}?period=${s.report_period}`}
                            >
                              {s.name}
                              <ArrowUpRight size={14} />
                            </Link>
                            <small>{s.code}</small>
                            <span className="earnings-chip">{s.industry_name ?? '行业未提供'}</span>
                          </td>
                          <td>
                            {s.signal_date}
                            <small>
                              {sourceNames[s.signal_source]} · 公告 {s.announcement_date}
                            </small>
                          </td>
                          <td>
                            <strong className="earnings-positive">{percent(s.profit_yoy)}</strong>
                            <small>
                              {s.profit_basis}
                              {s.signal_source === 'forecast' ? ' · 下限' : ''}
                            </small>
                          </td>
                          <td className="earnings-positive">+{percent(s.open_gap_pct)}</td>
                          <td>
                            {decimal(s.market_cap)}
                            <small>{decimal(s.circulating_market_cap)}</small>
                          </td>
                          <td>
                            {decimal(s.pe_ttm)}
                            <small>{decimal(s.pb)}</small>
                          </td>
                          <td className="earnings-analysis-cell">
                            <details>
                              <summary>
                                <span>{s.analysis.slice(0, 68)}…</span>
                                <em>展开分析 · {s.analysis.length} 字</em>
                              </summary>
                              <p>{s.analysis}</p>
                              <small>依据：{s.analysis_basis} · 自动规则解读</small>
                            </details>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
                <div className="earnings-pagination">
                  <span>
                    第 {data.page} / {Math.max(1, Math.ceil(data.total / data.page_size))} 页
                  </span>
                  <button
                    className="button button-quiet"
                    disabled={data.page <= 1}
                    onClick={() => change('page', String(data.page - 1))}
                  >
                    上一页
                  </button>
                  <button
                    className="button button-quiet"
                    disabled={data.page * data.page_size >= data.total}
                    onClick={() => change('page', String(data.page + 1))}
                  >
                    下一页
                  </button>
                </div>
              </section>
            )}
            <p className="earnings-note">
              口径：快报、预告为报告期累计；正式财报沿用原策略的单季度指标。公告日为交易日时使用当日行情，非交易日顺延；不据此断言公告引发上涨。原因分析基于已取得的证据，具体经营原因以公告原文为准。
            </p>
          </>
        )
      )}
    </div>
  )
}
