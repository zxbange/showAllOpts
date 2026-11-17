import { useEffect, useRef, useState } from 'react'
import { PageHeaderActions } from './PageHeaderActions'
import { RefreshCw } from 'lucide-react'
import { getDividendStatus, updateDividend, type DividendCollectionStatus } from '../api/dividend'

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
  const [busy, setBusy] = useState(false)
  const previous = useRef<DividendCollectionStatus | null>(null)
  const command = useRef<AbortController | null>(null)

  const acceptStatus = (next: DividendCollectionStatus) => {
    const old = previous.current
    if (old && ((old.running && !next.running) || old.last_success_at !== next.last_success_at))
      onComplete()
    previous.current = next
    setStatus(next)
  }

  useEffect(() => {
    const controller = new AbortController()
    let timer: ReturnType<typeof setTimeout>
    async function poll() {
      try {
        const next = await getDividendStatus(controller.signal)
        if (!controller.signal.aborted) {
          acceptStatus(next)
          setPollError('')
        }
      } catch (err) {
        if (!controller.signal.aborted)
          setPollError(err instanceof Error ? err.message : '读取采集状态失败')
      } finally {
        if (!controller.signal.aborted) timer = setTimeout(poll, 2000)
      }
    }
    void poll()
    return () => {
      controller.abort()
      clearTimeout(timer)
      command.current?.abort()
    }
    // onComplete is a stable useResource callback.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [onComplete])

  async function act(action: 'refresh' | 'cancel') {
    if (command.current) return
    const controller = new AbortController()
    command.current = controller
    setBusy(true)
    setError('')
    setNotice('')
    try {
      const result = await updateDividend(action, controller.signal)
      const next = await getDividendStatus(controller.signal)
      if (controller.signal.aborted) return
      acceptStatus(next)
      if (action === 'refresh' && !result.started && !next.running) {
        setNotice('其他业绩选股任务正在运行，请等待完成后再更新。')
      } else if (action === 'cancel') {
        setNotice(result.cancelled ? '已请求取消，正在等待当前步骤结束。' : '采集已结束。')
      }
    } catch (err) {
      if (!controller.signal.aborted) setError(err instanceof Error ? err.message : '操作失败')
    } finally {
      if (!controller.signal.aborted) setBusy(false)
      command.current = null
    }
  }

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
            disabled={busy || !status || status.running || !status.available}
            onClick={() => void act('refresh')}
          >
            <RefreshCw size={16} className={status?.running ? 'spin' : ''} />
            {status?.running ? '采集中' : status?.publication_pending ? '恢复同步' : '更新数据'}
          </button>
          {status?.running && (
            <button
              className="button button-quiet"
              disabled={busy}
              onClick={() => void act('cancel')}
            >
              取消采集
            </button>
          )}
        </PageHeaderActions>
      </div>
      {status && (
        <div className="growth-dividend-progress" aria-live="polite">
          <div className="growth-dividend-progress-label">
            <span>
              {status.interrupted
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
          {status.running && <progress aria-label="高股息采集更新进度" />}
          {status.running && (
            <p>
              按实际阶段更新，耗时取决于数据源和数据库响应。
              {stageSeconds != null && ` 当前阶段已用时 ${stageSeconds} 秒。`}
            </p>
          )}
          {!status.running && status.failed_stage && (
            <p>停止阶段：{stageLabel(status.failed_stage)}</p>
          )}
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
      {notice && <p role="status">{notice}</p>}
      {status && !status.available && (
        <p role="status">{status.unavailable_reason || '采集暂不可用'}</p>
      )}
      {status?.interrupted && !status.error && <p role="status">上次采集中断，可以重新更新。</p>}
    </section>
  )
}
