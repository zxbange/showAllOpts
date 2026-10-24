import { useCallback, useState } from 'react'
import { CalendarDays, RefreshCw, Users, Thermometer, TrendingUp, Snowflake } from 'lucide-react'
import { getSentimentHistory } from '../api/client'
import { useResource } from '../hooks/useResource'
import { PageHeader } from '../components/PageHeader'
import { StatCard } from '../components/StatCard'
import { SentimentChart } from '../components/SentimentChart'
import { EmptyState, ErrorState } from '../components/States'
import { formatDate } from '../lib/format'
import '../styles/sentiment.css'

const percent = (value: number | null | undefined) =>
  value == null ? '—' : (value * 100).toFixed(2) + '%'
const number = (value: number | undefined) => (value == null ? '—' : value.toLocaleString('zh-CN'))
const states = { cold: '冰点', normal: '正常区间', hot: '过热' }

export function SentimentPage() {
  const [days, setDays] = useState(0)
  const loader = useCallback((signal: AbortSignal) => getSentimentHistory(days, signal), [days])
  const { data, error, loading, reload } = useResource(loader)
  const latest = data?.latest
  return (
    <div className="page-enter sentiment-page">
      <PageHeader>
        <div>
          <h1>
            市场情绪 <span className="heading-tag">市场情绪跟踪</span>
          </h1>
          <p>全市场站上20日均线的股票比例：低于20%为冰点，高于80%为过热。</p>
        </div>
        <div className="heading-actions">
          <span className="date-badge">
            <CalendarDays size={15} />
            截至 {formatDate(latest?.date)}
          </span>
          <button
            className="button button-quiet icon-button"
            aria-label="刷新市场情绪数据"
            onClick={reload}
            disabled={loading}
          >
            <RefreshCw size={16} className={loading ? 'spin' : ''} />
          </button>
        </div>
      </PageHeader>
      {error ? (
        <ErrorState message={error} retry={reload} />
      ) : (
        <>
          <div className="stats-grid" aria-busy={loading}>
            <StatCard
              label="站上20日均线比例"
              value={percent(latest?.ratio)}
              note={latest ? states[latest.state] : '等待数据'}
              icon={<Thermometer size={18} />}
              accent
            />
            <StatCard
              label="站上均线股票"
              value={number(latest?.above_count)}
              suffix="只"
              note={'有效样本 ' + number(latest?.eligible_count) + ' 只'}
              icon={<Users size={18} />}
            />
            <StatCard
              label="区间最低比例"
              value={percent(data?.summary.low_ratio)}
              note={'区间最高 ' + percent(data?.summary.peak_ratio)}
              icon={<TrendingUp size={18} />}
            />
            <StatCard
              label="区间冰点天数"
              value={number(data?.summary.cold_days)}
              suffix="天"
              note={
                '过热 ' +
                number(data?.summary.hot_days) +
                ' 天 · 有效记录 ' +
                number(data?.summary.observed_days) +
                ' 天'
              }
              icon={<Snowflake size={18} />}
            />
          </div>
        </>
      )}
      <section className="panel trend-panel" aria-busy={loading}>
        <div className="panel-heading">
          <div>
            <div className="panel-title">
              <span className="status-dot" />
              <h2>全市场情绪走势</h2>
              {latest && (
                <span className={'sentiment-state sentiment-' + latest.state}>
                  {states[latest.state]}
                </span>
              )}
            </div>
            <p>
              {data?.start_date
                ? `${data.start_date} — ${data.end_date}`
                : '按交易日记录 · 保留最近3年'}
            </p>
          </div>
          <div className="range-switch" aria-label="市场情绪历史范围">
            {[
              { days: 30, label: '近1月' },
              { days: 90, label: '近3月' },
              { days: 180, label: '近半年' },
              { days: 365, label: '近1年' },
              { days: 0, label: '近3年' },
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
        ) : error ? null : latest && data ? (
          <>
            <SentimentChart points={data.points} />
            <div className="panel-bottom">
              <span>拖动底部滑块缩放区间 · 缺失交易日不补零</span>
              <span>{data.summary.observed_days} 个交易日</span>
            </div>
          </>
        ) : (
          <EmptyState
            title="暂无市场情绪数据"
            description="请先在聚宽运行市场情绪采集，再通过“数据同步”更新“市场情绪跟踪”。"
          />
        )}
      </section>
      <div className="method-card">
        <div>
          <h3>统计口径</h3>
          <p>
            收盘价严格高于当日 MA20 的股票数 ÷ 有效股票数。包含
            ST，剔除当日停牌及不足20日数据的股票；20%和80%本身属于正常区间。
          </p>
          <span>
            按历史上市、退市日期还原聚宽覆盖的 A
            股；MA20采用前复权收盘价，含当日，历史停牌日沿用前收盘价。
          </span>
        </div>
      </div>
    </div>
  )
}
