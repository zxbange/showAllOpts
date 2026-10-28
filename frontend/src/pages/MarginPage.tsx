import { useCallback, useEffect, useRef, useState } from 'react'
import {
  CalendarDays,
  RefreshCw,
  Download,
  Square,
  TrendingUp,
  TrendingDown,
  Gauge,
  Layers3,
} from 'lucide-react'
import { getMarginHistory, getMarginStatus, updateMargin } from '../api/client'
import { PageHeader } from '../components/PageHeader'
import { StatCard } from '../components/StatCard'
import { MarginChart } from '../components/MarginChart'
import { EmptyState } from '../components/States'
import type { MarginHistory, MarginStatus } from '../types/margin'
import '../styles/margin.css'

const percent = (value: number | null | undefined) => (value == null ? '—' : value.toFixed(2) + '%')
const pp = (value: number | null | undefined) =>
  value == null ? '—' : (value > 0 ? '+' : '') + value.toFixed(2) + ' 个百分点'
const message = (error: unknown) =>
  error instanceof Error && error.message !== 'Failed to fetch'
    ? error.message
    : '暂时无法连接数据服务，请稍后重试。'

export function MarginPage() {
  const [data, setData] = useState<MarginHistory | null>(null)
  const [status, setStatus] = useState<MarginStatus | null>(null)
  const currentStatus = useRef<MarginStatus | null>(null)
  const historyProgress = useRef(0)
  const historyRefreshAt = useRef(0)
  const historyLoading = useRef(false)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [pollError, setPollError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [stopping, setStopping] = useState(false)
  const [revision, setRevision] = useState(0)
  const reload = useCallback(() => setRevision((value) => value + 1), [])
  const saveStatus = useCallback((next: MarginStatus) => {
    currentStatus.current = next
    setStatus(next)
    if (!next.running) setStopping(false)
  }, [])

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
        setData(next)
        saveStatus(next.status)
      })
      .catch((reason: unknown) => {
        if (!controller.signal.aborted) setError(message(reason))
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
  }, [revision, saveStatus])

  useEffect(() => {
    const controller = new AbortController()
    let timer: number
    async function poll() {
      try {
        const next = await getMarginStatus(controller.signal)
        if (controller.signal.aborted) return
        setPollError(null)
        const previous = currentStatus.current
        saveStatus(next)
        const completed =
          previous &&
          ((previous.running && !next.running) || previous.last_success_at !== next.last_success_at)
        const progressed =
          next.running &&
          next.processed > historyProgress.current &&
          (historyProgress.current === 0 || Date.now() - historyRefreshAt.current >= 15000)
        if (!historyLoading.current && (completed || progressed)) {
          reload()
        }
      } catch (reason) {
        if (!controller.signal.aborted) setPollError(message(reason))
      } finally {
        if (!controller.signal.aborted)
          timer = window.setTimeout(poll, currentStatus.current?.running ? 2000 : 30000)
      }
    }
    timer = window.setTimeout(poll, 2000)
    return () => {
      controller.abort()
      window.clearTimeout(timer)
    }
  }, [revision, reload, saveStatus])

  async function act(action: 'refresh' | 'cancel') {
    setBusy(true)
    setError(null)
    setPollError(null)
    try {
      const result = await updateMargin(action)
      if (action === 'cancel' && result.cancelled) setStopping(true)
      saveStatus(await getMarginStatus())
      reload()
    } catch (reason) {
      setError(message(reason))
    } finally {
      setBusy(false)
    }
  }

  const latest = data?.latest
  const summary = data?.summary
  const running = status?.running ?? false
  const cancelling = stopping || status?.cancelling
  const progress = status?.percent == null ? null : Math.max(0, Math.min(100, status.percent))
  const shownError = error || pollError || (!running ? status?.error : null)
  return (
    <div className="page-enter margin-page">
      <PageHeader>
        <div>
          <h1>
            市场情绪 <span className="heading-tag">两融担保比例</span>
          </h1>
          <p>全市场平均维持担保比例，观察两融账户的担保充足程度。</p>
        </div>
        <div className="heading-actions">
          <span className="date-badge">
            <CalendarDays size={15} />
            数据截至 {latest?.date ?? '—'}
          </span>
          <button
            className="button button-quiet icon-button"
            aria-label="刷新两融展示数据"
            onClick={reload}
            disabled={loading}
          >
            <RefreshCw size={16} className={loading ? 'spin' : ''} />
          </button>
        </div>
      </PageHeader>

      <section className="margin-update panel" aria-label="两融数据更新">
        <div className="margin-update-row">
          <div>
            <strong>中证数据 · 每天北京时间 20:00 检查更新</strong>
            <p>
              {latest
                ? `当前展示 ${latest.date} 已公布数据；当天未公布时保留最近一期。`
                : '首次采集补齐近六年历史，后续按日期增量更新。'}
            </p>
          </div>
          <div className="margin-update-actions">
            {running ? (
              <button
                className="button button-quiet"
                onClick={() => void act('cancel')}
                disabled={busy || cancelling}
              >
                <Square size={15} />
                {cancelling ? '正在停止…' : '停止采集'}
              </button>
            ) : (
              <button
                className="button button-primary"
                onClick={() => void act('refresh')}
                disabled={busy || loading}
              >
                <Download size={16} />
                {busy ? '正在启动…' : latest ? '更新数据' : '采集历史数据'}
              </button>
            )}
          </div>
        </div>
        <div className="margin-progress-area" aria-live="polite">
          {running ? (
            <>
              <div className="margin-progress-label">
                <span>{status?.stage || '准备采集'}</span>
                <span>
                  {progress == null ? '处理中' : `${progress.toFixed(0)}%`}
                  {status?.total
                    ? ` · ${status.processed.toLocaleString('zh-CN')} / ${status.total.toLocaleString('zh-CN')}`
                    : ''}
                </span>
              </div>
              <progress aria-label="两融采集进度" max={100} value={progress ?? undefined} />
            </>
          ) : (
            <span className="margin-idle-state">
              {status?.stage || '等待更新'}
              {status?.last_success_at
                ? ` · 上次完成 ${new Date(status.last_success_at).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai', hour12: false })}`
                : ''}
            </span>
          )}
        </div>
        {shownError && (
          <div className="margin-error" role="alert">
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
              查看官方原表
            </a>
          )}
        </div>
        {data?.points.length ? (
          <MarginChart points={data.points} />
        ) : loading ? (
          <div className="skeleton margin-chart-placeholder" aria-label="正在加载两融数据" />
        ) : (
          <div className="margin-chart-placeholder">
            <EmptyState
              title="暂无两融担保比例数据"
              description="点击“采集历史数据”读取中证数据官方报表；完成的记录会保存，可中断后继续。"
            />
          </div>
        )}
        <div className="panel-bottom">
          <span>240% / 150% 为观察参考线，不代表统一监管阈值。</span>
          <span>来源：中证数据</span>
        </div>
      </section>
    </div>
  )
}
