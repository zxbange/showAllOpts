import { useCallback, useEffect, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { Search, Users, RefreshCw } from 'lucide-react'
import { getOwnership, searchOwnership, getSocialEntries } from '../api/client'
import { useResource } from '../hooks/useResource'
import { LoadingState, ErrorState, EmptyState } from '../components/States'
import { OwnershipChart } from '../components/OwnershipChart'
import type { SocialMode } from '../types/ownership'
import '../styles/ownership.css'

const socialModes: Record<SocialMode, { label: string; description: string }> = {
  first: {
    label: '股票首次出现社保持仓',
    description: '上季无社保组合，本季有新进社保组合。比较相邻两季完整的十大流通股东名单，按新进组合估算市值降序；“首次”不代表历史首次买入。',
  },
  combination: {
    label: '新增社保组合',
    description: '本季出现上季名单中没有的社保组合，允许原有社保继续持有。比较相邻两季完整的十大流通股东名单，按新进组合估算市值降序；名单新进不代表首次买入。',
  },
  increase: {
    label: '社保组合加仓',
    description: '本季十大流通股东中全部社保组合的持股总数高于上季即入选（含上季为零）。两季名单与持股数须完整，按增加股数降序；送转股和名单变动也可能导致增加。',
  },
}

const categories = [
  ['public_fund', '公募基金'],
  ['qfii', 'QFII'],
  ['social', '社保基金'],
  ['broker', '券商'],
  ['insurance', '保险'],
  ['trust', '信托'],
  ['private', '私募基金'],
]
const num = (value: number | null | undefined, digits = 0) =>
  value == null ? '—' : value.toLocaleString('zh-CN', { maximumFractionDigits: digits })

function StockView({ code }: { code: string }) {
  const loader = useCallback((signal: AbortSignal) => getOwnership(code, signal), [code])
  const { data, error, loading, reload } = useResource(loader)
  if (loading) return <LoadingState />
  if (error) return <ErrorState message={error} retry={reload} />
  if (!data) return null
  const latest = data.holders.at(-1)
  return (
    <>
      <div className="ownership-stock-heading">
        <div>
          <h2>
            {data.stock.name} <small>{data.stock.code}</small>
          </h2>
          <p>
            {data.stock.industry || '行业暂无'} · 最新已采集报告期{' '}
            {data.reports.at(-1)?.date || '暂无'}
          </p>
        </div>
        <button className="button button-quiet" onClick={reload}>
          <RefreshCw size={16} />
          刷新
        </button>
      </div>
      <section className="ownership-panel">
        <h3>股东户数</h3>
        <div className="ownership-metrics">
          <div>
            <span>最新 A 股股东户数</span>
            <strong>
              {num(latest?.payload.a_share_holders)} <small>户</small>
            </strong>
          </div>
          <div>
            <span>较上次增减</span>
            <strong>
              {num(data.holder_change)} <small>户</small>
            </strong>
          </div>
          <div>
            <span>较上次变化</span>
            <strong>
              {num(data.holder_change_pct, 2)} <small>%</small>
            </strong>
          </div>
          <div>
            <span>统计截止 / 披露日期</span>
            <strong className="ownership-date">
              {latest?.date || '暂无'}
              <small>{latest?.pub_date || '披露日期未知'}</small>
            </strong>
          </div>
        </div>
        {data.holders.length ? (
          <OwnershipChart
            dates={data.holders.map((p) => p.date)}
            values={data.holders.map((p) => p.payload.a_share_holders)}
            title="A股股东户数历史"
            holders
          />
        ) : (
          <p>暂无股东户数记录。</p>
        )}
        <p className="ownership-note">
          A 股户数与上一条记录比较，缺失不以总户数代替。
        </p>
      </section>
      <div className="ownership-grid">
        {categories.map(([key, label]) => {
          const available = data.reports.some((p) => p.payload.categories[key] != null)
          return (
            <section className="ownership-panel" key={key}>
              <h3>
                {label}
                <small>{key === 'public_fund' ? '基金披露持仓' : '十大流通股东披露持仓'}</small>
              </h3>
              {available ? (
                <OwnershipChart
                  dates={data.reports.map((p) => p.date)}
                  values={data.reports.map((p) => {
                    const v = p.payload.categories[key]?.market_value
                    return v == null ? null : v / 1e8
                  })}
                  counts={data.reports.map((p) => p.payload.categories[key]?.holder_count ?? null)}
                  title={label + '持仓趋势'}
                />
              ) : (
                <p className="ownership-missing">暂无披露记录，不代表持仓为零。</p>
              )}
            </section>
          )
        })}
      </div>
      {!data.reports.length && <p>暂无持仓报告，请先采集并同步。</p>}
    </>
  )
}

function SocialView({ onSelect }: { onSelect: (code: string) => void }) {
  const [period, setPeriod] = useState('')
  const [mode, setMode] = useState<SocialMode>('first')
  const [query, setQuery] = useState('')
  const loader = useCallback(
    (signal: AbortSignal) => getSocialEntries(period, mode, signal),
    [period, mode],
  )
  const { data, error, loading, reload } = useResource(loader)
  const items = data?.items.filter((r) => r.code.includes(query) || r.name.includes(query)) || []
  return (
    <>
      {!loading && !error && (
        <p className="ownership-summary" aria-live="polite">
          已采集 {data?.collected_count || 0} 只 · 可比较 {data?.comparable_count || 0} 只 ·
          无法判定 {data?.unknown_count || 0} 只 · 当前匹配 {items.length} 只
        </p>
      )}
      <section className="ownership-panel">
        <div className="ownership-controls">
          <label>
            报告期{' '}
            <select
              aria-label="社保报告期"
              value={period || data?.period || ''}
              onChange={(e) => setPeriod(e.target.value)}
            >
              {data?.periods.map((p) => (
                <option key={p}>{p}</option>
              ))}
            </select>
          </label>
          <label>
            筛选口径{' '}
            <select aria-label="社保筛选口径" value={mode} onChange={(e) => setMode(e.target.value as SocialMode)}>
              {Object.entries(socialModes).map(([value, option]) => (
                <option key={value} value={value}>{option.label}</option>
              ))}
            </select>
          </label>
          <input
            aria-label="筛选社保股票"
            placeholder="代码或股票名称"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
          />
          <button className="button button-quiet" onClick={reload} disabled={loading}>
            刷新
          </button>
        </div>
        <p className="ownership-note ownership-rule">{socialModes[mode].description}</p>
        {loading ? (
          <LoadingState />
        ) : error ? (
          <ErrorState message={error} retry={reload} />
        ) : (
          <>
            {items.length ? (
              <div className="ownership-table-wrap">
                <table className="ownership-table">
                  <thead>
                    <tr>
                      <th>股票 / 行业</th>
                      {mode === 'increase' ? <>
                        <th>上季度合计（万股）</th><th>本季度合计（万股）</th>
                        <th>增加股数（万股）</th><th>增幅</th><th>本季度占流通股比例</th>
                      </> : <>
                      <th>新进社保组合</th>
                      <th>持股数量（万股）</th>
                      <th>占流通股比例</th>
                      <th>估算市值（亿元）</th>
                      </>}
                      <th>披露日期</th>
                    </tr>
                  </thead>
                  <tbody>
                    {items.map((r) => (
                      <tr key={r.code}>
                        <td>
                          <button className="ownership-stock-link" onClick={() => onSelect(r.code)}>
                            {r.name}
                          </button>
                          <small>
                            {r.code} · {r.industry || '暂无行业'}
                          </small>
                        </td>
                        {mode === 'increase' ? <>
                          <td>{num(r.previous_shares == null ? null : r.previous_shares / 1e4, 2)}</td>
                          <td>{num(r.current_shares == null ? null : r.current_shares / 1e4, 2)}</td>
                          <td>{num(r.change_shares == null ? null : r.change_shares / 1e4, 2)}</td>
                          <td>{r.change_pct == null ? '上季为零' : `${num(r.change_pct, 2)}%`}</td>
                          <td>{r.current_ratio == null ? '—' : `${num(r.current_ratio, 3)}%`}</td>
                        </> : <>
                        <td>
                          {r.new_combinations?.map((m, i) => (
                            <div key={m.id || i}>{m.name}</div>
                          ))}
                        </td>
                        <td>{num(r.new_shares == null ? null : r.new_shares / 1e4, 2)}</td>
                        <td>
                          {r.new_combinations?.map((m, i) => (
                            <div key={m.id || i}>{m.ratio == null ? '—' : `${num(m.ratio, 3)}%`}</div>
                          ))}
                        </td>
                        <td>
                          {num(r.new_market_value == null ? null : r.new_market_value / 1e8, 3)}
                        </td>
                        </>}
                        <td>{r.pub_date || '未知'}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : (
              <p className="ownership-missing">
                暂无匹配记录；未披露或名单不完整不参与判定。
              </p>
            )}
          </>
        )}
      </section>
    </>
  )
}

export function OwnershipPage() {
  const [params, setParams] = useSearchParams()
  const code = params.get('code') || ''
  const tab = params.get('tab') || 'stock'
  const [query, setQuery] = useState('')
  const [term, setTerm] = useState('')
  useEffect(() => {
    const timer = setTimeout(() => setTerm(query.trim()), 250)
    return () => clearTimeout(timer)
  }, [query])
  const loader = useCallback((signal: AbortSignal) => term ? searchOwnership(term, signal) : Promise.resolve({ stocks: [] }), [term])
  const { data, error, loading, reload } = useResource(loader)
  const select = (value: string) => setParams({ code: value, tab: 'stock' })
  return (
    <div className="page-enter ownership-page">
      <div className="page-heading">
        <div>
          <span className="eyebrow">INSTITUTION OWNERSHIP OBSERVATORY</span>
          <h1>
            个股机构持仓 <span className="heading-tag">筹码观察</span>
          </h1>
          <p>查询机构持仓、股东户数与季度社保变动。</p>
        </div>
        <Users size={32} />
      </div>
      <div className="ownership-tabs">
        <button
          className={'button ' + (tab === 'stock' ? 'button-primary' : 'button-quiet')}
          onClick={() => setParams({ tab: 'stock', ...(code ? { code } : {}) })}
        >
          个股查询
        </button>
        <button
          className={'button ' + (tab === 'social' ? 'button-primary' : 'button-quiet')}
          onClick={() => setParams({ tab: 'social' })}
        >
          社保新进
        </button>
      </div>
      {tab === 'social' ? (
        <SocialView onSelect={select} />
      ) : (
        <>
          <p className="ownership-note">
            公募季报与半年报、年报披露范围不同；非公募市值按期末不复权价格估算，账户数不等于机构家数。
          </p>
          <section className="ownership-panel">
            <label className="ownership-search">
              <Search size={20} />
              <input
                aria-label="搜索股票"
                placeholder="输入股票代码或名称"
                value={query}
                onChange={(e) => setQuery(e.target.value)}
              />
            </label>
            {query.trim() && query.trim() === term && (loading ? (
              <p>正在查询…</p>
            ) : error ? (
              <ErrorState message={error} retry={reload} />
            ) : (
              <div className="ownership-results">
                {data?.stocks.map((s) => (
                  <button
                    key={s.code}
                    className={s.code === code ? 'selected' : ''}
                    onClick={() => select(s.code)}
                  >
                    <strong>{s.name}</strong>
                    <span>{s.code}</span>
                  </button>
                ))}
              </div>
            ))}
            {query.trim() && query.trim() === term && !loading && !error && !data?.stocks.length && (
              <p>暂无匹配股票；尚未采集的股票需先入库并同步。</p>
            )}
          </section>
          {code ? (
            <StockView key={code} code={code} />
          ) : (
            <EmptyState
              title="选择股票查看持仓"
              description="输入代码或名称，或从社保列表进入。"
            />
          )}
        </>
      )}
    </div>
  )
}
