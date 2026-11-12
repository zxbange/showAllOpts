import { useEffect, useRef, useState } from 'react'
import { useAuth } from './AccountAccess'
import { getETFCollectionStatus } from '../api/client'
import { cancelCollection, runCollection } from '../api/etfCollection'
import type { ETFCollectionStatus } from '../types/etf'
import { collectionErrorMessage, withManualRun } from '../lib/etf'

const active = (s?: ETFCollectionStatus | null) =>
  ['queued', 'running', 'cancelling'].includes(s?.manual?.status ?? '') ||
  s?.status === 'running' ||
  !!s?.progress
const phases: Record<string, string> = {
  calendar: '读取交易日历',
  fetching: '获取交易所数据',
  writing: '写入数据库',
}
const labels: Record<string, string> = {
  queued: '排队中',
  cancelling: '正在取消',
  cancelled: '已取消',
  running: '采集中',
  complete: '本次日常采集完成',
  partial: '部分完成，仍有缺口',
  failed: '失败',
  interrupted: '已中断',
}
export function ETFCollectionPanel({ onCollected }: { onCollected: () => void }) {
  const admin = useAuth().session?.user?.role === 'admin'
  const [status, setStatus] = useState<ETFCollectionStatus | null>(null)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState('')
  const [uncertain, setUncertain] = useState(false)
  const [retryExhausted, setRetryExhausted] = useState(false)
  const retryChoice = useRef(false)
  const [refresh, setRefresh] = useState(0)
  const lock = useRef(false)
  const requestId = useRef<string | null>(null)
  const mutationVersion = useRef(0)
  const mounted = useRef(false)
  const reload = useRef(onCollected)
  reload.current = onCollected
  const previous = useRef<ETFCollectionStatus | null>(null)
  useEffect(() => {
    mounted.current = true
    return () => {
      mounted.current = false
    }
  }, [])
  useEffect(() => {
    let live = true
    let timer: ReturnType<typeof setTimeout>
    const controller = new AbortController()
    async function poll() {
      let delay = 15000
      try {
        const version = mutationVersion.current
        const next = await getETFCollectionStatus(controller.signal)
        if (!live) return
        if (version !== mutationVersion.current || lock.current) {
          timer = setTimeout(poll, 2000)
          return
        }
        if (requestId.current && next.manual?.id === requestId.current) {
          requestId.current = null
          setUncertain(false)
          setRetryExhausted(false)
          setError('')
        }
        const old = previous.current
        if (
          old &&
          !next.cache_pending &&
          (old.cache_pending ||
            (active(old) && !active(next)) ||
            (!!next.manual &&
              (old.manual?.id !== next.manual.id || old.manual?.status !== next.manual.status) &&
              !active(next)) ||
            (!!next.runs?.[0] &&
              (old.runs?.[0]?.slot !== next.runs[0].slot ||
                old.runs?.[0]?.status !== next.runs[0].status) &&
              !active(next)))
        )
          reload.current()
        previous.current = next
        setStatus(next)
        if (active(next) || next.cache_pending) delay = 2000
      } catch {
        // Keep saved charts visible while the next poll retries status retrieval.
      }
      if (live) timer = setTimeout(poll, delay)
    }
    void poll()
    return () => {
      live = false
      controller.abort()
      clearTimeout(timer)
    }
  }, [refresh])
  const running = active(status)
  const manualActive = ['queued', 'running', 'cancelling'].includes(status?.manual?.status ?? '')
  const cancelling = status?.manual?.status === 'cancelling'
  async function run() {
    if (!admin || lock.current || running || !status?.configured) return
    lock.current = true
    mutationVersion.current++
    setBusy('run')
    setError('')
    if (!requestId.current) retryChoice.current = retryExhausted && !!status.retry_exhausted_count
    requestId.current ??= Array.from(crypto.getRandomValues(new Uint8Array(16)), (byte) =>
      byte.toString(16).padStart(2, '0'),
    ).join('')
    try {
      const result = await runCollection(requestId.current, retryChoice.current)
      if (!mounted.current) return
      requestId.current = null
      setUncertain(false)
      setRetryExhausted(false)
      previous.current = withManualRun(previous.current ?? { status: 'configured' }, result)
      setStatus(previous.current)
      setRefresh((n) => n + 1)
    } catch (e) {
      if (mounted.current) {
        setUncertain(true)
        setError(
          `${e instanceof Error ? collectionErrorMessage(e.message) : '请求失败'} 可重试同一请求，避免重复采集。`,
        )
        setRefresh((n) => n + 1)
      }
    } finally {
      lock.current = false
      if (mounted.current) setBusy('')
    }
  }
  async function cancel() {
    const manual = status?.manual
    if (!admin || lock.current || !manual || !manualActive || cancelling) return
    lock.current = true
    mutationVersion.current++
    setBusy('cancel')
    setError('')
    try {
      const result = await cancelCollection(manual.id)
      if (!mounted.current) return
      previous.current = withManualRun(previous.current ?? { status: 'configured' }, result)
      setStatus(previous.current)
    } catch (e) {
      if (mounted.current)
        setError(
          `${e instanceof Error ? collectionErrorMessage(e.message) : '取消请求失败'} 取消结果待确认，将继续查询状态；可重试取消同一任务。`,
        )
    } finally {
      lock.current = false
      if (mounted.current) {
        setBusy('')
        setRefresh((n) => n + 1)
      }
    }
  }
  const progress = status?.progress
  if (!admin && !progress && !status?.cache_pending && !status?.runs?.length) return null
  return (
    <section className="panel etf-collection-panel" aria-label="ETF采集管理">
      <div className="etf-collection-overview">
        <div role="status">
          {status?.cache_pending && <p>采集已入库，等待图表缓存更新…</p>}
          {progress && (
            <p>
              {phases[progress.phase] ?? '处理中'} · 本轮目标日已处理 {progress.completed} /{' '}
              {progress.total} 项目标
              <progress value={progress.completed} max={progress.total || 1} />
            </p>
          )}
        </div>
        {admin && (
          <div>
            {!!status?.retry_exhausted_count && (
              <label className="etf-check">
                <input
                  type="checkbox"
                  checked={retryExhausted}
                  disabled={!!busy || running || uncertain}
                  onChange={(e) => setRetryExhausted(e.target.checked)}
                />
                重新尝试当前目标日已耗尽重试的数据（{status.retry_exhausted_count}项）
              </label>
            )}
            {uncertain && <p>重试会沿用上次请求的日常采集选项，不新增历史补采。</p>}
            {cancelling && <p>正在等待当前采集步骤结束，取消后可重新开始。</p>}
            <button
              className="button"
              disabled={!!busy || (!manualActive && (running || !status?.configured))}
              onClick={() => void (manualActive ? cancel() : run())}
            >
              {manualActive
                ? '取消采集'
                : busy === 'run'
                  ? '提交中…'
                  : running
                    ? '自动采集中…'
                    : uncertain
                      ? '重试同一采集请求'
                      : '立即采集'}
            </button>
          </div>
        )}
      </div>
      {error && (
        <p role="alert" className="etf-collection-warning">
          {error}
        </p>
      )}
      {!!status?.gap_count && (
        <p>
          本轮目标日仍有 {status.gap_count}{' '}
          项数据缺口；勾选重试仅重置当前日常目标，不回补历史。本次日常采集完成不代表历史数据齐全。
        </p>
      )}
      {!!status?.runs?.length && (
        <details>
          <summary>最近采集记录</summary>
          <ul>
            {status.runs.map((run, i) => (
              <li key={`${run.slot}-${i}`}>
                {run.slot} · {labels[run.status] ?? '状态待确认'}
                {run.error ? ` · ${collectionErrorMessage(run.error)}` : ''}
              </li>
            ))}
          </ul>
        </details>
      )}
    </section>
  )
}
