import { useEffect, useMemo, useRef, useState } from 'react'
import { useAuth } from '../components/AccountAccess'
import { apiFetch } from '../api/http'
import '../styles/holdings.css'

type Position = { strategy2: string; code: string; name: string; amt: string }
type Event = {
  id: number
  strategy2: string
  code: string
  name: string
  delta: string
  before: string
  after: string
  kind: string
  observed_at: string
}
type Status = {
  enabled: boolean
  configured: boolean
  baseline: boolean
  rows: Position[]
  version: number
  window_open: boolean
  trading_day: boolean | null
  calendar_error: string | null
  error: string | null
  observed_at: string | null
}
type Config = {
  host: string
  port: number
  database: string
  username: string
  table: string
  password_set?: boolean
}
type History = { days: string[]; events: Event[]; next_before: number | null }
const empty: Config = { host: '', port: 3306, database: '', username: '', table: '' }
const names: Record<string, string> = { buy: '买入', add: '加仓', reduce: '减仓', clear: '清仓' }
const today = () =>
  new Intl.DateTimeFormat('en-CA', {
    timeZone: 'Asia/Shanghai',
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
  }).format(new Date())
const stamp = (value: string | null) =>
  value
    ? new Date(value).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai', hour12: false })
    : '尚未读取'

async function request<T>(path: string, body?: unknown, signal?: AbortSignal): Promise<T> {
  const response = await apiFetch('/api/v1/personal-holdings' + path, {
    method: body === undefined ? 'GET' : 'POST',
    cache: 'no-store',
    signal,
    headers: { 'Content-Type': 'application/json' },
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  })
  const result = await response.json()
  if (response.status === 403 && signal && !signal.aborted)
    window.dispatchEvent(new Event('account-session-expired'))
  if (!response.ok) throw new Error(result.detail || '持仓请求失败，请稍后重试')
  return result
}

export function PersonalHoldingsPage() {
  const { session } = useAuth()
  return session?.user?.role === 'admin' ? (
    <Holdings />
  ) : (
    <section className="empty-state">
      <h1>个人持仓</h1>
      <p>仅管理员可访问</p>
    </section>
  )
}

function Holdings() {
  const [status, setStatus] = useState<Status | null>(null)
  const [config, setConfig] = useState<Config>(empty)
  const [saved, setSaved] = useState<Config | null>(null)
  const [password, setPassword] = useState('')
  const [passwordEdited, setPasswordEdited] = useState(false)
  const [busy, setBusy] = useState(false)
  const [configurationLoaded, setConfigurationLoaded] = useState(false)
  const [error, setError] = useState('')
  const [pollError, setPollError] = useState('')
  const [test, setTest] = useState<'idle' | 'success' | 'failed'>('idle')
  const [day, setDay] = useState(today)
  const [history, setHistory] = useState<History>({ days: [], events: [], next_before: null })
  const [historyError, setHistoryError] = useState('')
  const [loadingMore, setLoadingMore] = useState(false)
  const generation = useRef(0)
  const mutating = useRef(false)
  const mounted = useRef(true)
  const historyGeneration = useRef(0)
  const groups = useMemo(() => {
    const rows = new Map<string, Position[]>()
    for (const row of status?.rows ?? []) {
      if (!rows.has(row.strategy2)) rows.set(row.strategy2, [])
      rows.get(row.strategy2)!.push(row)
    }
    return [...rows]
  }, [status?.rows])
  useEffect(() => {
    mounted.current = true
    const controller = new AbortController()
    let timer: ReturnType<typeof setTimeout>
    request<Config | null>('/configuration', undefined, controller.signal)
      .then((value) => {
        if (!controller.signal.aborted && value) {
          setConfig(value)
          setSaved(value)
        }
      })
      .catch((err) => {
        if (!controller.signal.aborted) setError(err.message)
      })
      .finally(() => {
        if (!controller.signal.aborted) setConfigurationLoaded(true)
      })
    async function poll() {
      const token = generation.current
      try {
        if (mutating.current) return
        const next = await request<Status>('/status', undefined, controller.signal)
        if (!controller.signal.aborted && token === generation.current) {
          setPollError('')
          setStatus((old) => ({
            ...next,
            rows: old?.version === next.version ? old.rows : next.rows,
          }))
        }
      } catch (err) {
        if (!controller.signal.aborted && token === generation.current)
          setPollError((err as Error).message)
      } finally {
        if (!controller.signal.aborted) timer = setTimeout(poll, 15000)
      }
    }
    void poll()
    return () => {
      mounted.current = false
      controller.abort()
      clearTimeout(timer)
    }
  }, [])
  useEffect(() => {
    const controller = new AbortController()
    const token = ++historyGeneration.current
    setHistory({ days: [], events: [], next_before: null })
    setHistoryError('')
    request<History>('/history?day=' + day, undefined, controller.signal)
      .then((value) => {
        if (!controller.signal.aborted && token === historyGeneration.current) setHistory(value)
      })
      .catch((err) => {
        if (!controller.signal.aborted) setHistoryError(err.message)
      })
    return () => controller.abort()
  }, [day, status?.version])
  function edit(key: keyof Config, value: string) {
    setConfig((old) => ({ ...old, [key]: key === 'port' ? Number(value) : value }))
    setTest('idle')
    setError('')
  }
  const unchangedTarget =
    saved &&
    ['host', 'port', 'database', 'username', 'table'].every(
      (key) => config[key as keyof Config] === saved[key as keyof Config],
    )
  async function operate(action: 'test' | 'configuration' | 'start' | 'stop') {
    if (mutating.current) return
    mutating.current = true
    setBusy(true)
    setError('')
    generation.current++
    try {
      if (action === 'test' || action === 'configuration') {
        const { password_set: _state, ...fields } = config
        const result = await request<Config>('/' + action, {
          ...fields,
          password: passwordEdited ? password : null,
        })
        if (!mounted.current) return
        if (action === 'test') setTest('success')
        else {
          setSaved(result)
          setConfig(result)
          setPassword('')
          setPasswordEdited(false)
          setTest('idle')
          setStatus(await request<Status>('/status'))
        }
      } else {
        const result = await request<Status>('/' + action, {})
        if (mounted.current) setStatus(result)
      }
    } catch (err) {
      if (mounted.current) {
        setError((err as Error).message)
        if (action === 'test') setTest('failed')
      }
    } finally {
      generation.current++
      mutating.current = false
      if (mounted.current) setBusy(false)
    }
  }
  async function more() {
    if (loadingMore || !history.next_before) return
    const token = historyGeneration.current
    setLoadingMore(true)
    try {
      const result = await request<History>(`/history?day=${day}&before=${history.next_before}`)
      if (mounted.current && token === historyGeneration.current)
        setHistory((old) => ({ ...result, events: [...old.events, ...result.events] }))
    } catch (err) {
      if (mounted.current && token === historyGeneration.current)
        setHistoryError((err as Error).message)
    } finally {
      if (mounted.current) setLoadingMore(false)
    }
  }
  return (
    <div className="holdings-page">
      <header className="holdings-heading">
        <div>
          <span className="eyebrow">管理员专属</span>
          <h1>个人持仓</h1>
          <p>按策略查看持仓，保存每次观测到的仓位变化。</p>
        </div>
        <span className="holdings-status">
          {!status
            ? '加载中'
            : !status.enabled
              ? '监控已停止'
              : status.calendar_error
                ? '日历待更新'
                : status.window_open
                  ? '监控中'
                  : '等待交易时段'}
        </span>
      </header>
      <section className="holdings-toolbar">
        <div>
          <strong>交易日 09:25–15:10 · 北京时间</strong>
          <p>每15秒查询，包含午间。关页后后台继续；首次完整读取建立基准，不记买入。</p>
          <small>最近成功读取：{stamp(status?.observed_at ?? null)}</small>
        </div>
        <button
          className="button button-primary"
          disabled={busy || !status?.configured}
          onClick={() => void operate(status?.enabled ? 'stop' : 'start')}
        >
          {status?.enabled ? '停止监控' : '开始监控'}
        </button>
      </section>
      {(error || pollError || status?.error || status?.calendar_error) && (
        <p className="holdings-error" role="alert">
          {error || pollError || status?.calendar_error || status?.error}
        </p>
      )}
      <details
        className="holdings-card holdings-configuration"
        open={!status?.configured || undefined}
      >
        <summary>数据库配置</summary>
        <form
          onSubmit={(event) => {
            event.preventDefault()
            void operate('configuration')
          }}
        >
          <fieldset disabled={busy || !configurationLoaded}>
            <div className="holdings-form">
              {(
                [
                  ['host', '主机'],
                  ['port', '端口'],
                  ['database', '数据库'],
                  ['username', '用户名'],
                  ['table', '持仓表名'],
                ] as const
              ).map(([key, label]) => (
                <label key={key}>
                  {label}
                  <input
                    required
                    type={key === 'port' ? 'number' : 'text'}
                    min={key === 'port' ? 1 : undefined}
                    max={key === 'port' ? 65535 : undefined}
                    value={config[key]}
                    onChange={(event) => edit(key, event.target.value)}
                    autoComplete="off"
                  />
                </label>
              ))}
              <label>
                密码
                <input
                  type="password"
                  value={password}
                  placeholder={
                    !passwordEdited && unchangedTarget && saved?.password_set ? '********' : ''
                  }
                  autoComplete="new-password"
                  onChange={(event) => {
                    setPassword(event.target.value)
                    setPasswordEdited(true)
                    setTest('idle')
                  }}
                />
              </label>
            </div>
            <small>
              密码占位表示已保存。修改连接或表名时需重新输入；编辑后清空表示使用空密码。源表须含
              strategy2、name、amt、code，股票代码须为文本。
            </small>
            <div className="holdings-actions">
              <button
                type="button"
                className={`button holdings-test ${test}`}
                aria-label={
                  test === 'success'
                    ? '测试连接成功'
                    : test === 'failed'
                      ? '测试连接失败'
                      : '测试连接'
                }
                onClick={() => void operate('test')}
              >
                测试连接 {test === 'success' ? '✓' : test === 'failed' ? '×' : ''}
              </button>
              <button type="submit" className="button">
                保存
              </button>
            </div>
          </fieldset>
        </form>
        <small>更换数据源会停止监控并建立新基准，历史记录保留。保存后点击“开始监控”。</small>
      </details>
      <section aria-label="当前持仓">
        <div className="holdings-section-title">
          <h2>当前持仓</h2>
          <span>
            {groups.length} 个策略 · {status?.rows.length ?? 0} 项持仓
          </span>
        </div>
        {!status?.baseline ? (
          <p className="holdings-card">等待首次完整持仓快照。</p>
        ) : groups.length === 0 ? (
          <p className="holdings-card">当前无持仓（最近一次完整查询为空）。</p>
        ) : (
          <div className="holdings-groups">
            {groups.map(([strategy, rows]) => (
              <article className="holdings-card" key={strategy}>
                <header>
                  <h3>{strategy}</h3>
                  <span>{rows.length} 只</span>
                </header>
                <div
                  className="holdings-scroll"
                  tabIndex={0}
                  role="region"
                  aria-label="持仓表格，可横向滚动"
                >
                  <table>
                    <thead>
                      <tr>
                        <th>股票</th>
                        <th>代码</th>
                        <th>股数</th>
                      </tr>
                    </thead>
                    <tbody>
                      {rows.map((row) => (
                        <tr key={row.code}>
                          <td>{row.name}</td>
                          <td>{row.code}</td>
                          <td>{row.amt}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </article>
            ))}
          </div>
        )}
      </section>
      <section className="holdings-card">
        <div className="holdings-section-title">
          <h2>仓位变动</h2>
          <label>
            记录日期{' '}
            <input
              aria-label="记录日期"
              type="date"
              value={day}
              onChange={(event) => setDay(event.target.value)}
            />
          </label>
        </div>
        <p>记录的是两次完整快照间的净变化，不代表逐笔成交；时间为北京时间的实际检测时间。</p>
        {history.days.length > 0 && (
          <label>
            历史日期{' '}
            <select
              aria-label="历史日期"
              value={history.days.includes(day) ? day : ''}
              onChange={(event) => {
                if (event.target.value) setDay(event.target.value)
              }}
            >
              <option value="">选择已有记录的日期</option>
              {history.days.map((value) => (
                <option key={value}>{value}</option>
              ))}
            </select>
          </label>
        )}
        {historyError && (
          <p role="alert" className="holdings-error">
            {historyError}
          </p>
        )}
        {!history.events.length ? (
          <p className="holdings-empty">当天暂无仓位变动记录。</p>
        ) : (
          <div
            className="holdings-scroll"
            tabIndex={0}
            role="region"
            aria-label="持仓表格，可横向滚动"
          >
            <table>
              <thead>
                <tr>
                  <th>检测时间</th>
                  <th>策略</th>
                  <th>股票 / 代码</th>
                  <th>变动</th>
                  <th>股数变化</th>
                  <th>变化后</th>
                </tr>
              </thead>
              <tbody>
                {history.events.map((event) => (
                  <tr key={event.id}>
                    <td>{stamp(event.observed_at)}</td>
                    <td>{event.strategy2}</td>
                    <td>
                      {event.name}
                      <small>{event.code}</small>
                    </td>
                    <td>
                      <span className={'holdings-event ' + event.kind}>{names[event.kind]}</span>
                    </td>
                    <td>{event.delta.startsWith('-') ? event.delta : '+' + event.delta}</td>
                    <td>{event.after}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        {history.next_before && (
          <button className="button" disabled={loadingMore} onClick={() => void more()}>
            加载更多
          </button>
        )}
      </section>
    </div>
  )
}
