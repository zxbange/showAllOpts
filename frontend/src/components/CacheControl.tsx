import { useCallback, useEffect, useRef, useState } from 'react'
import { Database, RefreshCw, X } from 'lucide-react'
import { cancelCache, getCacheStatus, refreshCache } from '../api/client'
import type { CacheStatus } from '../types/cache'
import '../styles/cache.css'

function formatTime(value: string | null) {
  return value
    ? new Date(value).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai', hour12: false })
    : '尚未同步'
}

export function CacheControl() {
  const [status, setStatus] = useState<CacheStatus | null>(null)
  const [error, setError] = useState('')
  const [message, setMessage] = useState('')
  const [open, setOpen] = useState(false)
  const [module, setModule] = useState('')
  const [stopping, setStopping] = useState(false)
  const [submitting, setSubmitting] = useState(false)
  const dialog = useRef<HTMLDialogElement>(null)
  const signature = useRef<string | null>(null)
  const mounted = useRef(false)
  const controller = useRef<AbortController | null>(null)
  const load = useCallback(async () => {
    controller.current?.abort()
    const active = new AbortController()
    controller.current = active
    try {
      const result = await getCacheStatus(active.signal)
      if (!mounted.current || active.signal.aborted) return
      setStatus(result)
      setError('')
      const next = JSON.stringify(result.modules.map((m) => [m.id, m.updated_at, m.ready]))
      if (signature.current !== null && signature.current !== next) {
        window.dispatchEvent(new Event('data-cache-updated'))
      }
      signature.current = next
    } catch (reason) {
      if (!active.signal.aborted && mounted.current) {
        setError(reason instanceof Error ? reason.message : '暂时无法读取同步状态。')
      }
    }
  }, [])
  useEffect(() => {
    mounted.current = true
    void load()
    return () => {
      mounted.current = false
      controller.current?.abort()
    }
  }, [load])
  useEffect(() => {
    const timer = window.setInterval(() => void load(), status?.refreshing || open ? 3000 : 30000)
    return () => window.clearInterval(timer)
  }, [load, status?.refreshing, open])
  useEffect(() => {
    if (open) dialog.current?.showModal()
    else dialog.current?.close()
  }, [open])
  const busy = submitting || status?.refreshing
  const issues = status?.modules.filter((m) => !m.ready || m.error).length ?? 0
  async function sync() {
    if (busy) return
    setSubmitting(true)
    setMessage('')
    try {
      await refreshCache(module || null)
      setMessage('同步任务已启动，完成后页面会自动读取新快照。')
      await load()
    } catch (reason) {
      setMessage(reason instanceof Error ? reason.message : '无法启动同步。')
    } finally {
      if (mounted.current) setSubmitting(false)
    }
  }
  async function cancel() {
    if (!status?.run_id || stopping || status.cancelling) return
    setStopping(true)
    try {
      await cancelCache(status.run_id)
      setMessage('已请求停止本次同步及等待中的模块，保留成功快照和续传断点。')
      await load()
    } catch (reason) {
      setMessage(reason instanceof Error ? reason.message : '无法停止同步。')
      await load()
    } finally {
      if (mounted.current) setStopping(false)
    }
  }
  return (
    <>
      <button
        className={`cache-trigger ${issues || error ? 'cache-attention' : ''}`}
        onClick={() => setOpen(true)}
        aria-label="数据同步状态"
      >
        {status?.refreshing ? (
          <RefreshCw size={16} className="cache-spin" />
        ) : (
          <Database size={16} />
        )}
        <span>{status?.refreshing ? '同步中' : status?.enabled ? '本地数据' : '数据同步'}</span>
        {issues > 0 && <b>{issues}</b>}
      </button>
      <dialog
        ref={dialog}
        className="cache-dialog"
        aria-labelledby="cache-title"
        onClose={() => setOpen(false)}
      >
        <div className="cache-heading">
          <div>
            <span className="eyebrow">DATA SYNC</span>
            <h2 id="cache-title">数据同步</h2>
          </div>
          <button className="cache-close" onClick={() => setOpen(false)} aria-label="关闭数据同步">
            <X size={20} />
          </button>
        </div>
        <p className="cache-intro">
          {status?.enabled
            ? `页面读取本地快照，启动时不自动同步。${status.auto_refresh ? `每天北京时间 ${status.daily_time} 自动同步` : '自动同步已关闭'}，同步失败时保留上次成功数据。`
            : status
              ? '当前使用数据库直连，尚未启用本地缓存。'
              : '正在读取同步状态…'}
        </p>
        {error && (
          <p className="cache-message" role="alert">
            {error}
          </p>
        )}
        {status?.enabled && (
          <>
            <div className="cache-list">
              {status.modules.map((item) => (
                <div key={item.id} className="cache-row">
                  <div>
                    <strong>{item.name}</strong>
                    <small>上次成功同步 · {formatTime(item.updated_at)}</small>
                    {item.progress && item.progress.status !== 'idle' && (
                      <div>
                        <progress
                          className="cache-progress"
                          aria-label={`${item.name}同步进度`}
                          max={100}
                          value={
                            item.progress.status === 'complete' ? 100 : (item.progress.percent ?? 0)
                          }
                        />
                        <small>
                          阶段进度{' '}
                          {item.progress.status === 'complete' ? 100 : (item.progress.percent ?? 0)}
                          % ·
                          {item.progress.status === 'interrupted'
                            ? '任务已中断，可手动同步'
                            : item.progress.stage}
                          {item.progress.processed
                            ? ` · 已处理 ${item.progress.processed.toLocaleString()} 行（含对比与重试）`
                            : ''}
                          {item.progress.elapsed_seconds
                            ? ` · ${Math.round(item.progress.elapsed_seconds)} 秒`
                            : ''}
                        </small>
                      </div>
                    )}
                    {item.error && !['waiting', 'running'].includes(item.progress?.status ?? '') && <p className="cache-row-error">{item.error}</p>}
                  </div>
                  <span
                    className={`cache-badge ${item.error || !item.ready ? 'cache-badge-warn' : ''}`}
                  >
                    {item.ready ? (item.error ? '保留旧快照' : '已就绪') : '待同步'}
                  </span>
                </div>
              ))}
            </div>
            <form
              className="cache-form"
              onSubmit={(event) => {
                event.preventDefault()
                void sync()
              }}
            >
              <div className="cache-form-title">
                <Database size={17} />
                <strong>从数据库同步</strong>
              </div>
              <p>选择全部模块或指定模块，点击即可同步最新数据。</p>
              <div className="cache-fields">
                <label>
                  同步范围
                  <select
                    aria-label="同步范围"
                    value={module}
                    onChange={(e) => setModule(e.target.value)}
                    disabled={busy}
                  >
                    <option value="">全部模块</option>
                    {status.modules.map((item) => (
                      <option key={item.id} value={item.id}>
                        {item.name}
                      </option>
                    ))}
                  </select>
                </label>
              </div>
              <button type="submit" className="cache-submit" disabled={busy}>
                <RefreshCw size={15} className={busy ? 'cache-spin' : ''} />
                {busy ? '同步进行中…' : '立即同步'}
              </button>
              {status.refreshing && (
                <button type="button" className="cache-submit" onClick={() => void cancel()}
                  disabled={stopping || status.cancelling || !status.run_id}>
                  {stopping || status.cancelling ? '正在停止…' : '停止本次同步'}
                </button>
              )}
              {status.cancelling && <p role="status">正在等待当前数据库操作结束；保留成功快照和续传断点。</p>}
              {message && (
                <p role="status" className="cache-message">
                  {message}
                </p>
              )}
            </form>
            <p className="cache-footnote">
              百分比按执行阶段推进，不代表剩余时间比例；大表读取时可能停留，行数仍会更新。同步时间不等于数据交易日；模块页面会显示实际数据日期。页面上的刷新按钮重新读取当前快照。
            </p>
          </>
        )}
      </dialog>
    </>
  )
}
