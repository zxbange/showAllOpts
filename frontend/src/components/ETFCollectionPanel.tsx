import { useEffect, useRef, useState } from 'react'
import { useAuth } from './AccountAccess'
import { getETFCollectionStatus } from '../api/client'
import {
  cancelCollection,
  getCollectionConfiguration,
  runCollection,
  saveCollectionConfiguration,
  testCollectionConfiguration,
} from '../api/etfCollection'
import type { ETFCollectionConfiguration, ETFCollectionStatus } from '../types/etf'
import { collectionErrorMessage, withManualRun } from '../lib/etf'

const dailyTime = (schedule: string[]) => (schedule.length === 1 ? schedule[0] : '09:00')
const normalizeConfig = (value: ETFCollectionConfiguration) => ({
  ...value,
  schedule_migrated: value.schedule_migrated || value.schedule.length > 1,
  schedule: [dailyTime(value.schedule)],
})

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
  const [config, setConfig] = useState<ETFCollectionConfiguration | null>(null)
  const [saved, setSaved] = useState<ETFCollectionConfiguration | null>(null)
  const [password, setPassword] = useState('')
  const [error, setError] = useState('')
  const [message, setMessage] = useState('')
  const [busy, setBusy] = useState('')
  const [uncertain, setUncertain] = useState(false)
  const [retryExhausted, setRetryExhausted] = useState(false)
  const retryChoice = useRef(false)
  const [refresh, setRefresh] = useState(0)
  const [configVersion, setConfigVersion] = useState(0)
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
          setConfig(normalizeConfig(value))
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
  const manualActive = ['queued', 'running', 'cancelling'].includes(status?.manual?.status ?? '')
  const cancelling = status?.manual?.status === 'cancelling'
  const insecure = config && !config.transport_secure
  const targetEdited =
    !!config &&
    !!saved &&
    (config.host !== saved.host || config.port !== saved.port || config.database !== saved.database)
  const transportBlocked = !!insecure
  const passwordNeeded =
    config &&
    (!saved?.password_set ||
      saved.target_changed ||
      targetEdited ||
      config.username !== saved.username) &&
    !password
  const invalidConfig =
    !config ||
    !config.host.trim() ||
    !config.database.trim() ||
    !config.username.trim() ||
    !Number.isInteger(config.port) ||
    config.port < 1 ||
    config.port > 65535 ||
    !dailyTime(config.schedule)
  function editTarget(patch: Partial<ETFCollectionConfiguration>) {
    if (!config) return
    setConfig({
      ...config,
      ...patch,
    })
    setPassword('')
    setMessage('')
  }
  async function submit(kind: 'save' | 'test') {
    if (
      !admin ||
      !config ||
      running ||
      lock.current ||
      transportBlocked ||
      passwordNeeded ||
      invalidConfig
    )
      return
    lock.current = true
    setBusy(kind)
    setError('')
    setMessage('')
    try {
      const {
        revision,
        host,
        port,
        database,
        username,
        enabled,
        lookback_days,
        batch_days,
        schedule,
      } = config
      const body = {
        revision,
        host,
        port,
        database,
        username,
        enabled,
        lookback_days,
        batch_days,
        schedule: [dailyTime(schedule)],
        ...(password ? { password } : {}),
      }
      if (kind === 'save') {
        const value = await saveCollectionConfiguration(body)
        if (!mounted.current) return
        setConfig(normalizeConfig(value))
        setSaved(value)
        setPassword('')
        setMessage(
          targetEdited
            ? '采集配置已保存。新数据库历史需由聚宽脚本补齐，页面仅更新最新已收市交易日，其他模块不受影响。'
            : '采集配置已保存。',
        )
        reload.current()
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
    mutationVersion.current++
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
    setMessage('')
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
              {(config.target_changed || targetEdited) && (
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
                <div className="etf-collection-sections">
                  <section className="etf-config-section" aria-labelledby="etf-account-heading">
                    <h3 id="etf-account-heading">数据库账户</h3>
                    <p className="etf-collection-hint">可使用现有具备权限的账号。</p>
                    <div className="etf-collection-fields etf-target-fields">
                      <label className="etf-host-field">
                        数据库主机
                        <input
                          required
                          autoComplete="off"
                          value={config.host}
                          onChange={(e) => editTarget({ host: e.target.value })}
                        />
                      </label>
                      <label>
                        端口
                        <input
                          required
                          type="number"
                          min="1"
                          max="65535"
                          value={config.port}
                          onChange={(e) => editTarget({ port: Number(e.target.value) })}
                        />
                      </label>
                      <label>
                        数据库名称
                        <input
                          required
                          autoComplete="off"
                          value={config.database}
                          onChange={(e) => editTarget({ database: e.target.value })}
                        />
                      </label>
                    </div>
                    <p className="etf-collection-hint">仅用于 ETF 采集与图表，其他模块不受影响。</p>
                    <div className="etf-collection-fields">
                      <label>
                        采集写入账号
                        <input
                          autoComplete="off"
                          required
                          value={config.username}
                          onChange={(e) => editTarget({ username: e.target.value })}
                        />
                      </label>
                      <label>
                        数据库密码
                        <input
                          type="password"
                          autoComplete="new-password"
                          value={password}
                          placeholder={
                            saved?.password_set &&
                            !saved.target_changed &&
                            !targetEdited &&
                            config.username === saved.username
                              ? '留空保留已保存密码（账号不变时）'
                              : '请输入密码'
                          }
                          onChange={(e) => setPassword(e.target.value)}
                        />
                      </label>
                    </div>
                    {passwordNeeded && <p>首次配置、更换账号或数据库目标时，必须输入密码。</p>}
                    <p className="etf-collection-hint etf-permission-note">
                      仅需 etf_share_daily、etf_price_daily、etf_trading_day 的 SELECT / INSERT /
                      UPDATE 权限；缺表时需 CREATE。
                    </p>
                  </section>
                  <section className="etf-config-section" aria-labelledby="etf-plan-heading">
                    <h3 id="etf-plan-heading">采集计划</h3>
                    <label className="etf-check">
                      <input
                        type="checkbox"
                        checked={config.enabled}
                        onChange={(e) => setConfig({ ...config, enabled: e.target.checked })}
                      />
                      启用自动采集
                    </label>
                    <div className="etf-collection-fields">
                      <label className="etf-daily-time">
                        每日采集时间（北京时间）
                        <input
                          type="time"
                          required
                          value={dailyTime(config.schedule)}
                          onChange={(e) => setConfig({ ...config, schedule: [e.target.value] })}
                        />
                      </label>
                    </div>
                    <p className="etf-collection-hint">
                      默认 09:00，开盘前采集上一已收市交易日的份额与价格。
                    </p>
                    {config.schedule_migrated && (
                      <p className="etf-collection-hint" role="status">
                        旧时段已合并为每日一次，可调整时间并保存确认。
                      </p>
                    )}
                    <p className="etf-collection-hint">
                      历史数据由聚宽平台脚本从 2024-01-01 起补齐，本页面接口不回补历史。
                    </p>
                  </section>
                </div>
                <div className="etf-collection-actions">
                  <button
                    className="button"
                    type="submit"
                    disabled={transportBlocked || !!passwordNeeded || invalidConfig}
                  >
                    {busy === 'save' ? '保存中…' : '保存采集配置'}
                  </button>
                  <button
                    type="button"
                    className="button button-quiet"
                    disabled={transportBlocked || !!passwordNeeded || invalidConfig}
                    onClick={() => void submit('test')}
                  >
                    {busy === 'test' ? '测试中…' : '测试连接（只读）'}
                  </button>
                  <button
                    className="button button-quiet"
                    type="button"
                    title="重新读取配置，丢弃未保存修改"
                    disabled={!!busy || running}
                    onClick={() => {
                      setPassword('')
                      setConfig(null)
                      setConfigVersion((n) => n + 1)
                    }}
                  >
                    重置修改
                  </button>
                </div>
              </fieldset>
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
