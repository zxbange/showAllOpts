import { useCallback, useEffect, useRef, useState } from 'react'
import { CalendarDays, Clock3, Download, RefreshCw, Square, TrendingUp } from 'lucide-react'
import {
  getGrowthDividendHistory,
  getGrowthDividendStatus,
  updateGrowthDividend,
} from '../api/growthDividend'
import { useAuth } from '../components/AccountAccess'
import { GrowthDividendChart } from '../components/GrowthDividendChart'
import { PageHeader } from '../components/PageHeader'
import { StatCard } from '../components/StatCard'
import { EmptyState } from '../components/States'
import {
  growthDividendLines,
  growthDividendSpan,
  growthDividendValue,
  type DateSpan,
  type GrowthDividendSelection,
  type GrowthDividendYears,
} from '../lib/growthDividend'
import type { GrowthDividendHistory, GrowthDividendStatus } from '../types/growthDividend'
import '../styles/growth-dividend.css'

const message = (error: unknown) =>
  error instanceof Error && error.message !== 'Failed to fetch'
    ? error.message
    : '暂时无法连接数据服务，请稍后重试。'
const timestamp = (value: string | null | undefined) =>
  value
    ? new Date(value).toLocaleString('zh-CN', {
        timeZone: 'Asia/Shanghai',
        hour12: false,
        year: 'numeric',
        month: '2-digit',
        day: '2-digit',
        hour: '2-digit',
        minute: '2-digit',
      })
    : '—'

export function GrowthDividendPage() {
  const admin = useAuth().session?.user?.role === 'admin'
  const [data, setData] = useState<GrowthDividendHistory | null>(null)
  const [status, setStatus] = useState<GrowthDividendStatus | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [actionError, setActionError] = useState<string | null>(null)
  const [pollError, setPollError] = useState<string | null>(null)
  const [uncertain, setUncertain] = useState(false)
  const [busy, setBusy] = useState(false)
  const [stopping, setStopping] = useState(false)
  const [revision, setRevision] = useState(0)
  const [selection, setSelection] = useState<GrowthDividendSelection>({ years: 5, revision: 0 })
  const [activeYears, setActiveYears] = useState<GrowthDividendYears | null>(5)
  const [shownRange, setShownRange] = useState<DateSpan | null>(null)
  const [start, setStart] = useState('')
  const [end, setEnd] = useState('')
  const [rangeError, setRangeError] = useState<string | null>(null)
  const operation = useRef<AbortController | null>(null)
  const editingDates = useRef(false)
  const reload = useCallback(() => setRevision((current) => current + 1), [])
  const onRangeChange = useCallback((span: DateSpan, manual: boolean) => {
    setShownRange(span)
    if (manual) editingDates.current = false
    if (!editingDates.current) {
      setStart(span.start)
      setEnd(span.end)
    }
    if (manual) setActiveYears(null)
  }, [])

  useEffect(() => () => operation.current?.abort(), [])

  useEffect(() => {
    const controller = new AbortController()
    let timer: ReturnType<typeof setTimeout>
    let previous: GrowthDividendStatus | null = null
    let historyAt = 0
    let historyProgress = -1
    setLoading(true)
    setError(null)
    async function loadHistory() {
      const result = await getGrowthDividendHistory(controller.signal)
      if (controller.signal.aborted) return
      setData(result)
      setStatus(result.status)
      setUncertain(false)
      previous = result.status
      historyAt = Date.now()
      historyProgress = result.status.processed
      if (!result.status.running) setStopping(false)
      setError(null)
    }
    async function poll() {
      try {
        const next = await getGrowthDividendStatus(controller.signal)
        if (controller.signal.aborted) return
        const completed =
          !previous ||
          (previous.running && !next.running) ||
          previous.last_success_at !== next.last_success_at ||
          (!next.running && previous.last_attempt_at !== next.last_attempt_at)
        const progressed =
          next.running &&
          next.processed !== historyProgress &&
          (historyProgress <= 0 || Date.now() - historyAt >= 15000)
        previous = next
        setStatus(next)
        if (!next.running) setStopping(false)
        if (completed || progressed) await loadHistory()
        if (!controller.signal.aborted) setPollError(null)
      } catch (reason) {
        if (!controller.signal.aborted) setPollError(message(reason))
      } finally {
        if (!controller.signal.aborted) timer = setTimeout(poll, previous?.running ? 2000 : 30000)
      }
    }
    void loadHistory()
      .catch((reason: unknown) => {
        if (!controller.signal.aborted) setError(message(reason))
      })
      .finally(() => {
        if (!controller.signal.aborted) {
          setLoading(false)
          timer = setTimeout(poll, 2000)
        }
      })
    return () => {
      controller.abort()
      clearTimeout(timer)
    }
  }, [revision])

  async function act(action: 'refresh' | 'cancel' | 'sync-joinquant') {
    if (operation.current || !admin || uncertain || (status?.running && action !== 'cancel')) return
    const controller = new AbortController()
    operation.current = controller
    setBusy(true)
    setError(null)
    setActionError(null)
    setPollError(null)
    try {
      const result = await updateGrowthDividend(action, controller.signal)
      if (controller.signal.aborted) return
      if (action === 'cancel' && result.cancelled) setStopping(true)
      reload()
    } catch (reason) {
      if (!controller.signal.aborted) {
        setUncertain(true)
        setActionError(message(reason) + ' 操作结果待确认，请重新加载后再操作。')
      }
    } finally {
      if (operation.current === controller) operation.current = null
      if (!controller.signal.aborted) setBusy(false)
    }
  }

  const latest = data?.latest
  const running = status?.running ?? false
  const progress = status?.percent == null ? null : Math.max(0, Math.min(100, status.percent))
  const shownError = actionError || error || pollError || (!running ? status?.error : null)
  const pending = status?.pending_dates ?? 0

  return (
    <div className="page-enter growth-dividend-page">
      <PageHeader>
        <div>
          <h1>
            市场情绪 <span className="heading-tag">创红比</span>
          </h1>
          <p>对照创业板、科创50与中证红利，观察成长与红利的相对强弱。</p>
        </div>
        <div className="heading-actions">
          <span className="date-badge">
            <CalendarDays size={15} />
            数据截至 {data?.end_date ?? '—'}
          </span>
          <button
            className="button button-quiet icon-button"
            aria-label="刷新创红比展示数据"
            onClick={reload}
            disabled={loading}
          >
            <RefreshCw size={16} className={loading ? 'spin' : ''} />
          </button>
        </div>
      </PageHeader>

      <div className="stats-grid growth-dividend-stats" aria-busy={loading}>
        {growthDividendLines.map((line) => (
          <StatCard
            key={line.key}
            label={line.name}
            value={growthDividendValue(latest?.[line.key], line.ratio)}
            suffix={line.ratio ? undefined : '点'}
            note={
              line.ratio
                ? line.key === 'chinext_dividend_ratio'
                  ? '创业板指 ÷ 中证红利'
                  : '科创50 ÷ 中证红利'
                : `${line.code} · 收盘点位`
            }
            icon={<TrendingUp size={17} color={line.color} />}
            accent={line.key === 'chinext_dividend_ratio'}
          />
        ))}
      </div>

      <section className="panel trend-panel growth-dividend-trend" aria-busy={loading}>
        <div className="panel-heading">
          <div>
            <div className="panel-title">
              <span className="status-dot" />
              <h2>指数点位与成长红利比</h2>
            </div>
            <p>实线为指数点位（左轴），虚线为比值（右轴）。点击图例显示或隐藏曲线。</p>
          </div>
        </div>
        <div className="growth-dividend-toolbar">
          <div className="range-switch" aria-label="创红比历史范围">
            {(
              [
                [1, '近1年'],
                [3, '近3年'],
                [5, '近5年'],
                [0, '全部'],
              ] as const
            ).map(([years, label]) => (
              <button
                type="button"
                key={years}
                className={activeYears === years ? 'active' : ''}
                aria-pressed={activeYears === years}
                onClick={() => {
                  editingDates.current = false
                  setActiveYears(years)
                  setSelection((current) => ({ years, revision: current.revision + 1 }))
                  setRangeError(null)
                }}
              >
                {label}
              </button>
            ))}
          </div>
          <form
            className="growth-dividend-dates"
            onSubmit={(event) => {
              event.preventDefault()
              if (!start || !end || start > end) {
                setRangeError('开始日期不能晚于结束日期。')
                return
              }
              const span = growthDividendSpan(data?.points ?? [], 0, { start, end })
              if (!span) {
                setRangeError('所选区间暂无交易日数据。')
                return
              }
              setActiveYears(null)
              editingDates.current = false
              setSelection((current) => ({ years: 0, span, revision: current.revision + 1 }))
              setRangeError(null)
            }}
          >
            <label>
              <span className="sr-only">创红比开始日期</span>
              <input
                type="date"
                value={start}
                min={data?.points[0]?.date ?? data?.start_date ?? '2018-01-01'}
                max={data?.points.at(-1)?.date ?? data?.end_date ?? undefined}
                onChange={(event) => {
                  editingDates.current = true
                  setStart(event.target.value)
                }}
                required
                disabled={!data?.points.length}
              />
            </label>
            <span aria-hidden="true">—</span>
            <label>
              <span className="sr-only">创红比结束日期</span>
              <input
                type="date"
                value={end}
                min={data?.points[0]?.date ?? data?.start_date ?? '2018-01-01'}
                max={data?.points.at(-1)?.date ?? data?.end_date ?? undefined}
                onChange={(event) => {
                  editingDates.current = true
                  setEnd(event.target.value)
                }}
                required
                disabled={!data?.points.length}
              />
            </label>
            <button className="button button-quiet" disabled={!data?.points.length}>
              应用
            </button>
          </form>
        </div>
        {rangeError && (
          <p className="growth-dividend-range-error" role="alert">
            {rangeError}
          </p>
        )}
        {data?.points.length ? (
          <GrowthDividendChart
            points={data.points}
            selection={selection}
            onRangeChange={onRangeChange}
          />
        ) : loading ? (
          <div className="skeleton growth-dividend-placeholder" aria-label="正在加载创红比数据" />
        ) : (
          <div className="growth-dividend-placeholder">
            <EmptyState
              title={running ? '正在补齐指数历史数据' : '暂无创红比数据'}
              description={
                running
                  ? '历史数据会逐步显示，稍后将自动刷新。'
                  : '首次自动补齐2018年以来的历史数据，也可点击“更新数据”重试。'
              }
            />
          </div>
        )}
        <div className="panel-bottom growth-dividend-chart-note">
          <span>拖动滑块或图表平移 · Ctrl + 滚轮缩放 · 触屏双指缩放</span>
          <span aria-label="创红比当前显示范围">
            {shownRange ? `${shownRange.start} — ${shownRange.end}` : '默认显示最新5年'}
          </span>
        </div>
      </section>

      <section className="panel growth-dividend-update" aria-label="创红比数据更新">
        <div className="growth-dividend-update-heading">
          <div>
            <strong>
              <Clock3 size={16} /> 每个交易日北京时间 18:00 更新
            </strong>
            <p>补齐当天与此前未成功更新的数据，历史记录从2018年起保留。</p>
          </div>
          {admin && (
            <button
              className="button button-quiet"
              onClick={() => void act('sync-joinquant')}
              disabled={busy || loading || running || uncertain}
            >
              同步聚宽历史
            </button>
          )}
          {running ? (
            <button
              className="button button-quiet"
              onClick={() => void act('cancel')}
              disabled={busy || stopping || status?.cancelling || !admin || uncertain}
            >
              <Square size={15} />
              {stopping || status?.cancelling ? '正在停止…' : '停止更新'}
            </button>
          ) : (
            <button
              className="button button-primary"
              onClick={() => void act('refresh')}
              disabled={busy || loading || !admin || uncertain}
            >
              <Download size={16} />
              {busy ? '正在启动…' : '更新数据'}
            </button>
          )}
        </div>
        <div className="growth-dividend-progress" aria-live="polite">
          {running ? (
            <>
              <div className="growth-dividend-progress-label">
                <span>{status?.stage || '正在更新'}</span>
                <span>{progress == null ? '处理中' : `${progress.toFixed(0)}%`}</span>
              </div>
              <progress aria-label="创红比更新进度" max={100} value={progress ?? undefined} />
            </>
          ) : (
            <span>{status?.stage || '等待更新'}</span>
          )}
          <div className="growth-dividend-update-meta">
            <span>上次完成 {timestamp(status?.last_success_at)}</span>
            {pending > 0 && (
              <span className="growth-dividend-pending">
                待补 {pending.toLocaleString('zh-CN')} 个交易日 ·{' '}
                {(status?.pending_points ?? 0).toLocaleString('zh-CN')} 条指数数据
              </span>
            )}
          </div>
        </div>
        {status?.sync_summary && (
          <p role="status">
            聚宽同步：新增 {status.sync_summary.inserted} 条， 已存在 {status.sync_summary.existing}{' '}
            条，冲突保留原值 {status.sync_summary.conflicts} 条。
            {status.sync_summary.coverage}
          </p>
        )}
        {!!status?.comparison_conflicts && (
          <details>
            <summary>公开源对照有 {status.comparison_conflicts} 条差异，已保留原值</summary>
            <ul>
              {status.recent_conflicts?.map((item) => (
                <li key={`${item.code}-${item.trade_date}-${item.candidate_close}`}>
                  {item.trade_date} · {item.code}：已保存 {item.published_close}，公开源候选{' '}
                  {item.candidate_close}（未覆盖）
                </li>
              ))}
            </ul>
          </details>
        )}
        {shownError && (
          <div className="growth-dividend-error" role="alert">
            <span>
              {shownError}
              {data?.points.length ? ' 已保留现有数据。' : ''}
            </span>
            <button className="button button-quiet" onClick={reload} disabled={loading}>
              重新加载
            </button>
          </div>
        )}
      </section>

      <div className="method-card growth-dividend-method">
        <div>
          <h3>统计口径</h3>
          <p>
            创红比 = 创业板指收盘点位 ÷ 中证红利收盘点位；科创红利比 = 科创50收盘点位 ÷
            中证红利收盘点位。
          </p>
          <span>
            同一比值上行，表示对应成长指数在该区间强于中证红利；下行则相反。采用价格指数；科创50从2020年可取得的历史行情开始，缺失数据留空。
            分位基准只使用真实有效比值：创红比从2018-01-01起，科创红利比从首个有效数据日起，均计算至最新历史数据；缩放区间不参与计算。
          </span>
        </div>
      </div>
    </div>
  )
}
