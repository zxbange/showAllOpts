import { useEffect, useRef, useState } from 'react'
import { useAuth } from './AccountAccess'
import { getETFCollectionStatus } from '../api/client'
import {
  getCollectionConfiguration,
  runCollection,
  saveCollectionConfiguration,
  testCollectionConfiguration,
} from '../api/etfCollection'
import type { ETFCollectionConfiguration, ETFCollectionStatus } from '../types/etf'
import { collectionSummary, collectionErrorMessage } from '../lib/etf'

const active = (s?: ETFCollectionStatus | null) =>
  ['queued', 'running'].includes(s?.manual?.status ?? '') ||
  s?.status === 'running' ||
  !!s?.progress
const phases: Record<string, string> = {
  calendar: '读取交易日历',
  fetching: '获取交易所数据',
  writing: '写入数据库',
}
const labels: Record<string, string> = {
  queued: '排队中',
  running: '采集中',
  complete: '已完成',
  partial: '部分完成',
  failed: '失败',
  interrupted: '已中断',
}
export function ETFCollectionPanel({ onCollected }: { onCollected: () => void }) {
  const admin = useAuth().session?.user?.role === 'admin'
  const [status, setStatus] = useState<ETFCollectionStatus | null>(null)
  const [statusError, setStatusError] = useState('')
  const [config, setConfig] = useState<ETFCollectionConfiguration | null>(null)
  const [saved, setSaved] = useState<ETFCollectionConfiguration | null>(null)
  const [password, setPassword] = useState('')
  const [error, setError] = useState('')
  const [message, setMessage] = useState('')
  const [busy, setBusy] = useState('')
  const [uncertain, setUncertain] = useState(false)
  const [retryExhausted, setRetryExhausted] = useState(false)
  const retryChoice = useRef(false)
  const [dbAck, setDbAck] = useState(false)
  const [refresh, setRefresh] = useState(0)
  const [configVersion, setConfigVersion] = useState(0)
  const lock = useRef(false)
  const requestId = useRef<string | null>(null)
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
        const next = await getETFCollectionStatus(controller.signal)
        if (!live) return
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
        setStatusError('')
        if (active(next) || next.cache_pending) delay = 2000
      } catch {
        if (live) setStatusError('采集状态暂不可用，已保存数据仍可查看。')
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
  useEffect(() => {
    if (!admin) {
      setConfig(null)
      setSaved(null)
      setPassword('')
      return
    }
    let live = true
    const controller = new AbortController()
    getCollectionConfiguration(controller.signal)
      .then((value) => {
        if (live) {
          setConfig(value)
          setSaved(value)
          setError('')
        }
      })
      .catch(() => {
        if (live) setError('无法读取采集配置，请重试。')
      })
    return () => {
      live = false
      controller.abort()
    }
  }, [admin, configVersion])
  const running = active(status)
  const insecure = config && !config.transport_secure
  const remote =
    config && !(config.db_local ?? ['localhost', '127.0.0.1', '::1'].includes(config.host))
  const transportBlocked = !!insecure
  const dbBlocked = !!remote && !config?.db_tls && !dbAck
  const passwordNeeded =
    config &&
    (!saved?.password_set || saved.target_changed || config.username !== saved.username) &&
    !password
  async function submit(kind: 'save' | 'test') {
    if (
      !admin ||
      !config ||
      running ||
      lock.current ||
      transportBlocked ||
      dbBlocked ||
      passwordNeeded
    )
      return
    lock.current = true
    setBusy(kind)
    setError('')
    setMessage('')
    try {
      const { revision, username, enabled, lookback_days, batch_days, schedule, db_tls } = config
      const body = {
        revision,
        username,
        enabled,
        lookback_days,
        batch_days,
        schedule,
        db_tls,
        ...(password ? { password } : {}),
        db_transport_acknowledged: dbAck,
      }
      if (kind === 'save') {
        const value = await saveCollectionConfiguration(body)
        if (!mounted.current) return
        setConfig(value)
        setSaved(value)
        setPassword('')
        setMessage('采集配置已保存。')
        setRefresh((n) => n + 1)
      } else {
        const result = await testCollectionConfiguration(body)
        if (mounted.current)
          setMessage(`${result.message} 此测试仅读取连接和表，不代表写入权限验证通过。`)
      }
    } catch (e) {
      if (mounted.current)
        setError(e instanceof Error ? collectionErrorMessage(e.message) : '操作失败，请重试。')
    } finally {
      lock.current = false
      if (mounted.current) setBusy('')
    }
  }
  async function run() {
    if (!admin || lock.current || running || !status?.configured) return
    lock.current = true
    setBusy('run')
    setError('')
    setMessage('')
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
      previous.current = { ...(previous.current ?? { status: 'configured' }), manual: result }
      setStatus(previous.current)
      setMessage(`手动采集：${labels[result.status] ?? '状态待确认'}`)
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
  const progress = status?.progress
  return (
    <section className="panel etf-collection-panel" aria-label="ETF采集管理">
      <div className="etf-collection-overview">
        <div role="status">
          {statusError || (status ? collectionSummary(status) : '正在读取采集状态…')}
          {status?.manual && (
            <p>
              手动采集：{labels[status.manual.status] ?? '状态待确认'}
              {status.manual.error ? ` · ${collectionErrorMessage(status.manual.error)}` : ''}
            </p>
          )}
          {status?.cache_pending && <p>采集已入库，等待图表缓存更新…</p>}
          {progress && (
            <p>
              {phases[progress.phase] ?? '处理中'} · {progress.completed} / {progress.total}
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
                重新尝试已耗尽重试的数据（{status.retry_exhausted_count}项）
              </label>
            )}
            {uncertain && <p>重试会沿用上次请求的补采选项。</p>}
            <button
              className="button"
              disabled={!!busy || running || !status?.configured}
              onClick={() => void run()}
            >
              {busy === 'run'
                ? '提交中…'
                : running
                  ? '正在采集…'
                  : uncertain
                    ? '重试同一采集请求'
                    : '立即采集'}
            </button>
          </div>
        )}
      </div>
      <p className="etf-collection-hint">
        立即采集使用已保存配置，从交易所补采并写入数据；页面顶部刷新仅重读已保存数据。关闭自动采集后仍可手动采集。
      </p>
      {admin && (
        <details>
          <summary>采集配置</summary>
          {!config ? (
            <button className="button button-quiet" onClick={() => setConfigVersion((n) => n + 1)}>
              重新读取配置
            </button>
          ) : (
            <form
              onSubmit={(e) => {
                e.preventDefault()
                void submit('save')
              }}
            >
              <p>
                固定写入目标：{config.host}:{config.port} / {config.database} · 配置来源：
                {config.source === 'page'
                  ? '页面'
                  : config.source === 'environment'
                    ? '现有服务配置'
                    : '未配置'}
              </p>
              <p>
                使用独立采集账号，不要使用数据库管理员账号。仅需
                etf_share_daily、etf_price_daily、etf_trading_day 三张表的 SELECT / INSERT / UPDATE
                权限；首次缺表时还需 CREATE 权限。
              </p>
              {config.target_changed && (
                <p className="etf-collection-warning" role="alert">
                  数据库目标已变更，旧密码不会沿用，请重新输入新目标的密码并保存。
                </p>
              )}
              {insecure && (
                <div className="etf-collection-warning" role="alert">
                  当前连接为 HTTP，数据库账号和密码可能被截获。建议使用 HTTPS。
                  <p>请使用 HTTPS 后保存或测试凭据。</p>
                </div>
              )}
              <fieldset disabled={!!busy || running || transportBlocked}>
                <div className="etf-collection-fields">
                  <label>
                    采集写入账号
                    <input
                      autoComplete="off"
                      required
                      value={config.username}
                      onChange={(e) => setConfig({ ...config, username: e.target.value })}
                    />
                  </label>
                  <label>
                    数据库密码
                    <input
                      type="password"
                      autoComplete="new-password"
                      value={password}
                      placeholder={
                        saved?.password_set && !saved.target_changed
                          ? '留空保留已保存密码（账号不变时）'
                          : '请输入密码'
                      }
                      onChange={(e) => setPassword(e.target.value)}
                    />
                  </label>
                  <label>
                    回溯天数
                    <input
                      type="number"
                      min="1"
                      max="1500"
                      required
                      value={config.lookback_days}
                      onChange={(e) =>
                        setConfig({ ...config, lookback_days: Number(e.target.value) })
                      }
                    />
                  </label>
                  <label>
                    每批天数
                    <input
                      type="number"
                      min="1"
                      max="20"
                      required
                      value={config.batch_days}
                      onChange={(e) => setConfig({ ...config, batch_days: Number(e.target.value) })}
                    />
                  </label>
                </div>
                {passwordNeeded && <p>首次配置、更换账号或数据库目标时，必须输入密码。</p>}
                <label className="etf-check">
                  <input
                    type="checkbox"
                    checked={config.db_tls}
                    onChange={(e) => {
                      setConfig({ ...config, db_tls: e.target.checked })
                      setDbAck(false)
                    }}
                  />
                  数据库连接使用 TLS
                </label>
                {remote && !config.db_tls && (
                  <label className="etf-collection-warning">
                    <input
                      type="checkbox"
                      checked={dbAck}
                      onChange={(e) => setDbAck(e.target.checked)}
                    />
                    远程数据库未启用
                    TLS，凭据和数据可能被截获；我接受保存此配置后自动和手动采集均存在该风险
                  </label>
                )}
                <label className="etf-check">
                  <input
                    type="checkbox"
                    checked={config.enabled}
                    onChange={(e) => setConfig({ ...config, enabled: e.target.checked })}
                  />
                  启用自动采集
                </label>
                <p>每日采集时间（北京时间，1–6 个，不可重复）</p>
                <div className="etf-schedule">
                  {config.schedule.map((time, i) => (
                    <div key={i}>
                      <input
                        aria-label={`采集时间 ${i + 1}`}
                        type="time"
                        required
                        value={time}
                        onChange={(e) =>
                          setConfig({
                            ...config,
                            schedule: config.schedule.map((v, n) => (n === i ? e.target.value : v)),
                          })
                        }
                      />
                      <button
                        type="button"
                        aria-label={`删除采集时间 ${i + 1}`}
                        disabled={config.schedule.length <= 1}
                        onClick={() =>
                          setConfig({
                            ...config,
                            schedule: config.schedule.filter((_, n) => n !== i),
                          })
                        }
                      >
                        删除
                      </button>
                    </div>
                  ))}
                  <button
                    type="button"
                    disabled={config.schedule.length >= 6}
                    onClick={() =>
                      setConfig({ ...config, schedule: [...config.schedule, '18:00'] })
                    }
                  >
                    添加时间
                  </button>
                </div>
                {new Set(config.schedule).size !== config.schedule.length && (
                  <p role="alert">采集时间不能重复。</p>
                )}
                <div className="heading-actions">
                  <button
                    className="button"
                    type="submit"
                    disabled={
                      transportBlocked ||
                      dbBlocked ||
                      !!passwordNeeded ||
                      new Set(config.schedule).size !== config.schedule.length
                    }
                  >
                    {busy === 'save' ? '保存中…' : '保存采集配置'}
                  </button>
                  <button
                    type="button"
                    className="button button-quiet"
                    disabled={transportBlocked || dbBlocked || !!passwordNeeded}
                    onClick={() => void submit('test')}
                  >
                    {busy === 'test' ? '测试中…' : '测试连接（只读）'}
                  </button>
                </div>
              </fieldset>
              <button
                className="button button-quiet"
                type="button"
                disabled={!!busy}
                onClick={() => {
                  setPassword('')
                  setConfig(null)
                  setDbAck(false)
                  setConfigVersion((n) => n + 1)
                }}
              >
                重新读取配置（丢弃未保存修改）
              </button>
              {running && <p>采集运行期间不能修改配置，请完成后再保存。</p>}
            </form>
          )}
        </details>
      )}
      {error && (
        <p role="alert" className="etf-collection-warning">
          {error}
        </p>
      )}
      {message && <p role="status">{message}</p>}
      {!!status?.gap_count && (
        <p>
          仍有 {status.gap_count}{' '}
          项数据缺口，未耗尽重试的部分会继续补齐；已耗尽部分可勾选后手动重试。未核验历史数据不在重试范围内，历史目标不代表已经完成。
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
