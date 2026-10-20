import { useCallback, useMemo, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import {
  ArrowLeft,
  CalendarDays,
  Flame,
  RefreshCw,
  Search,
  TrendingUp,
  Layers,
  CheckCircle2,
} from 'lucide-react'
import { getETFHeat, getETFHeatHistory } from '../api/client'
import { useResource } from '../hooks/useResource'
import { EmptyState, ErrorState, LoadingState } from '../components/States'
import { StatCard } from '../components/StatCard'
import { TrendChart } from '../components/TrendChart'
import type { HeatPoint } from '../types/etfHeat'
import '../styles/etfHeat.css'

const number = (n: number | null | undefined, digits = 4) => (n == null ? '—' : n.toFixed(digits))
const condition = (value: boolean | null) =>
  value === null ? '条件数据不足' : value ? '收盘参考条件通过' : '收盘参考条件未通过'
const labels: Record<string, string> = {
  score_range: '0 < 得分 < 6',
  drawdown: '近三段跌幅限制',
  intraday: '当日跌幅限制',
  volume_daily: '日成交量不超过均量2倍',
  rsrs: 'RSRS 与均线',
}
function CheckDetails({ point }: { point: HeatPoint }) {
  return (
    <div className="etf-checks">
      {point.details.checks ? (
        Object.entries(point.details.checks).map(([key, value]) => (
          <span key={key} className={value === null ? '' : value ? 'passed' : 'failed'}>
            {labels[key] ?? key} · {value === null ? '待定' : value ? '通过' : '未通过'}
          </span>
        ))
      ) : (
        <span>{point.details.reason ?? '暂无计算结果'}</span>
      )}
    </div>
  )
}

export function ETFHeatPage() {
  const { data, error, loading, reload } = useResource(getETFHeat)
  const [search, setSearch] = useState('')
  const [category, setCategory] = useState('全部')
  const items = data?.items ?? []
  const categories = useMemo(
    () => ['全部', ...new Set(data?.items.map((i) => i.fund.category) ?? [])],
    [data],
  )
  const visible = items.filter(
    (i) =>
      (category === '全部' || category === i.fund.category) &&
      `${i.name}${i.code}${i.fund.target_name}`.toLowerCase().includes(search.trim().toLowerCase()),
  )
  const max = Math.max(0.01, ...items.map((i) => Math.abs(i.score ?? 0)))
  const valid = items.filter((i) => i.score !== null)
  return (
    <div className="page-enter">
      <div className="page-heading">
        <div>
          <span className="section-eyebrow">ETF MOMENTUM / AT THE CLOSE</span>
          <h1>
            ETF动量热度<span className="heading-tag">收盘观察</span>
          </h1>
          <p>穿过不同市场与资产，观察趋势的强弱和持续性。</p>
        </div>
        <div className="heading-actions">
          <span className="date-badge">
            <CalendarDays size={15} />
            {data?.trade_date ?? '暂无记录'}
          </span>
          <button
            className="button button-quiet icon-button"
            aria-label="刷新ETF热度"
            onClick={reload}
            disabled={loading}
          >
            <RefreshCw size={16} />
          </button>
        </div>
      </div>
      {loading ? (
        <LoadingState />
      ) : error ? (
        <ErrorState message={error} retry={reload} />
      ) : !data?.trade_date || !items.length ? (
        <EmptyState
          title="ETF热度尚未到达"
          description="在聚宽运行 ETF 动量热度采集，再通过数据同步更新此模块。"
        />
      ) : (
        <>
          <div className="stats-grid">
            <StatCard
              label="当前候选"
              value={items.length}
              suffix="只"
              note="规模大于30亿元 · 按标的去重"
              icon={<Layers size={18} />}
            />
            <StatCard
              label="正向动量"
              value={items.filter((i) => (i.score ?? 0) > 0).length}
              suffix="只"
              note={`有效计算 ${valid.length} 只`}
              icon={<Flame size={18} />}
            />
            <StatCard
              label="最高热度"
              value={number(valid[0]?.score)}
              note={valid[0]?.name ?? '暂无有效计算'}
              icon={<TrendingUp size={18} />}
              accent
            />
            <StatCard
              label="参考条件通过"
              value={items.filter((i) => i.reference_pass === true).length}
              suffix="只"
              note="收盘口径参考，非买入信号"
              icon={<CheckCircle2 size={18} />}
            />
          </div>
          <section className="panel heatmap-section">
            <div className="panel-heading">
              <div>
                <h2>ETF 热力全景</h2>
                <p>从左上到右下按原始得分递减，点击基金查看走势。</p>
              </div>
              <label className="search-box">
                <Search size={16} />
                <input
                  aria-label="搜索ETF"
                  placeholder="名称、代码或跟踪标的"
                  value={search}
                  onChange={(e) => setSearch(e.target.value)}
                />
              </label>
            </div>
            <div className="etf-heat-filters">
              {categories.map((c) => (
                <button
                  key={c}
                  className={category === c ? 'active' : ''}
                  aria-pressed={category === c}
                  onClick={() => setCategory(c)}
                >
                  {c}
                </button>
              ))}
            </div>
            <div className="etf-heat-legend">
              <span>绿色：负分</span>
              <span>浅色：零分 / 缺少数据</span>
              <span>红色：正分</span>
            </div>
            {visible.length ? (
              <div className="etf-heat-grid">
                {visible.map((i) => {
                  const alpha = 0.08 + 0.48 * Math.sqrt(Math.abs(i.score ?? 0) / max)
                  return (
                    <Link
                      key={i.code}
                      className="etf-heat-tile"
                      style={{
                        background:
                          i.score == null || i.score === 0
                            ? 'var(--surface)'
                            : `rgba(${i.score > 0 ? '204, 74, 51' : '45, 140, 109'},${alpha})`,
                      }}
                      to={`/etf-momentum/funds/${i.code}`}
                      aria-label={`${i.name}，热度${number(i.score)}`}
                    >
                      <div className="etf-tile-meta">
                        <span>
                          #{i.rank ?? '—'} · {i.fund.category}
                        </span>
                        <span>
                          {i.rank_change == null
                            ? '—'
                            : i.rank_change > 0
                              ? `↑${i.rank_change}`
                              : i.rank_change < 0
                                ? `↓${-i.rank_change}`
                                : '持平'}
                        </span>
                      </div>
                      <h3>{i.name}</h3>
                      <strong>{number(i.score)}</strong>
                      <span className="etf-tile-code">{i.code}</span>
                      <small>{condition(i.reference_pass)}</small>
                    </Link>
                  )
                })}
              </div>
            ) : (
              <EmptyState title="没有匹配的基金" description="换个名称或分类试试。" search />
            )}
            <div className="panel-bottom">
              <span>热度 = 年化趋势收益率 × R²；保留负分、零分及高分。</span>
              <span>排名变化按当前候选池与前一记录日比较</span>
            </div>
          </section>
          <div className="etf-method-note">
            历史回补使用当前候选池，不代表历史每天实际入选名单。收盘参考条件包含日成交量检查，与原策略
            11:00 的交易判断不同。规模数据可能有披露延迟，详见基金详情。
          </div>
        </>
      )}
    </div>
  )
}

export function ETFHeatDetailPage() {
  const { code = '' } = useParams()
  const [days, setDays] = useState(365)
  const loader = useCallback(
    (signal: AbortSignal) => getETFHeatHistory(code, days, signal),
    [code, days],
  )
  const { data, error, loading, reload } = useResource(loader)
  const last = data?.points[data.points.length - 1]
  const points = (field: 'score' | 'close') =>
    (data?.points ?? []).map((p) => ({
      date: p.date,
      momentum: p[field],
      rank: null,
      front_stock_count: null,
      industry_stock_count: null,
    }))
  return (
    <div className="page-enter">
      <Link className="back-link" to="/etf-momentum">
        <ArrowLeft size={15} />
        返回ETF热力图
      </Link>
      <div className="page-heading">
        <div>
          <span className="section-eyebrow">ETF TREND / {code}</span>
          <h1>{data?.name ?? 'ETF动量走势'}</h1>
          <p>沿着同一只基金的时间线，观察趋势与热度变化。</p>
        </div>
        <button
          className="button button-quiet icon-button"
          onClick={reload}
          aria-label="刷新ETF趋势"
        >
          <RefreshCw size={16} />
        </button>
      </div>
      {loading ? (
        <LoadingState />
      ) : error ? (
        <ErrorState message={error} retry={reload} />
      ) : data ? (
        <>
          <div className="stats-grid">
            <StatCard
              icon={<Flame size={18} />}
              label="最新热度"
              value={number(last?.score)}
              note={data.end_date}
              accent
            />
            <StatCard
              icon={<TrendingUp size={18} />}
              label="年化趋势收益率"
              value={
                last?.annual_return == null ? '—' : `${(last.annual_return * 100).toFixed(2)}%`
              }
              note="回归斜率推算，并非实际收益率"
            />
            <StatCard
              icon={<CheckCircle2 size={18} />}
              label="趋势拟合 R²"
              value={number(last?.r2)}
              note="沿用原策略的加权公式"
            />
            <StatCard
              icon={<Layers size={18} />}
              label="筛选时规模"
              value={data.fund ? Number(data.fund.size_yi).toFixed(2) : '—'}
              suffix="亿元"
              note={data.fund?.size_date ?? '已不在当前候选池'}
            />
          </div>
          {last && <CheckDetails point={last} />}
          <section className="panel trend-panel">
            <div className="panel-heading">
              <div>
                <h2>动量热度曲线</h2>
                <p>负分正常展示，缺失数据留空，不补零。</p>
              </div>
              <div className="range-switch">
                {[
                  [30, '近1月'],
                  [90, '近3月'],
                  [365, '近1年'],
                ].map(([n, label]) => (
                  <button
                    key={n}
                    className={days === n ? 'active' : ''}
                    aria-pressed={days === n}
                    onClick={() => setDays(Number(n))}
                  >
                    {label}
                  </button>
                ))}
              </div>
            </div>
            <TrendChart points={points('score')} name={data.name} allowNegative precision={4} />
          </section>
          <section className="panel trend-panel">
            <div className="panel-heading">
              <div>
                <h2>复权收盘价</h2>
                <p>各次采集的复权基准可能变化；此图作趋势参考，不拼接计算实际收益。</p>
              </div>
            </div>
            <TrendChart
              points={points('close')}
              name={data.name}
              seriesLabel="复权收盘价"
              precision={4}
            />
          </section>
          <div className="etf-method-note">
            {data.fund
              ? `${data.fund.category} · ${data.fund.target_name} · ${data.fund.size_source}`
              : '该基金已不在当前候选池，历史记录继续保留。'}
            <br />
            历史仅表示当前或曾经候选基金的走势。RSRS
            需要250期历史，上市时间不足时标为待定；参考条件未通过的基金仍保留热度。
          </div>
        </>
      ) : (
        <EmptyState />
      )}
    </div>
  )
}
