import { useEffect, useRef, useState } from 'react'
import { PageHeaderActions } from './PageHeaderActions'
import { RefreshCw } from 'lucide-react'
import {
  getDividendStatus,
  updateDividend,
  DividendRequestError,
  type DividendCollectionStatus,
} from '../api/dividend'

// Shared by bank/nonbank routes so an accepted asynchronous POST never
// becomes clickable while a stale status response still reports idle.
const mutation = {
  pending: false,
  awaiting: false,
  uncertain: false,
  baselineId: null as string | null,
  baselineAttempt: null as string | null,
  observedRunning: false,
}

const stageLabels: Record<string, string> = {
  collecting: '准备采集',
  universe: '核验股票池',
  quotes: '采集收盘行情与停牌状态',
  valuations: '采集并核验估值',
  dividends: '采集分红方案',
  ranking: '筛选银行与非银行候选',
  history: '核验历史股息率',
  validating: '校验完整结果',
  publishing: '写入共享数据库',
  syncing_local: '同步并校验本地缓存',
  complete: '更新完成',
  failed: '采集失败',
  publication_pending: '等待恢复同步',
}
const stageLabel = (stage?: string | null) => (stage ? stageLabels[stage] || stage : '准备采集')

export function DividendCollectionControls({ onComplete }: { onComplete: () => void }) {
  const [status, setStatus] = useState<DividendCollectionStatus | null>(null)
  const [error, setError] = useState('')
  const [pollError, setPollError] = useState('')
  const [notice, setNotice] = useState('')
  const [busy, setBusy] = useState(mutation.pending)
  const [awaiting, setAwaiting] = useState(mutation.awaiting)
  const previous = useRef<DividendCollectionStatus | null>(null)
  const mounted = useRef(false)
  const generation = useRef(0)
  const statusRequest = useRef<AbortController | null>(null)

  const acceptStatus = (next: DividendCollectionStatus) => {
    const old = previous.current
    const wasAwaiting = mutation.awaiting
    if (next.running) mutation.observedRunning = true
    const changedRun = !!next.run_id && next.run_id !== mutation.baselineId
    const changedAttempt =
      !!next.last_attempt_at && next.last_attempt_at !== mutation.baselineAttempt
    if (
      mutation.awaiting &&
      !mutation.pending &&
      !next.running &&
      (mutation.observedRunning || changedRun || changedAttempt)
    ) {
      mutation.awaiting = false
      mutation.uncertain = false
      mutation.observedRunning = false
    }
    if (
      old &&
      ((old.running && !next.running) ||
        old.last_success_at !== next.last_success_at ||
        (wasAwaiting && !mutation.awaiting))
    )
      onComplete()
    previous.current = next
    setStatus(next)
    setBusy(mutation.pending)
    setAwaiting(mutation.awaiting)
  }

  async function readStatus() {
    statusRequest.current?.abort()
    const controller = new AbortController()
    statusRequest.current = controller
    const epoch = ++generation.current
    try {
      const next = await getDividendStatus(controller.signal)
      if (
        !mounted.current ||
        controller.signal.aborted ||
        epoch !== generation.current ||
        mutation.pending
      )
        return
      acceptStatus(next)
      setPollError('')
      return next
    } catch (err) {
      if (mounted.current && !controller.signal.aborted && epoch === generation.current)
        setPollError(err instanceof Error ? err.message : '读取采集状态失败')
    }
  }
  useEffect(() => {
    mounted.current = true
    let stopped = false
    let timer: ReturnType<typeof setTimeout>
    async function poll() {
      if (!mutation.pending) await readStatus()
      if (!stopped) timer = setTimeout(poll, 2000)
    }
    void poll()
    return () => {
      stopped = true
      mounted.current = false
      generation.current++
      statusRequest.current?.abort()
      clearTimeout(timer)
    }
    // onComplete is a stable useResource callback.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [onComplete])

  async function act() {
    if (
      mutation.pending ||
      mutation.awaiting ||
      !previous.current ||
      previous.current.running ||
      !previous.current.available ||
      pollError
    )
      return
    mutation.pending = true
    mutation.uncertain = false
    mutation.baselineId = previous.current.run_id ?? null
    mutation.baselineAttempt = previous.current.last_attempt_at ?? null
    mutation.observedRunning = false
    generation.current++
    statusRequest.current?.abort()
    setBusy(true)
    setError('')
    setNotice('')
    try {
      const result = await updateDividend('refresh')
      mutation.awaiting = result.started === true
      if (mounted.current) {
        setAwaiting(mutation.awaiting)
        if (!result.started) setNotice('其他业绩选股任务正在运行，请等待完成后再更新。')
      }
    } catch (err) {
      // A lost response is not a definite rejection. Wait for a new run or
      // its terminal result, just like an accepted request.
      mutation.awaiting = !(
        err instanceof DividendRequestError &&
        err.status >= 400 &&
        err.status < 500
      )
      mutation.uncertain = mutation.awaiting
      if (mounted.current) {
        setAwaiting(mutation.awaiting)
        setError(err instanceof Error ? err.message : '操作失败')
      }
    } finally {
      mutation.pending = false
      if (mounted.current) {
        setBusy(false)
        await readStatus()
      }
    }
  }

  const running = !!status?.running || awaiting
  const stageStarted = Date.parse(status?.stage_started_at || '')
  const stageSeconds = Number.isFinite(stageStarted)
    ? Math.max(0, Math.floor((Date.now() - stageStarted) / 1000))
    : null

  return (
    <section className="panel dividend-collection" aria-label="高股息数据采集">
      <div className="panel-heading">
        <div>
          <h2>数据采集</h2>
          <p>一次更新银行与非银行选股结果</p>
        </div>
        <PageHeaderActions>
          <button
            className="button button-primary"
            disabled={busy || !status || running || !!pollError || !status.available}
            onClick={() => void act()}
          >
            <RefreshCw size={16} className={busy || running ? 'spin' : ''} />
            {running ? '采集中…' : busy ? '提交中…' : '更新数据'}
          </button>
        </PageHeaderActions>
      </div>
      {status && (
        <div className="growth-dividend-progress" aria-live="polite">
          <div className="growth-dividend-progress-label">
            <span>
              {awaiting && !status.running
                ? '等待确认本次采集状态'
                : status.interrupted
                  ? '上次采集中断'
                  : status.stage
                    ? stageLabel(status.stage)
                    : '等待更新'}
            </span>
            {status.stage === 'history' && status.total != null && status.processed != null && (
              <span>
                已处理 {status.processed} / {status.total} 只（含历史数据不可用）
              </span>
            )}
          </div>
          {running && <progress aria-label="高股息采集更新进度" />}
          {running && (
            <p>
              按实际阶段更新，耗时取决于数据源和数据库响应。
              {stageSeconds != null && ` 当前阶段已用时 ${stageSeconds} 秒。`}
            </p>
          )}
          {!running && status.failed_stage && <p>停止阶段：{stageLabel(status.failed_stage)}</p>}
          {status.last_success_at && (
            <p>
              上次成功更新：
              {new Date(status.last_success_at).toLocaleString('zh-CN', {
                timeZone: 'Asia/Shanghai',
                hour12: false,
              })}
            </p>
          )}
        </div>
      )}
      {(error || pollError || status?.error) && (
        <p role="alert">{error || pollError || status?.error}</p>
      )}
      {notice && !running && <p role="status">{notice}</p>}
      {status?.publication_pending && !running && (
        <p role="status">已有待发布结果，更新数据将优先恢复数据库及本地同步。</p>
      )}
      {mutation.uncertain && awaiting && (
        <p role="status">提交结果待确认，正在读取采集状态，请勿重复启动。</p>
      )}
      {status && !status.available && (
        <p role="status">{status.unavailable_reason || '采集暂不可用'}</p>
      )}
      {status?.interrupted && !status.error && <p role="status">上次采集中断，可以重新更新。</p>}
    </section>
  )
}
