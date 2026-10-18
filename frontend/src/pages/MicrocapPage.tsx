import { useCallback, useState } from 'react'
import {
  CalendarDays,
  ChartNoAxesCombined,
  Info,
  Layers3,
  RefreshCw,
  Thermometer,
  TrendingUp,
} from 'lucide-react'
import { getMicrocapHistory } from '../api/client'
import { useResource } from '../hooks/useResource'
import { formatDate, formatScore, signedScore } from '../lib/format'
import { MicrocapChart, temperatureViews } from '../components/MicrocapChart'
import type { TemperatureView } from '../components/MicrocapChart'
import { StatCard } from '../components/StatCard'
import { EmptyState, ErrorState, LoadingState } from '../components/States'

const percent = (value: number | null | undefined) =>
  value == null ? '—' : (value * 100).toFixed(1) + '%'

export function MicrocapPage() {
  const [days, setDays] = useState(365)
  const [view, setView] = useState<TemperatureView>('score')
  const [overlay, setOverlay] = useState(true)
  const loader = useCallback((signal: AbortSignal) => getMicrocapHistory(days, signal), [days])
  const { data, error, loading, reload } = useResource(loader)
  const latest = data?.latest
  return (
    <div className="page-enter microcap-page">
      <div className="page-heading">
        <div>
          <span className="section-eyebrow">MICROCAP THERMOMETER / 1000</span>
          <h1>
            微盘温度计<span className="heading-tag">估值 · 交易 · 温度</span>
          </h1>
          <p>从微盘股的估值与交易集中度，观察温度变化和等权指数走势。</p>
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
            aria-label="刷新微盘温度数据"
          >
            <RefreshCw size={16} className={loading ? 'spin' : ''} />
          </button>
        </div>
      </div>
      {loading ? (
        <LoadingState />
      ) : error ? (
        <ErrorState message={error} retry={reload} />
      ) : !latest ? (
        <EmptyState
          title="暂无微盘温度数据"
          description="请先在聚宽运行微盘温度计_数据库版.py，写入同一数据库后，在「数据同步」中更新本地快照。"
        />
      ) : (
        data && (
          <>
            <div className="stats-grid">
              <StatCard
                label="综合温度"
                value={(latest.score * 100).toFixed(1)}
                note={`累计 ${latest.sample_count} 期历史样本 · 满分 100`}
                icon={<Thermometer size={18} />}
                accent
              />
              <StatCard
                label="微盘 1000 等权指数"
                value={formatScore(latest.micro_index)}
                note="首期基准 1000 · 每期等权调整"
                icon={<TrendingUp size={18} />}
              />
              <StatCard
                label="微盘市值中位数"
                value={formatScore(latest.micro_median)}
                note={`亿元 · 对照组 ${formatScore(latest.all_median)} 亿元`}
                icon={<Layers3 size={18} />}
              />
              <StatCard
                label="换手率拥挤度"
                value={formatScore(latest.crowding_ratio)}
                note="微盘 / 对照组的近 5 日平均换手率"
                icon={<ChartNoAxesCombined size={18} />}
              />
            </div>
            <section className="panel microcap-thermometer">
              <div className="temperature-heading">
                <div>
                  <span className="section-eyebrow">TEMPERATURE COMPOSITION</span>
                  <h2>温度来自哪里</h2>
                </div>
                <span>每 {latest.sample_step} 个交易日采样</span>
              </div>
              <div
                className="temperature-track"
                role="img"
                aria-label={`综合温度 ${(latest.score * 100).toFixed(1)} 分`}
              >
                <span style={{ left: `${latest.score * 100}%` }} />
              </div>
              <div className="temperature-scale">
                <span>0</span>
                <span>20</span>
                <span>50</span>
                <span>80</span>
                <span>100</span>
              </div>
              <div className="temperature-factors">
                {[
                  {
                    label: '相对估值分位',
                    value: latest.relative_pct,
                    note: '权重 50%',
                    raw: `市值中位数比 ${latest.relative_ratio.toFixed(3)}`,
                  },
                  {
                    label: '换手拥挤度分位',
                    value: latest.crowding_pct,
                    note: '权重 25%',
                    raw: `换手率比 ${latest.crowding_ratio.toFixed(3)}`,
                  },
                  {
                    label: '成交额拥挤度分位',
                    value: latest.amount_crowding_pct,
                    note: '权重 25%',
                    raw: `成交额占比 ${percent(latest.amount_crowding_ratio)}`,
                  },
                  {
                    label: '市值分位',
                    value: latest.cap_pct,
                    note: '独立观察 · 不计入温度',
                    raw: `微盘市值中位数 ${latest.micro_median.toFixed(2)} 亿元`,
                  },
                ].map((item) => (
                  <div key={item.label}>
                    <span>{item.label}</span>
                    <strong>{percent(item.value)}</strong>
                    <small>{item.note}</small>
                    <small>{item.raw}</small>
                  </div>
                ))}
              </div>
            </section>
          </>
        )
      )}
      <section className="panel trend-panel">
        <div className="panel-heading">
          <div>
            <div className="panel-title">
              <span className="status-dot" />
              <h2>微盘历史观察</h2>
            </div>
            <p>
              {data?.start_date
                ? `${formatDate(data.start_date)} — ${formatDate(data.end_date)}`
                : '自 2021 年起 · 每 5 个交易日采样'}
            </p>
          </div>
          <div className="range-switch" aria-label="微盘温度历史范围">
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
        <div className="microcap-view-switch" aria-label="微盘指标切换">
          {temperatureViews.map((item) => (
            <button
              key={item.key}
              aria-pressed={view === item.key}
              className={view === item.key ? 'active' : ''}
              onClick={() => setView(item.key)}
            >
              {item.label}
            </button>
          ))}
        </div>
        {loading ? (
          <div className="skeleton chart-skeleton" />
        ) : !error && latest && data ? (
          <>
            <label className="crowding-toggle">
              <input
                type="checkbox"
                checked={overlay}
                onChange={(event) => setOverlay(event.target.checked)}
              />
              叠加微盘 1000 等权指数（右轴）
            </label>
            <MicrocapChart points={data.points} view={view} overlayIndex={overlay} />
            <div className="panel-bottom">
              <span>{data.summary.observed_samples} 个采样点 · 拖动滑块缩放</span>
              <span>
                区间温度变化 {signedScore(data.summary.score_change)} 分 · 指数变化{' '}
                {signedScore(data.summary.index_change_pct)}%
              </span>
            </div>
          </>
        ) : null}
      </section>
      {!loading && !error && data && latest && (
        <div className="microcap-secondary">
          <section className="panel trend-panel">
            <div className="panel-heading">
              <div>
                <h2>PE / PB 中位数</h2>
                <p>
                  PE {formatScore(latest.pe_median)} 倍 · PB {formatScore(latest.pb_median)} 倍
                </p>
              </div>
            </div>
            <MicrocapChart points={data.points} view="valuation" />
          </section>
          <section className="panel trend-panel">
            <div className="panel-heading">
              <div>
                <h2>归母净利润同比增速</h2>
                <p>
                  最新{' '}
                  {latest.avg_np_growth == null
                    ? '暂无数据'
                    : latest.avg_np_growth.toFixed(1) + '%'}{' '}
                  · 有效样本 {latest.profit_growth_count} / {latest.micro_num}
                </p>
              </div>
            </div>
            <MicrocapChart points={data.points} view="profit" />
          </section>
        </div>
      )}
      <section className="method-card crowding-method">
        <span className="method-icon">
          <Info size={19} />
        </span>
        <div>
          <h3>读懂这支温度计</h3>
          <p>
            剔除北交所、ST 和上市不足 250 个自然日的股票，在数据完整的股票中选择市值最小的 1000
            只。综合温度 = 相对估值历史分位 × 50% + 换手率拥挤度历史分位 × 25% +
            成交额拥挤度历史分位 × 25%，页面按 0–100 展示。
          </p>
          <span>
            分位以 2021
            年以来截至当期的采样值计算，相同值取平均排名；切换图表范围不会重算分位。对照组为经过相同筛选与数据清洗后的股票，并非全部
            A
            股。历史样本较少时分位参考有限。指数按上期成分股等权收益续算；利润增速缺失保留断点，不补零。当天数据可能尚未收盘，建议收盘后采集。
          </span>
        </div>
      </section>
    </div>
  )
}
