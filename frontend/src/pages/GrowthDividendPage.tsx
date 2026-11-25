import { HistoryRangeSwitch } from '../components/HistoryRangeSwitch'
import { TrendPanelHeading } from '../components/TrendPanelHeading'
import { DisplayRefreshButton, PageActionButton } from '../components/PageActionButton'
import { PageHeaderActions } from '../components/PageHeaderActions'
import { useCallback, useEffect, useRef, useState } from 'react'
import { Download, Square, TrendingUp } from 'lucide-react'
import { getGrowthDividendHistory } from '../api/growthDividend'
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
import { useCollectionTask } from '../hooks/useCollectionTask'
import {
  growthDividendCollectionTask,
  growthCollectionErrorText,
} from '../lib/growthDividendCollectionTask'
import '../styles/growth-dividend.css'

export function GrowthDividendPage() {
  const { session } = useAuth()
  const admin = session?.user?.role === 'admin'
  const {
    status,
    statusError: pollError,
    actionError,
    pending,
    accepted,
    uncertain,
    stopping,
    historyRevision,
    act: startCollection,
    cancel,
    reconcile,
  } = useCollectionTask(growthDividendCollectionTask, session, admin)
  const [data, setData] = useState<GrowthDividendHistory | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const historyProgress = useRef(-1)
  const historyAt = useRef(0)
  const historyLoading = useRef(false)
  const historyStatus = useRef<GrowthDividendStatus | null>(null)
  const [revision, setRevision] = useState(0)
  const [selection, setSelection] = useState<GrowthDividendSelection>({ years: 5, revision: 0 })
  const [activeYears, setActiveYears] = useState<GrowthDividendYears | null>(5)
  const [shownRange, setShownRange] = useState<DateSpan | null>(null)
  const [start, setStart] = useState('')
  const [end, setEnd] = useState('')
  const [rangeError, setRangeError] = useState<string | null>(null)
  const editingDates = useRef(false)
  const reload = useCallback(() => setRevision((current) => current + 1), [])
  const reloadDisplay = () => {
    reload()
    void reconcile()
  }
  const onRangeChange = useCallback((span: DateSpan, manual: boolean) => {
    setShownRange(span)
    if (manual) editingDates.current = false
    if (!editingDates.current) {
      setStart(span.start)
      setEnd(span.end)
    }
    if (manual) setActiveYears(null)
  }, [])

  useEffect(() => {
    const controller = new AbortController()
    historyLoading.current = true
    setLoading(true)
    setError(null)
    getGrowthDividendHistory(controller.signal)
      .then((result) => {
        if (controller.signal.aborted) return
        // The independent /status request is the authority for command state.
        setData(result)
        historyAt.current = Date.now()
        historyProgress.current = result.status.processed
        historyStatus.current = result.status
      })
      .catch((reason: unknown) => {
        if (!controller.signal.aborted) setError(growthCollectionErrorText(reason))
      })
      .finally(() => {
        if (!controller.signal.aborted) {
          historyLoading.current = false
          setLoading(false)
        }
      })
    return () => {
      controller.abort()
      historyLoading.current = false
    }
  }, [revision, historyRevision])

  useEffect(() => {
    if (!status || historyLoading.current) return
    const shown = historyStatus.current
    const elapsed = Date.now() - historyAt.current
    const progressed =
      status.running &&
      status.processed !== historyProgress.current &&
      (historyProgress.current <= 0 || elapsed >= 15000)
    // A status response can arrive before initial history. Recheck on the
    // next poll if that history captured an older terminal/publication state.
    const completedSinceHistory =
      shown &&
      elapsed >= 15000 &&
      !status.running &&
      !status.database_syncing &&
      (shown.running ||
        shown.database_syncing ||
        status.last_success_at !== shown.last_success_at ||
        status.last_attempt_at !== shown.last_attempt_at)
    if (progressed || completedSinceHistory) reload()
  }, [status, reload])

  function act(action: 'refresh' | 'cancel') {
    return action === 'refresh' ? startCollection() : cancel()
  }

  const latest = data?.latest
  const databaseSyncing = status?.database_syncing ?? false
  const running = !databaseSyncing && (status?.running ?? false)
  const busy = pending || (!running && (accepted || stopping))
  const progress = status?.percent == null ? null : Math.max(0, Math.min(100, status.percent))
  const shownError =
    (actionError && uncertain
      ? actionError + ' 操作结果待确认，请重新加载后再操作。'
      : actionError) ||
    error ||
    pollError ||
    (!running ? status?.error : null)

  return (
    <div className="page-enter growth-dividend-page">
      <PageHeader dataUpdatedAt={data?.updated_at}>
        <div>
          <h1>
            市场情绪 <span className="heading-tag">创红比</span>
          </h1>
        </div>
        <div className="heading-actions">
          <DisplayRefreshButton
            ariaLabel="刷新创红比展示数据"
            onClick={reloadDisplay}
            loading={loading}
          />
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
        <TrendPanelHeading
          title="指数点位与成长红利比"
          description="实线为指数点位（左轴），虚线为比值（右轴）。点击图例显示或隐藏曲线。"
        />
        <div className="growth-dividend-toolbar">
          <HistoryRangeSwitch
            label="创红比历史范围"
            options={
              [
                { value: 1, label: '近1年' },
                { value: 3, label: '近3年' },
                { value: 5, label: '近5年' },
                { value: 0, label: '全部' },
              ] as const
            }
            value={activeYears}
            onChange={(years) => {
              editingDates.current = false
              setActiveYears(years)
              setSelection((current) => ({ years, revision: current.revision + 1 }))
              setRangeError(null)
            }}
          />
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

      <PageHeaderActions>
        {running ? (
          <PageActionButton
            icon={Square}
            iconSize={15}
            tone="quiet"
            onClick={() => void act('cancel')}
            disabled={busy || stopping || status?.cancelling || !admin || uncertain || !!pollError}
          >
            {stopping || status?.cancelling ? '正在停止…' : '取消采集'}
          </PageActionButton>
        ) : (
          <PageActionButton
            icon={Download}
            onClick={() => void act('refresh')}
            disabled={
              databaseSyncing || busy || loading || !admin || uncertain || !status || !!pollError
            }
          >
            {databaseSyncing ? '数据库同步中' : busy ? '正在启动…' : '更新数据'}
          </PageActionButton>
        )}
      </PageHeaderActions>
      {running && (
        <p role="status">
          {status?.stage || '正在更新'}
          {progress != null ? ` · ${progress.toFixed(0)}%` : ''}
        </p>
      )}
      {databaseSyncing && <p role="status">数据库同步中，请到顶部“数据同步”查看或取消。</p>}
      {shownError && (
        <p role="alert">
          {shownError}
          {data?.points.length ? ' 已保留现有数据。' : ''}
        </p>
      )}

      <details className="method-card growth-dividend-method">
        <summary>统计口径</summary>
        <div>
          <p>
            创红比 = 创业板指收盘点位 ÷ 中证红利收盘点位；科创红利比 = 科创50收盘点位 ÷
            中证红利收盘点位。
          </p>
          <span>
            同一比值上行，表示对应成长指数在该区间强于中证红利；下行则相反。采用价格指数；科创50从2020年可取得的历史行情开始，缺失数据留空。
            分位基准只使用真实有效比值：创红比从2018-01-01起，科创红利比从首个有效数据日起，均计算至最新历史数据；缩放区间不参与计算。
          </span>
        </div>
      </details>
    </div>
  )
}
