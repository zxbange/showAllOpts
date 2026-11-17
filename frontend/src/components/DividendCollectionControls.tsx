import { useEffect, useRef, useState } from 'react'
import { PageHeaderActions } from './PageHeaderActions'
import { RefreshCw } from 'lucide-react'
import { getDividendStatus, updateDividend, type DividendCollectionStatus } from '../api/dividend'

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
    if (old && ((old.running && !next.running) || old.last_success_at !== next.last_success_at)) onComplete()
    previous.current = next
    setStatus(next)
  }

  useEffect(() => {
    const controller = new AbortController()
    let timer: ReturnType<typeof setTimeout>
    async function poll() {
      try {
        const next = await getDividendStatus(controller.signal)
        if (!controller.signal.aborted) { acceptStatus(next); setPollError('') }
      } catch (err) {
        if (!controller.signal.aborted) setPollError(err instanceof Error ? err.message : '读取采集状态失败')
      } finally {
        if (!controller.signal.aborted) timer = setTimeout(poll, 2000)
      }
    }
    void poll()
    return () => { controller.abort(); clearTimeout(timer); command.current?.abort() }
    // onComplete is a stable useResource callback.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [onComplete])

  async function act(action: 'refresh' | 'cancel') {
    if (command.current) return
    const controller = new AbortController()
    command.current = controller
    setBusy(true); setError(''); setNotice('')
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

  return <section className="panel dividend-collection" aria-label="高股息数据采集">
    <div className="panel-heading">
      <div><h2>数据采集</h2><p>一次更新银行与非银行选股结果</p></div>
      <PageHeaderActions>
        <button className="button button-primary" disabled={busy || !status || status.running || !status.available} onClick={() => void act('refresh')}>
          <RefreshCw size={16} className={status?.running ? 'spin' : ''} />
          {status?.running ? '采集中' : status?.publication_pending ? '恢复同步' : '更新数据'}
        </button>
        {status?.running && <button className="button button-quiet" disabled={busy} onClick={() => void act('cancel')}>取消采集</button>}
      </PageHeaderActions>
    </div>
    {(error || pollError || status?.error) && <p role="alert">{error || pollError || status?.error}</p>}
    {notice && <p role="status">{notice}</p>}
    {status && !status.available && <p role="status">{status.unavailable_reason || '采集暂不可用'}</p>}
    {status?.interrupted && !status.error && <p role="status">上次采集中断，可以重新更新。</p>}
  </section>
}
