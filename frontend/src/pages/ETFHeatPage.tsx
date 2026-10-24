import { PageHeader } from '../components/PageHeader'
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
import '../styles/etfHeat.css'

const number = (n: number | null | undefined, digits = 4) => (n == null ? '—' : n.toFixed(digits))
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
      <PageHeader>
        <div>
          <h1>
            ETF动量热度<span className="heading-tag">收盘观察</span>
          </h1>
          <p>按年化趋势收益率 × R² 排序，比较 ETF / LOF 动量。</p>
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
      </PageHeader>
      {loading ? (
        <LoadingState />
      ) : error ? (
        <ErrorState message={error} retry={reload} />
      ) : !data?.trade_date || !items.length ? (
        <EmptyState
          title="暂无ETF热度数据"
          description="请先在聚宽采集入库，再通过“数据同步”更新本模块。"
        />
      ) : (
        <>
          <div className="stats-grid">
            <StatCard
              label="当前候选"
              value={items.length}
              suffix="只"
              note="规模大于30亿元 · ETF 按标的去重"
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
              label="覆盖类别"
              value={categories.length - 1}
              suffix="类"
              note="ETF 与国内股票／混合 LOF"
              icon={<CheckCircle2 size={18} />}
            />
          </div>
          {valid.length === 0 && (
            <div className="etf-method-note" role="status">
              热度缺失，并非零分。请运行最新版采集脚本补算，再同步本模块。
            </div>
          )}
          <section className="panel heatmap-section">
            <div className="panel-heading">
              <div>
                <h2>ETF 热力全景</h2>
                <p>按热度降序，点击基金查看历史。</p>
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
                      <small>{i.fund.fund_type}</small>
                      {i.score == null && (
                        <small>{i.details.reason ?? '暂无有效热度，请检查采集结果'}</small>
                      )}
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
            历史回补使用当前候选池，不代表当时入选名单。LOF 规模取已披露的份额类别净资产，可能滞后。
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
      <PageHeader>
        <div>
          <span className="page-code">{code}</span>
          <h1>{data?.name ?? 'ETF动量走势'}</h1>
          <p>查看基金热度与收盘价的历史变化。</p>
        </div>
        <button
          className="button button-quiet icon-button"
          onClick={reload}
          aria-label="刷新ETF趋势"
        >
          <RefreshCw size={16} />
        </button>
      </PageHeader>
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
              note="加权回归拟合度"
            />
            <StatCard
              icon={<Layers size={18} />}
              label="筛选时规模"
              value={data.fund ? Number(data.fund.size_yi).toFixed(2) : '—'}
              suffix="亿元"
              note={data.fund?.size_date ?? '已不在当前候选池'}
            />
          </div>

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
                <h2>收盘价（不复权）</h2>
                <p>价格图为不复权收盘价；热度使用后复权价格计算。</p>
              </div>
            </div>
            <TrendChart
              points={points('close')}
              name={data.name}
              seriesLabel="收盘价（不复权）"
              precision={4}
            />
          </section>
          <div className="etf-method-note">
            {data.fund
              ? `${data.fund.category} · ${data.fund.target_name} · ${data.fund.size_source}`
              : '该基金已不在当前候选池，历史记录继续保留。'}
            <br />
            历史仅表示当前或曾经候选基金的走势。热度需要连续26期有效价格，数据不足或当日停牌时留空。
          </div>
        </>
      ) : (
        <EmptyState />
      )}
    </div>
  )
}
