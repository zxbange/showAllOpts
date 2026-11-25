import { HistoryRangeSwitch, standardHistoryRanges } from '../components/HistoryRangeSwitch'
import { DisplayRefreshButton } from '../components/PageActionButton'
import { PageHeader } from '../components/PageHeader'
import { useCallback, useState } from 'react'
import { ArrowLeft, ChartNoAxesCombined, CircleDot, Trophy, TrendingUp } from 'lucide-react'
import { Link, useParams, useSearchParams } from 'react-router-dom'
import { getHistory } from '../api/client'
import { useResource } from '../hooks/useResource'
import { displayName, formatDate, formatScore, signedScore } from '../lib/format'
import { TrendChart } from '../components/TrendChart'
import { StatCard } from '../components/StatCard'
import { EmptyState, ErrorState, LoadingState } from '../components/States'

export function IndustryPage() {
  const { code = '' } = useParams()
  const [query] = useSearchParams()
  const end = query.get('date')
  const [days, setDays] = useState(365)
  const loader = useCallback(
    (signal: AbortSignal) => getHistory(code, days, end, signal),
    [code, days, end],
  )
  const { data, error, loading, reload } = useResource(loader)
  const name = data ? displayName(data.name) : '行业趋势'
  return (
    <div className="page-enter">
      <Link className="back-link" to="/industry-momentum">
        <ArrowLeft size={15} />
        返回行业热力图
      </Link>
      <PageHeader dataUpdatedAt={data?.updated_at}>
        <div>
          <span className="page-code">{code}</span>
          <h1>
            {name}
            <span className="heading-tag">动量趋势</span>
          </h1>
          <p>查看行业动量、排名及强势股数量的历史变化。</p>
        </div>
        <div className="heading-actions">
          <DisplayRefreshButton ariaLabel="刷新趋势数据" onClick={reload} loading={loading} />
        </div>
      </PageHeader>
      {loading ? (
        <LoadingState />
      ) : error ? (
        <ErrorState message={error} retry={reload} />
      ) : (
        data && (
          <>
            <div className="stats-grid">
              <StatCard
                label="最新热力值"
                value={formatScore(data.summary.latest_momentum)}
                note={`记录日期 ${formatDate(data.summary.latest_date)}`}
                icon={<FlameIcon />}
                accent
              />
              <StatCard
                label="区间最高值"
                value={formatScore(data.summary.peak_momentum)}
                note={`出现在 ${formatDate(data.summary.peak_date)}`}
                icon={<Trophy size={18} />}
              />
              <StatCard
                label="区间平均值"
                value={formatScore(data.summary.average_momentum)}
                note={`基于 ${data.summary.observed_days} 个有效数据点`}
                icon={<ChartNoAxesCombined size={18} />}
              />
              <StatCard
                label="区间变化"
                value={signedScore(data.summary.change)}
                note="期末值 − 期初值（非收益率）"
                icon={<TrendingUp size={18} />}
              />
            </div>
          </>
        )
      )}
      <section className="panel trend-panel">
        <div className="panel-heading">
          <div>
            <div className="panel-title">
              <span className="status-dot" />
              <h2>热力值走势</h2>
            </div>
            <p>
              {data
                ? `${formatDate(data.start_date)} — ${formatDate(data.end_date)}`
                : '历史动量表现'}
              <span className="chart-subtitle">日度数据</span>
            </p>
          </div>
          <HistoryRangeSwitch
            label="历史数据范围"
            options={standardHistoryRanges.slice(0, 4)}
            value={days}
            onChange={setDays}
          />
        </div>
        {loading ? (
          <div className="skeleton chart-skeleton" />
        ) : data?.summary.observed_days ? (
          <>
            <div className="chart-legend">
              <span />
              <b>{name}</b>
              <span className="legend-extra">动量热力值</span>
            </div>
            <TrendChart points={data.points} name={name} />
            <div className="panel-bottom">
              <span>
                <CircleDot size={13} />
                拖动底部滑块，聚焦一段趋势
              </span>
              <span>
                {data.summary.observed_days} 个有效数据点
                {data.summary.missing_days ? ` · ${data.summary.missing_days} 个缺失记录` : ''}
              </span>
            </div>
          </>
        ) : (
          !error && (
            <EmptyState
              title="这个时间段暂无记录"
              description="可以切换更长的时间范围，查看已有历史数据。"
            />
          )
        )}
      </section>
      {data && (
        <section className="detail-bottom">
          <details className="method-card">
            <summary>计算口径</summary>
            <div>
              <p>
                动量 = 20 个交易日涨幅超过 20% 的股票数² ÷
                行业成分股总数，衡量强势股集中度，受行业规模影响。
              </p>
              <span>缺失记录留空，不补零。</span>
            </div>
          </details>
          <div className="observation-card">
            <strong>
              {data.summary.observed_days}
              <small>个数据点</small>
            </strong>
            <p>最近 {days} 个自然日 · 截至最新快照日期</p>
          </div>
        </section>
      )}
    </div>
  )
}

function FlameIcon() {
  return <CircleDot size={18} />
}
