import { useCallback, useState } from 'react'
import { Activity, ArrowUpDown, CalendarDays, Info, RefreshCw, TrendingUp } from 'lucide-react'
import { getBreadthHistory } from '../api/client'
import { useResource } from '../hooks/useResource'
import { formatDate } from '../lib/format'
import { StatCard } from '../components/StatCard'
import { BreadthChart } from '../components/BreadthChart'
import type { BreadthView } from '../components/BreadthChart'
import { EmptyState, ErrorState, LoadingState } from '../components/States'
import '../styles/breadth.css'

const signed = (n: number | null | undefined) =>
  n == null ? '—' : `${n > 0 ? '+' : ''}${n.toFixed(2)}`

export function BreadthPage() {
  const [days, setDays] = useState(180)
  const [period, setPeriod] = useState(5)
  const [view, setView] = useState<BreadthView>('counts')
  const [strengthView, setStrengthView] = useState<BreadthView>('strength')
  const loader = useCallback((signal: AbortSignal) => getBreadthHistory(days, signal), [days])
  const { data, error, loading, reload } = useResource(loader)
  const latest = data?.latest
  return (
    <div className="page-enter breadth-page">
      <div className="page-heading">
        <div>
          <span className="eyebrow">MICROCAP MARKET BREADTH</span>
          <h1>
            微盘新高新低 <span className="heading-tag">宽度观察</span>
          </h1>
          <p>统计微盘 400 在 5、10、20、30 日周期的新高、新低数量。</p>
        </div>
        <div className="heading-actions">
          <span className="date-badge">
            <CalendarDays size={15} />
            最新记录 {formatDate(latest?.date)}
          </span>
          <button
            className="button button-quiet icon-button"
            onClick={reload}
            disabled={loading}
            aria-label="刷新微盘新高新低"
          >
            <RefreshCw size={16} className={loading ? 'spin' : ''} />
          </button>
        </div>
      </div>
      {loading ? (
        <LoadingState />
      ) : error ? (
        <ErrorState message={error} retry={reload} />
      ) : !latest || !data ? (
        <EmptyState
          title="暂无微盘新高新低数据"
          description="请先在聚宽采集入库，再通过“数据同步”更新本模块。"
        />
      ) : (
        <>
          <div className="stats-grid">
            <StatCard
              label="加权市场强度"
              value={signed(latest.strength)}
              suffix="点"
              note={latest.market_state + ' · 短周期权重更高'}
              icon={<Activity size={18} />}
              accent
            />
            <StatCard
              label="每日动态样本"
              value={latest.sample_count}
              suffix="只"
              note="当日满足条件的最小市值股票"
              icon={<ArrowUpDown size={18} />}
            />
            <StatCard
              label="较 6 个采集交易日前"
              value={signed(latest.change_6d)}
              suffix="点"
              note="市场强度的点数变化"
              icon={<TrendingUp size={18} />}
            />
            <StatCard
              label="较 12 个采集交易日前"
              value={signed(latest.change_12d)}
              suffix="点"
              note="样本不足时显示 —"
              icon={<TrendingUp size={18} />}
            />
          </div>
          <section className="breadth-periods" aria-label="选择新高新低周期">
            {latest.periods.map((m) => (
              <button
                key={m.period}
                className={`breadth-period ${period === m.period ? 'selected' : ''}`}
                aria-pressed={period === m.period}
                onClick={() => setPeriod(m.period)}
              >
                <span className="breadth-period-title">
                  {m.period} 日观察 <small>比例差 {signed(m.spread_pp)} 点</small>
                </span>
                <span className="breadth-pair">
                  <span className="breadth-high">
                    <small>新高</small>
                    <strong>
                      {m.high_count}
                      <i>只</i>
                    </strong>
                    <em>{m.high_ratio.toFixed(1)}%</em>
                  </span>
                  <span className="breadth-low">
                    <small>新低</small>
                    <strong>
                      {m.low_count}
                      <i>只</i>
                    </strong>
                    <em>{m.low_ratio.toFixed(1)}%</em>
                  </span>
                </span>
              </button>
            ))}
          </section>
          <section className="panel trend-panel">
            <div className="panel-heading">
              <div>
                <span className="section-eyebrow">HIGHS & LOWS</span>
                <h2>{period} 日新高新低趋势</h2>
                <p>
                  {formatDate(data.start_date)} — {formatDate(data.end_date)}
                </p>
              </div>
              <div className="range-switch" aria-label="新高新低历史范围">
                {[30, 90, 180, 300].map((n) => (
                  <button
                    key={n}
                    className={days === n ? 'active' : ''}
                    aria-pressed={days === n}
                    onClick={() => setDays(n)}
                  >
                    近 {n} 日
                  </button>
                ))}
              </div>
            </div>
            <div className="microcap-view-switch" aria-label="新高新低指标">
              {(
                [
                  ['counts', '股票数量'],
                  ['ratios', '样本占比'],
                  ['spread', '新高－新低差'],
                ] as const
              ).map(([key, label]) => (
                <button
                  key={key}
                  className={view === key ? 'active' : ''}
                  aria-pressed={view === key}
                  onClick={() => setView(key)}
                >
                  {label}
                </button>
              ))}
            </div>
            <BreadthChart points={data.points} period={period} view={view} />
            <div className="panel-bottom">
              <span>{data.observed_days} 个已采集交易日 · 拖动滑块缩放</span>
              <span>新高与新低可同时成立，不作互斥统计</span>
            </div>
          </section>
          <div className="breadth-secondary">
            <section className="panel trend-panel">
              <div className="panel-heading">
                <div>
                  <span className="section-eyebrow">MARKET STRENGTH</span>
                  <h2>市场强度趋势</h2>
                </div>
              </div>
              <div className="microcap-view-switch">
                {(
                  [
                    ['strength', '加权强度'],
                    ['sentiment', '情绪加权值'],
                  ] as const
                ).map(([key, label]) => (
                  <button
                    key={key}
                    className={strengthView === key ? 'active' : ''}
                    aria-pressed={strengthView === key}
                    onClick={() => setStrengthView(key)}
                  >
                    {label}
                  </button>
                ))}
              </div>
              <BreadthChart points={data.points} period={period} view={strengthView} />
            </section>
            <section className="panel breadth-dominance">
              <span className="section-eyebrow">BREADTH PERSISTENCE</span>
              <h2>新高占优的持续性</h2>
              <p>近 15 个已采集交易日中新高数量大于新低数量的天数比例。</p>
              {latest.periods.map((m) => (
                <div className="breadth-dominance-row" key={m.period}>
                  <div>
                    <strong>{m.period} 日周期</strong>
                    <span>
                      {m.dominant_days} / {m.observed_days} 天 · {m.dominance_pct.toFixed(1)}%
                    </span>
                  </div>
                  <div
                    className="breadth-progress"
                    role="img"
                    aria-label={`${m.period}日周期新高占优 ${m.dominance_pct.toFixed(1)}%`}
                  >
                    <span style={{ width: `${m.dominance_pct}%` }} />
                  </div>
                </div>
              ))}
              <small>表示行情宽度的持续性，不是交易胜率。</small>
            </section>
          </div>
          <section className="panel trend-panel">
            <div className="panel-heading">
              <div>
                <h2>最近记录 · {period} 日周期</h2>
                <p>最近 10 条，按日期倒序。</p>
              </div>
            </div>
            <div className="breadth-table-wrap">
              <table className="breadth-table">
                <thead>
                  <tr>
                    <th>日期</th>
                    <th>新高 / 只</th>
                    <th>新低 / 只</th>
                    <th>比例差 / 点</th>
                    <th>样本 / 只</th>
                  </tr>
                </thead>
                <tbody>
                  {data.points
                    .slice(-10)
                    .reverse()
                    .map((p) => {
                      const m = p.periods.find((m) => m.period === period)!
                      return (
                        <tr key={p.date}>
                          <td>{p.date}</td>
                          <td className="breadth-high">{m.high_count}</td>
                          <td className="breadth-low">{m.low_count}</td>
                          <td>{signed(m.spread_pp)}</td>
                          <td>{p.sample_count}</td>
                        </tr>
                      )
                    })}
                </tbody>
              </table>
            </div>
          </section>
          <section className="method-card">
            <span className="method-icon">
              <Info size={19} />
            </span>
            <div>
              <h3>统计口径</h3>
              <p>
                剔除 68、4、8 开头、ST 及上市不超过 240 个自然日的股票，取总市值最小的 400 只。
                当日收盘价与此前 N 个交易日收盘价比较：不低于最高价计新高，不高于最低价计新低。
                使用前复权价格及停牌填充值，持平可能同时计入两侧。
              </p>
              <span>
                强度按四个周期的新高与新低比例差加权，权重依次为 48%、24%、16%、12%。
                情绪值按 exp(−N/20) 加权求和，不归一化。变化以点数差表示；历史范围按已采集交易日，缺失不补零。
              </span>
            </div>
          </section>
        </>
      )}
    </div>
  )
}
