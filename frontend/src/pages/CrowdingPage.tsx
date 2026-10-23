import { useCallback, useState } from 'react'
import {
  CalendarDays,
  ChartNoAxesCombined,
  Info,
  RefreshCw,
  TrendingUp,
  Trophy,
} from 'lucide-react'
import { getCrowdingHistory } from '../api/client'
import { useResource } from '../hooks/useResource'
import { formatDate } from '../lib/format'
import { CrowdingChart } from '../components/CrowdingChart'
import { StatCard } from '../components/StatCard'
import { EmptyState, ErrorState, LoadingState } from '../components/States'

const percent = (value: number | null | undefined) =>
  value == null ? '—' : (value * 100).toFixed(2) + '%'

export function CrowdingPage() {
  const [days, setDays] = useState(365)
  const [showSmooth, setShowSmooth] = useState(true)
  const loader = useCallback((signal: AbortSignal) => getCrowdingHistory(days, signal), [days])
  const { data, error, loading, reload } = useResource(loader)
  const latest = data?.latest
  return (
    <div className="page-enter crowding-page">
      <div className="page-heading">
        <div>
          <span className="section-eyebrow">MARKET CROWDING / TOP 5%</span>
          <h1>
            抱团拥挤度<span className="heading-tag">成交额集中度</span>
          </h1>
          <p>成交额前 5% 股票占全市场成交额的比例。</p>
        </div>
        <div className="heading-actions">
          <span className="date-badge">
            <CalendarDays size={15} />
            截至 {formatDate(latest?.date)}
          </span>
          <button
            className="button button-quiet icon-button"
            onClick={reload}
            disabled={loading}
            aria-label="刷新拥挤度数据"
          >
            <RefreshCw size={16} className={loading ? 'spin' : ''} />
          </button>
        </div>
      </div>
      {loading ? (
        <LoadingState />
      ) : error ? (
        <ErrorState message={error} retry={reload} />
      ) : data && !latest ? (
        <EmptyState
          title="暂无拥挤度数据"
          description="请先在聚宽采集入库，再通过“数据同步”更新本模块。"
        />
      ) : data && latest ? (
        <>
          <div className="stats-grid">
            <StatCard
              label="最新成交额占比"
              value={percent(latest.ratio)}
              note={`前 ${latest.top_stock_count} / ${latest.stock_count} 只股票 · ${formatDate(latest.date)}`}
              icon={<ChartNoAxesCombined size={18} />}
              accent
            />
            <StatCard
              label="区间最高占比"
              value={percent(data.summary.peak_ratio)}
              note={`区间最低 ${percent(data.summary.low_ratio)}`}
              icon={<Trophy size={18} />}
            />
            <StatCard
              label="区间平均占比"
              value={percent(data.summary.average_ratio)}
              note={`基于 ${data.summary.observed_days} 个交易日原始值`}
              icon={<ChartNoAxesCombined size={18} />}
            />
            <StatCard
              label="区间变化"
              value={
                data.summary.change_pp == null
                  ? '—'
                  : `${data.summary.change_pp > 0 ? '+' : ''}${data.summary.change_pp.toFixed(2)}`
              }
              note="个百分点 · 期末占比 − 期初占比"
              icon={<TrendingUp size={18} />}
            />
          </div>
          <div className="crowding-share panel">
            <div>
              <span className="section-eyebrow">LATEST CONCENTRATION</span>
              <h2>前 5% 股票，贡献 {percent(latest.ratio)} 的成交额</h2>
            </div>
            <div
              className="concentration-track"
              role="img"
              aria-label={`前百分之五股票成交额占比 ${percent(latest.ratio)}`}
            >
              <span style={{ width: `${latest.ratio * 100}%` }} />
            </div>
            <div className="concentration-labels">
              <span>前 5% · {(latest.top_money / 1e8).toFixed(2)} 亿元</span>
              <span>全市场 · {(latest.total_money / 1e8).toFixed(2)} 亿元</span>
            </div>
          </div>
        </>
      ) : null}
      <section className="panel trend-panel">
        <div className="panel-heading">
          <div>
            <div className="panel-title">
              <span className="status-dot" />
              <h2>成交额集中度走势</h2>
            </div>
            <p>
              {data?.start_date
                ? `${formatDate(data.start_date)} — ${formatDate(data.end_date)}`
                : '按交易日记录 · 从 2020 年起采集'}
            </p>
          </div>
          <div className="range-switch" aria-label="拥挤度历史范围">
            {[
              { days: 30, label: '近 1 月' },
              { days: 90, label: '近 3 月' },
              { days: 180, label: '近半年' },
              { days: 365, label: '近 1 年' },
              { days: 0, label: '全部' },
            ].map((range) => (
              <button
                key={range.days}
                aria-pressed={days === range.days}
                className={days === range.days ? 'active' : ''}
                onClick={() => setDays(range.days)}
              >
                {range.label}
              </button>
            ))}
          </div>
        </div>
        {loading ? (
          <div className="skeleton chart-skeleton" />
        ) : !error && data && latest ? (
          <>
            <label className="crowding-toggle">
              <input
                type="checkbox"
                checked={showSmooth}
                onChange={(event) => setShowSmooth(event.target.checked)}
              />
              显示 EMA 10 平滑参考线
            </label>
            <CrowdingChart data={data} showSmooth={showSmooth} />
            <div className="panel-bottom">
              <span>拖动底部滑块，聚焦一段趋势</span>
              <span>{data.summary.observed_days} 个有效数据点</span>
            </div>
          </>
        ) : null}
      </section>
      <div className="method-card crowding-method">
        <span className="method-icon">
          <Info size={19} />
        </span>
        <div>
          <h3>指标口径</h3>
          <p>
            股票按成交额降序，取数量前 5%（向下取整），计算其成交额占比。占比越高，成交越集中，不代表涨跌方向。
          </p>
          <span>
            停牌股仍计入股票总数；卡片显示原始值，EMA 10 仅作平滑参考。时间范围按自然日，缺失日不补零。
          </span>
        </div>
      </div>
    </div>
  )
}
