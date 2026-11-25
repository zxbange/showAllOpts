import { PageHeaderActions } from '../components/PageHeaderActions'
import { useAuth } from '../components/AccountAccess'
import { useCallback, useEffect, useRef, useState } from 'react'
import { RefreshCw, Download, Square, TrendingUp, TrendingDown, Gauge, Layers3 } from 'lucide-react'
import { getMarginHistory } from '../api/client'
import { useCollectionTask } from '../hooks/useCollectionTask'
import { marginCollectionTask, marginCollectionErrorText } from '../lib/marginCollectionTask'
import { PageHeader } from '../components/PageHeader'
import { StatCard } from '../components/StatCard'
import { MarginChart } from '../components/MarginChart'
import { EmptyState } from '../components/States'
import type { MarginHistory, MarginStatus } from '../types/margin'
import '../styles/margin.css'

const percent = (value: number | null | undefined) => (value == null ? '—' : value.toFixed(2) + '%')
const pp = (value: number | null | undefined) =>
  value == null ? '—' : (value > 0 ? '+' : '') + value.toFixed(2) + ' 个百分点'
export function MarginPage() {
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
  } = useCollectionTask(marginCollectionTask, session, admin)
  const [data, setData] = useState<MarginHistory | null>(null)
  const historyProgress = useRef(0)
  const historyRefreshAt = useRef(0)
  const historyLoading = useRef(false)
  const historyStatus = useRef<MarginStatus | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [revision, setRevision] = useState(0)
  const [chartDays, setChartDays] = useState(0)
  const [chartReset, setChartReset] = useState(0)
  const reload = useCallback(() => setRevision((value) => value + 1), [])
  const reloadDisplay = () => {
    reload()
    void reconcile()
  }

  useEffect(() => {
    const controller = new AbortController()
    historyLoading.current = true
    historyRefreshAt.current = Date.now()
    setLoading(true)
    setError(null)
    getMarginHistory(controller.signal)
      .then((next) => {
        if (controller.signal.aborted) return
        historyProgress.current = next.status.processed
        historyStatus.current = next.status
        setData(next)
      })
      .catch((reason: unknown) => {
        if (!controller.signal.aborted) setError(marginCollectionErrorText(reason))
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

  // History and task status are separate requests; history may never overwrite
  // the latest command/status observation. Preserve the existing progress throttle.
  useEffect(() => {
    if (!status || historyLoading.current) return
    const shown = historyStatus.current
    const elapsed = Date.now() - historyRefreshAt.current
    const progressed =
      status.running &&
      status.processed > historyProgress.current &&
      (historyProgress.current === 0 || elapsed >= 15000)
    // A status response can arrive before initial history. Recheck on the
    // next poll if that history captured an older terminal/publication state.
    const completedSinceHistory =
      shown &&
      elapsed >= 15000 &&
      !status.running &&
      (shown.running ||
        status.last_success_at !== shown.last_success_at ||
        status.last_attempt_at !== shown.last_attempt_at)
    if (progressed || completedSinceHistory) reload()
  }, [status, reload])

  function act(action: 'refresh' | 'cancel') {
    return action === 'refresh' ? startCollection() : cancel()
  }

  const latest = data?.latest
  const summary = data?.summary
  const running = status?.running ?? false
  const cancelling = stopping || status?.cancelling
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
    <div className="page-enter margin-page">
      <PageHeader dataUpdatedAt={latest?.updated_at}>
        <div>
          <h1>
            市场情绪 <span className="heading-tag">两融担保比例</span>
          </h1>
          <p>全市场平均维持担保比例，观察两融账户的担保充足程度。</p>
        </div>
        <div className="heading-actions">
          <button
            className="button button-quiet icon-button"
            aria-label="刷新两融展示数据"
            onClick={reloadDisplay}
            disabled={loading}
          >
            <RefreshCw size={16} className={loading ? 'spin' : ''} />
          </button>
        </div>
      </PageHeader>

      <div className="stats-grid margin-stats" aria-busy={loading}>
        <StatCard
          label="最新平均维持担保比例"
          value={percent(latest?.ratio)}
          note={
            <span title={summary?.previous_date ? `上一记录：${summary.previous_date}` : undefined}>
              {summary?.change_pp == null ? '较上一记录 —' : `较上一记录 ${pp(summary.change_pp)}`}
            </span>
          }
          icon={<Gauge size={18} />}
          accent
        />
        <StatCard
          label="距 240% 参考线"
          value={
            summary?.distance_pp == null
              ? '—'
              : `${summary.distance_pp > 0 ? '+' : ''}${summary.distance_pp.toFixed(2)}`
          }
          suffix="百分点"
          note={
            summary?.distance_pp == null
              ? '等待数据'
              : summary.distance_pp > 0
                ? '高于参考线'
                : summary.distance_pp < 0
                  ? '低于参考线'
                  : '位于参考线'
          }
          icon={<TrendingUp size={18} />}
        />
        <StatCard
          label="历史分位（已采集）"
          value={summary?.percentile == null ? '—' : summary.percentile.toFixed(1) + '%'}
          note={
            summary
              ? `${summary.observed_days.toLocaleString('zh-CN')} 条记录中 ${summary.below_count.toLocaleString('zh-CN')} 条低于当前`
              : '等待数据'
          }
          icon={<Layers3 size={18} />}
        />
        <StatCard
          label="区间最高"
          value={percent(summary?.peak_ratio)}
          note={summary?.peak_date ?? '—'}
          icon={<TrendingUp size={18} />}
        />
        <StatCard
          label="区间最低"
          value={percent(summary?.low_ratio)}
          note={summary?.low_date ?? '—'}
          icon={<TrendingDown size={18} />}
        />
      </div>

      <section className="panel trend-panel margin-trend" aria-busy={loading}>
        <div className="panel-heading">
          <div>
            <div className="panel-title">
              <span className="status-dot" />
              <h2>两融担保比例走势 · 近六年</h2>
            </div>
            <p>
              {data?.start_date
                ? `${data.start_date} — ${data.end_date} · ${summary?.observed_days.toLocaleString('zh-CN')} 条记录`
                : '按官方披露日期记录'}
            </p>
          </div>
          {latest?.source_url && (
            <a
              className="button button-quiet"
              href={latest.source_url}
              target="_blank"
              rel="noreferrer"
            >
              查看数据来源
            </a>
          )}
        </div>
        <div className="margin-chart-toolbar">
          <div className="range-switch" aria-label="两融历史范围">
            {[
              [30, '近1月'],
              [90, '近3月'],
              [180, '近半年'],
              [365, '近1年'],
              [0, '全部'],
            ].map(([value, label]) => (
              <button
                key={value}
                className={chartDays === value ? 'active' : ''}
                aria-pressed={chartDays === value}
                onClick={() => {
                  setChartDays(Number(value))
                  setChartReset((current) => current + 1)
                }}
              >
                {label}
              </button>
            ))}
          </div>
          <span>拖动底部滑块缩放 · Ctrl + 滚轮缩放 · 触屏双指缩放</span>
        </div>
        {data?.points.length ? (
          <MarginChart points={data.points} days={chartDays} resetKey={chartReset} />
        ) : loading ? (
          <div className="skeleton margin-chart-placeholder" aria-label="正在加载两融数据" />
        ) : (
          <div className="margin-chart-placeholder">
            <EmptyState
              title="暂无两融担保比例数据"
              description="点击“更新数据”获取近六年记录；已保存的数据可继续补采。"
            />
          </div>
        )}
        <div className="panel-bottom">
          <span>240% / 150% 为观察参考线，不代表统一监管阈值。</span>
          <span>来源：东方财富；历史记录保留原来源</span>
        </div>
      </section>

      <PageHeaderActions>
        <div className="margin-update-actions">
          {running ? (
            <button
              className="button button-quiet"
              onClick={() => void act('cancel')}
              disabled={busy || cancelling || !admin || uncertain || !!pollError}
            >
              <Square size={15} />
              {cancelling ? '正在停止…' : '取消采集'}
            </button>
          ) : (
            <button
              className="button button-primary"
              onClick={() => void act('refresh')}
              disabled={busy || loading || !admin || uncertain || !status || !!pollError}
            >
              <Download size={16} />
              {busy ? '正在启动…' : '更新数据'}
            </button>
          )}
        </div>
      </PageHeaderActions>
      {running && (
        <p role="status">
          {status?.stage || '准备采集'}
          {progress != null ? ` · ${progress.toFixed(0)}%` : ''}
          {status?.total
            ? ` · ${status.processed.toLocaleString('zh-CN')} / ${status.total.toLocaleString('zh-CN')}`
            : ''}
        </p>
      )}
      {shownError && <p role="alert">{shownError}</p>}
    </div>
  )
}
