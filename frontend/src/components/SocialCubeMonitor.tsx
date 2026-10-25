import { useCallback, useEffect, useState } from 'react'
import { Clock3, ExternalLink, Layers3, Plus, RefreshCw } from 'lucide-react'

export type MonitoredCube = {
  id: number
  symbol: string
  name: string
  enabled: number
  initialized: number
  last_check: string | null
  last_success_at: string | null
  last_error: string | null
  scan_page: number
}

type PlatformState = {
  paused?: boolean
  reason?: string | null
  retry_at?: number
}

type Rebalance = {
  id: number
  cube_id: number
  cube_name: string
  cube_symbol: string
  rebalance_id: string
  status: string
  notify_status: 'baseline' | 'waiting' | 'pending' | 'sent'
  occurred_at: string
  discovered_at: string
  sent_at: string | null
  last_error: string | null
  payload: {
    time: string
    comment?: string
    changes: {
      symbol: string
      name: string
      price: number | null
      before: number | null
      after: number | null
      action: string
    }[]
  }
}

type RebalanceFeed = { total: number; page: number; items: Rebalance[] }

const notifications = {
  baseline: '历史记录',
  waiting: '待确认',
  pending: '待推送',
  sent: '已推送',
}
const statuses: Record<string, string> = {
  success: '调仓成功',
  pending: '处理中',
  waiting: '待执行',
  failed: '调仓失败',
  canceled: '已撤销',
  cancelled: '已撤销',
}

function date(value: string | null) {
  if (!value) return '尚未检查'
  const parsed = new Date(value)
  return Number.isNaN(parsed.getTime())
    ? '时间待确认'
    : parsed.toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai', hour12: false })
}

function percent(value: number | null) {
  return value !== null && Number.isFinite(value) ? `${value.toFixed(2)}%` : '—'
}

function cubeLink(symbol: string) {
  return /^ZH\d+$/i.test(symbol) ? `https://www.xueqiu.com/P/${symbol.toUpperCase()}` : undefined
}

async function request<T>(path: string, body?: unknown, signal?: AbortSignal): Promise<T> {
  const response = await fetch(`/api/v1/social-monitor${path}`, {
    method: body === undefined ? 'GET' : 'POST',
    headers: { 'Content-Type': 'application/json' },
    signal,
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  })
  const data = await response.json().catch(() => null)
  if (!response.ok)
    throw new Error(typeof data?.detail === 'string' ? data.detail : '组合监控请求失败，请重试')
  return data as T
}

function cubeState(
  cube: MonitoredCube,
  enabled: boolean,
  monitoringActive: boolean,
  platform?: PlatformState,
) {
  if (!cube.enabled) return { label: '已暂停', reason: '该组合已手动暂停。' }
  if (!enabled) return { label: '已暂停', reason: '总监控开关已关闭，请在上方启动监控。' }
  if (platform?.paused)
    return { label: '平台已暂停', reason: platform.reason || '请完成人工处理后恢复雪球监控。' }
  if (!monitoringActive) return { label: '非监控时段', reason: '' }
  if (platform?.retry_at && platform.retry_at * 1000 > Date.now())
    return { label: '退避等待中', reason: platform.reason || '平台请求暂时失败，正在等待重试。' }
  return { label: cube.initialized ? '监控中' : '等待首次检查', reason: '' }
}

export function SocialCubeMonitor({
  cubes,
  enabled,
  monitoringActive,
  platform,
  onChanged,
}: {
  cubes: MonitoredCube[]
  enabled: boolean
  monitoringActive: boolean
  platform?: PlatformState
  onChanged: () => Promise<void>
}) {
  const [url, setUrl] = useState('')
  const [name, setName] = useState('')
  const [filter, setFilter] = useState('')
  const [page, setPage] = useState(1)
  const [feed, setFeed] = useState<RebalanceFeed>({ total: 0, page: 1, items: [] })
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(false)
  const [revision, setRevision] = useState(0)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')

  const refresh = useCallback(
    async (signal?: AbortSignal) => {
      const query = new URLSearchParams({ page: String(page) })
      if (filter) query.set('cube_id', filter)
      const data = await request<RebalanceFeed>(`/cube-rebalances?${query}`, undefined, signal)
      if (!signal?.aborted) {
        setFeed(data)
        setLoading(false)
      }
    },
    [filter, page],
  )

  useEffect(() => {
    const abort = new AbortController()
    setLoading(true)
    const load = () =>
      void refresh(abort.signal).catch((err: unknown) => {
        if (!abort.signal.aborted) {
          setError(err instanceof Error ? err.message : '调仓记录加载失败')
          setLoading(false)
        }
      })
    load()
    const timer = window.setInterval(load, 10000)
    return () => {
      abort.abort()
      window.clearInterval(timer)
    }
  }, [refresh, revision])

  async function action(work: () => Promise<void>, message: string) {
    setBusy(true)
    setError('')
    setNotice('')
    try {
      await work()
      await onChanged()
      setRevision((current) => current + 1)
      setNotice(message)
    } catch (err) {
      setError(err instanceof Error ? err.message : '组合操作失败，请重试')
    } finally {
      setBusy(false)
    }
  }

  return (
    <section
      className="social-panel social-cubes"
      role="tabpanel"
      id="social-panel-cubes"
      aria-labelledby="social-tab-cubes"
    >
      <div className="social-panel-heading">
        <div>
          <h2>组合调仓</h2>
          <p className="social-muted">跟踪成功调仓，复用雪球登录与 Telegram 通知。</p>
        </div>
        <span className="social-count">共 {cubes.length} 个</span>
      </div>
      <div className="social-cube-window">
        <Clock3 size={16} />
        <div>
          <strong>北京时间 周一至周五 09:00～15:00</strong>
          <span>
            {monitoringActive
              ? '当前处于监控时段'
              : '当前为非监控时段，下一个监控窗口按队列补齐新调仓'}
          </span>
        </div>
      </div>
      {error && (
        <p className="social-error" role="alert">
          {error}
        </p>
      )}
      {notice && (
        <p className="social-notice" role="status">
          {notice}
        </p>
      )}
      <form
        className="social-cube-form"
        onSubmit={(event) => {
          event.preventDefault()
          void action(async () => {
            await request('/cubes', { url: url.trim(), name: name.trim() })
            setUrl('')
            setName('')
          }, '已添加组合；首次建立历史基线，添加并启动监控后的成功调仓会发送通知。')
        }}
      >
        <label>
          组合链接或代码
          <input
            value={url}
            onChange={(event) => setUrl(event.target.value)}
            placeholder="ZH679918 或雪球组合链接"
            required
            maxLength={500}
            disabled={busy}
          />
        </label>
        <label>
          组合备注
          <input
            value={name}
            onChange={(event) => setName(event.target.value)}
            placeholder="可选"
            maxLength={100}
            disabled={busy}
          />
        </label>
        <button className="button button-primary" disabled={busy}>
          <Plus size={16} />
          添加组合
        </button>
      </form>
      <div className="social-authors social-cube-list">
        {cubes.map((cube) => {
          const state = cubeState(cube, enabled, monitoringActive, platform)
          return (
            <article className="social-author social-cube-card" key={cube.id}>
              <div>
                <span className="social-platform xueqiu">雪球</span>
                <strong title={cube.name || cube.symbol}>{cube.name || cube.symbol}</strong>
              </div>
              <p className="social-muted">
                {cube.symbol} · {state.label}
                <br />
                最近检查：{date(cube.last_check)}
                {cube.scan_page > 1 && ` · 待补抓第 ${cube.scan_page} 页`}
              </p>
              {state.reason && <p className="social-author-reason">{state.reason}</p>}
              {cube.last_error && cube.last_error !== state.reason && (
                <p className="social-error">最近抓取错误：{cube.last_error}</p>
              )}
              <div className="social-actions">
                <button
                  className="button"
                  disabled={busy}
                  aria-label={`${cube.enabled ? '暂停' : '恢复'}组合 ${cube.symbol}`}
                  onClick={() =>
                    void action(
                      async () => {
                        await request(`/cubes/${cube.id}`, { enabled: !cube.enabled })
                      },
                      cube.enabled ? '组合已暂停。' : '组合已恢复，将在监控时段按队列检查。',
                    )
                  }
                >
                  {cube.enabled ? '暂停' : '恢复'}
                </button>
                <button
                  className="button"
                  disabled={busy}
                  aria-label={`删除组合 ${cube.symbol}`}
                  onClick={() => {
                    if (!window.confirm(`删除 ${cube.name || cube.symbol} 的监控及本地调仓记录？`))
                      return
                    void action(async () => {
                      await request(`/cubes/${cube.id}/delete`, {})
                      if (filter === String(cube.id)) {
                        setFilter('')
                        setPage(1)
                      }
                    }, '组合及本地调仓记录已删除。')
                  }}
                >
                  删除
                </button>
              </div>
            </article>
          )
        })}
      </div>
      {!cubes.length && (
        <div className="social-empty social-cube-empty">
          <Layers3 size={28} />
          <h3>添加要关注的组合</h3>
          <p>保存组合后，在监控时段建立基线并开始检查。</p>
        </div>
      )}
      <div className="social-feed-heading social-cube-feed-heading">
        <div>
          <h3>调仓记录</h3>
          <p className="social-muted">共 {feed.total} 条 · 北京时间 · 首次历史记录不推送</p>
        </div>
        <div className="social-actions">
          <select
            aria-label="按组合筛选"
            value={filter}
            onChange={(event) => {
              setFilter(event.target.value)
              setPage(1)
            }}
          >
            <option value="">全部组合</option>
            {cubes.map((cube) => (
              <option value={cube.id} key={cube.id}>
                {cube.name || cube.symbol}
              </option>
            ))}
          </select>
          <button
            className="button"
            disabled={busy || loading}
            onClick={() => void action(async () => {}, '调仓记录已刷新。')}
          >
            <RefreshCw size={15} />
            刷新调仓记录
          </button>
        </div>
      </div>
      <div aria-busy={loading}>
        {loading && !feed.items.length && (
          <p className="social-muted social-cube-loading" role="status">
            正在加载调仓记录…
          </p>
        )}
        {!loading && !feed.items.length && (
          <p className="social-muted social-cube-loading">暂无调仓记录，采集后会显示在这里。</p>
        )}
        {feed.items.map((record) => (
          <article className="social-rebalance" key={record.id}>
            <header>
              <div>
                <strong>{record.cube_name || record.cube_symbol}</strong>
                <span className="social-cube-symbol">{record.cube_symbol}</span>
              </div>
              <span className={`social-post-status ${record.notify_status}`}>
                {notifications[record.notify_status] || '状态待确认'}
              </span>
            </header>
            <p className="social-muted social-rebalance-time">
              <span>{statuses[record.status] || '状态待确认'}</span>
              <time>调仓时间：{date(record.occurred_at || record.payload.time)}</time>
              {record.sent_at && <span>推送时间：{date(record.sent_at)}</span>}
            </p>
            {!!record.payload.changes?.length && (
              <ul className="social-cube-changes">
                {record.payload.changes.map((change, index) => (
                  <li key={`${change.symbol}-${index}`}>
                    <div className="social-cube-stock">
                      <strong>{change.name || change.symbol}</strong>
                      <span>{change.symbol}</span>
                    </div>
                    <span className="social-cube-action">{change.action}</span>
                    <strong className="social-cube-weights">
                      {percent(change.before)} <span>→</span> {percent(change.after)}
                    </strong>
                    {change.price !== null && Number.isFinite(change.price) && (
                      <span className="social-cube-price">组合记录价：{change.price}</span>
                    )}
                  </li>
                ))}
              </ul>
            )}
            {!record.payload.changes?.length && (
              <p className="social-muted social-cube-no-changes">暂无可确认的股票调整明细。</p>
            )}
            {record.payload.comment && (
              <p className="social-cube-comment">{record.payload.comment}</p>
            )}
            {record.last_error && <p className="social-error">{record.last_error}</p>}
            {cubeLink(record.cube_symbol) && (
              <a
                className="social-original"
                href={cubeLink(record.cube_symbol)}
                target="_blank"
                rel="noreferrer"
              >
                查看组合 <ExternalLink size={14} />
              </a>
            )}
          </article>
        ))}
      </div>
      <div className="social-pagination">
        <button
          className="button"
          disabled={busy || loading || page <= 1}
          onClick={() => setPage(page - 1)}
        >
          上一页
        </button>
        <span>
          第 {page} / {Math.max(1, Math.ceil(feed.total / 20))} 页
        </span>
        <button
          className="button"
          disabled={busy || loading || page * 20 >= feed.total}
          onClick={() => setPage(page + 1)}
        >
          下一页
        </button>
      </div>
    </section>
  )
}
