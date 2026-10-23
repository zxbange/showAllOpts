import { useMemo, useState } from 'react'
import {
  ArrowDownRight,
  ArrowUpRight,
  CalendarDays,
  ChevronRight,
  Flame,
  Grid2X2,
  Info,
  RefreshCw,
  Search,
  TrendingUp,
  X,
} from 'lucide-react'
import { Link } from 'react-router-dom'
import { getSnapshot } from '../api/client'
import { useResource } from '../hooks/useResource'
import {
  displayName,
  formatDate,
  formatScore,
  formatTimestamp,
  signedScore,
  sortIndustries,
} from '../lib/format'
import { Heatmap } from '../components/Heatmap'
import { StatCard } from '../components/StatCard'
import { EmptyState, ErrorState, LoadingState } from '../components/States'

export function MomentumPage() {
  const { data, error, loading, reload } = useResource(getSnapshot)
  const [search, setSearch] = useState('')
  const industries = useMemo(() => sortIndustries(data?.industries ?? []), [data])
  const visible = industries.filter((i) =>
    `${i.name}${i.code}`.toLowerCase().includes(search.trim().toLowerCase()),
  )
  const leader = industries[0]
  return (
    <div className="page-enter">
      <div className="page-heading">
        <div>
          <span className="section-eyebrow">INDUSTRY MOMENTUM</span>
          <h1>
            行业动量<span className="heading-tag">申万二级</span>
          </h1>
          <p>按申万二级行业比较强势股集中度。</p>
        </div>
        <div className="heading-actions">
          <span className="date-badge">
            <CalendarDays size={15} />
            {formatDate(data?.trade_date)}
          </span>
          <button
            className="button button-quiet icon-button"
            onClick={reload}
            disabled={loading}
            aria-label="刷新行业数据"
          >
            <RefreshCw size={16} className={loading ? 'spin' : ''} />
          </button>
        </div>
      </div>
      {loading ? (
        <LoadingState />
      ) : error ? (
        <ErrorState message={error} retry={reload} />
      ) : !data?.trade_date || !industries.length ? (
        <EmptyState />
      ) : (
        <>
          <div className="stats-grid">
            <StatCard
              label="覆盖行业"
              value={data.summary.industry_count}
              suffix="个"
              note="全行业覆盖 · 包含零分行业"
              icon={<Grid2X2 size={18} />}
            />
            <StatCard
              label="活跃行业"
              value={data.summary.active_count}
              suffix="个"
              note={
                <>
                  <span className="text-accent">
                    {((data.summary.active_count / data.summary.industry_count) * 100).toFixed(1)}%
                  </span>{' '}
                  的行业动量大于零
                </>
              }
              icon={<Flame size={18} />}
            />
            <StatCard
              label="平均热力值"
              value={formatScore(data.summary.average_momentum)}
              note="当日全部行业的平均动量"
              icon={<TrendingUp size={18} />}
            />
            <StatCard
              label="最高热力值"
              value={formatScore(data.summary.max_momentum)}
              note={leader ? `${displayName(leader.name)} · 当日排名第 1` : '—'}
              icon={<ArrowUpRight size={18} />}
              accent
            />
          </div>
          <section className="heatmap-section panel">
            <div className="panel-heading">
              <div>
                <div className="panel-title">
                  <span className="status-dot" />
                  <h2>市场热力全景</h2>
                  <span className="subtle-count">
                    {visible.length} / {industries.length}
                  </span>
                </div>
                <p>按热力值降序，点击行业查看历史。</p>
              </div>
              <label className="search-box">
                <Search size={16} />
                <input
                  aria-label="搜索行业名称或代码"
                  placeholder="搜索行业名称或代码"
                  value={search}
                  onChange={(e) => setSearch(e.target.value)}
                />
                {search && (
                  <button aria-label="清空搜索" onClick={() => setSearch('')}>
                    <X size={14} />
                  </button>
                )}
              </label>
            </div>
            <div className="heatmap-meta">
              <span>
                <Grid2X2 size={13} />
                按热力值降序排列
              </span>
              <div className="heat-legend">
                <span>低</span>
                <i />
                <span>高</span>
                <span className="legend-type">对数色阶</span>
              </div>
            </div>
            {visible.length ? (
              <Heatmap
                industries={visible}
                maximum={data.summary.max_momentum}
                date={data.trade_date}
              />
            ) : (
              <EmptyState title="没有匹配的行业" description="试试其他行业名称或代码。" search />
            )}
            <div className="panel-bottom">
              <span>
                <Info size={13} />
                热力值 = 达标股票数² ÷ 行业成分股数
              </span>
              <span>数据写入时间 {formatTimestamp(data.updated_at)}</span>
            </div>
          </section>
          <section className="leaders-section">
            <div className="section-heading">
              <div>
                <span className="section-eyebrow">IN THE SPOTLIGHT</span>
                <h2>当前领跑行业</h2>
              </div>
              <span className="subtle-tag">变化对比 {formatDate(data.previous_trade_date)}</span>
            </div>
            <div className="leader-grid">
              {industries.slice(0, 3).map((industry, index) => (
                <Link
                  className="leader-card"
                  to={`/industry-momentum/industries/${industry.code}?date=${data.trade_date}`}
                  key={industry.code}
                >
                  <span className="leader-rank">0{index + 1}</span>
                  <div>
                    <h3>{displayName(industry.name)}</h3>
                    <span>
                      {industry.front_stock_count} / {industry.industry_stock_count} 只股票达标
                    </span>
                  </div>
                  <div className="leader-score">
                    <strong>{formatScore(industry.momentum)}</strong>
                    <small className={(industry.change ?? 0) >= 0 ? 'text-warm' : 'text-accent'}>
                      {industry.change !== null &&
                        (industry.change >= 0 ? (
                          <ArrowUpRight size={12} />
                        ) : (
                          <ArrowDownRight size={12} />
                        ))}
                      {signedScore(industry.change)}
                    </small>
                  </div>
                  <ChevronRight size={15} />
                </Link>
              ))}
            </div>
          </section>
          <p className="data-note">
            <Info size={13} />
            热力值衡量强势股集中度，不代表行业收益率。
          </p>
        </>
      )}
    </div>
  )
}
