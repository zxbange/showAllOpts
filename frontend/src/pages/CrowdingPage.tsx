import { PageHeader } from '../components/PageHeader'
import { useCallback, useState } from 'react'
import { CalendarDays, ChartNoAxesCombined, RefreshCw, TrendingUp, Trophy } from 'lucide-react'
import { getCrowdingHistory } from '../api/client'
import { useResource } from '../hooks/useResource'
import { formatDate } from '../lib/format'
import { CrowdingChart } from '../components/CrowdingChart'
import { MarketCollectionPanel } from '../components/MarketCollectionPanel'
import { StatCard } from '../components/StatCard'
import { EmptyState, ErrorState, LoadingState } from '../components/States'
import { crowdingSourceLabel, marketSourceSegments } from '../lib/marketCollection'

const percent = (value: number | null | undefined) =>
  value == null ? '—' : (value * 100).toFixed(2) + '%'

export function CrowdingPage() {
  const [days, setDays] = useState(365)
  const [showSmooth, setShowSmooth] = useState(true)
  const loader = useCallback((signal: AbortSignal) => getCrowdingHistory(days, signal), [days])
  const { data, error, loading, reload } = useResource(loader)
  const latest = data?.latest
  const mixedSources = marketSourceSegments(data?.points ?? [], 'crowding').length > 1
  return (
    <div className="page-enter crowding-page">
      <PageHeader>
        <div>
          <h1>
            市场情绪<span className="heading-tag">抱团拥挤度</span>
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
      </PageHeader>
      {loading ? (
        <LoadingState />
      ) : error ? (
        <ErrorState message={error} retry={reload} />
      ) : data && !latest ? (
        <EmptyState
          title="暂无拥挤度数据"
          description="请查看“市场日采集”中的配置与任务状态。市场热度与抱团拥挤度共用一次日采集，完成后自动显示数据。"
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
              value={mixedSources ? '—' : percent(data.summary.average_ratio)}
              note={
                mixedSources
                  ? '区间包含不同来源，分别查看各段趋势'
                  : `基于 ${data.summary.observed_days} 个交易日原始值`
              }
              icon={<ChartNoAxesCombined size={18} />}
            />
            <StatCard
              label="区间变化"
              value={
                mixedSources || data.summary.change_pp == null
                  ? '—'
                  : `${data.summary.change_pp > 0 ? '+' : ''}${data.summary.change_pp.toFixed(2)}`
              }
              note={mixedSources ? '跨来源区间不计算整体变化' : '个百分点 · 期末占比 − 期初占比'}
              icon={<TrendingUp size={18} />}
            />
          </div>
          <div className="crowding-share panel">
            <div>
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
      {latest && (
        <p className="market-source-note">
          最新记录：<strong>{crowdingSourceLabel(latest)}</strong>
          {mixedSources && '。旧历史与公开行情日采集按来源分段，EMA 10 从各段分别起算。'}
        </p>
      )}
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
                : '按交易日记录 · 已有历史保留'}
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
      <details className="method-card crowding-method">
        <summary>指标口径</summary>
        <div>
          <p>
            股票按成交额降序，取数量前
            5%（向下取整），计算其成交额占比。占比越高，成交越集中，不代表涨跌方向。
          </p>
          <span>
            停牌股仍计入股票总数；卡片显示原始值，EMA 10
            仅作平滑参考。时间范围按自然日，缺失日不补零。
          </span>
          <p className="market-collection-note">
            公开行情日采集采用聚宽原脚本的前5%成交额占比算法；行情来源不同，旧聚宽历史保持原值，来源切换处分段显示。
          </p>
        </div>
      </details>
      <MarketCollectionPanel onCollected={reload} />
    </div>
  )
}
