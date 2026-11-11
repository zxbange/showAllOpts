import { useCallback, useEffect, useRef, useState } from 'react'
import { Clock3, Database, RefreshCw } from 'lucide-react'
import { useAuth } from './AccountAccess'
import { usePolling } from '../hooks/usePolling'
import {
  MarketCollectionRequestError,
  getMarketCollectionConfiguration,
  getMarketCollectionStatus,
  runMarketCollection,
  saveMarketCollectionConfiguration,
  testMarketCollectionConfiguration,
} from '../api/marketCollection'
import {
  marketCollectionActive,
  marketCollectionErrorMessage,
  marketCollectionFindRun,
  marketCollectionRunFailure,
  marketCollectionRunMessage,
  marketCollectionLabels,
  marketCollectionNeedsReload,
  marketResultLabel,
} from '../lib/marketCollection'
import type {
  MarketCollectionConfiguration,
  MarketCollectionInput,
  MarketCollectionStatus,
} from '../types/marketCollection'
import '../styles/market-collection.css'

const count = (value: number | null | undefined) =>
  value == null ? '—' : value.toLocaleString('zh-CN')
const timestamp = (value: string | null | undefined) => {
  if (!value) return '尚无记录'
  const date = new Date(value)
  return Number.isNaN(date.getTime())
    ? '尚无记录'
    : date.toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai', hour12: false })
}
const phases: Record<string, string> = {
  calendar: '确认交易日',
  universe: '读取目标日股票名单',
  snapshot: '读取当日成交数据',
  verifying_snapshot: '核对当日快照',
  fetching: '逐股核对行情与均线',
  ma20: '逐股核对行情与均线',
  validating: '校验当日数据',
  writing: '保存采集结果',
  writing_crowding: '保存拥挤度结果',
  cache: '更新图表数据',
  publishing: '更新图表数据',
}

export function MarketCollectionPanel({ onCollected }: { onCollected: () => void }) {
  const admin = useAuth().session?.user?.role === 'admin'
  const [status, setStatus] = useState<MarketCollectionStatus | null>(null)
  const [config, setConfig] = useState<MarketCollectionConfiguration | null>(null)
  const [saved, setSaved] = useState<MarketCollectionConfiguration | null>(null)
  const [password, setPassword] = useState('')
  const [configVersion, setConfigVersion] = useState(0)
  const [configError, setConfigError] = useState('')
  const [statusError, setStatusError] = useState('')
  const [actionError, setActionError] = useState('')
  const [message, setMessage] = useState('')
  const [busy, setBusy] = useState('')
  const [uncertain, setUncertain] = useState(false)
  const [confirming, setConfirming] = useState(false)
  const [acceptedActive, setAcceptedActive] = useState(false)
  const mounted = useRef(false)
  const lock = useRef(false)
  const mutationVersion = useRef(0)
  const operation = useRef<AbortController | null>(null)
  const actionKind = useRef<'save' | 'test' | 'run' | null>(null)
  const requestId = useRef<string | null>(null)
  const awaitingRun = useRef<string | null>(null)
  const busyConflict = useRef(false)
  const previous = useRef<MarketCollectionStatus | null>(null)
  const onPublished = useRef(onCollected)
  onPublished.current = onCollected

  useEffect(() => {
    mounted.current = true
    return () => {
      mounted.current = false
      operation.current?.abort()
    }
  }, [])

  const loadStatus = useCallback(async (signal: AbortSignal) => {
    const version = mutationVersion.current
    const value = await getMarketCollectionStatus(signal)
    return { value, version }
  }, [])
  const { refresh: refreshStatus } = usePolling(
    loadStatus,
    ({ value: next, version }) => {
      if (!mounted.current || lock.current || version !== mutationVersion.current) return
      const matched = requestId.current && marketCollectionFindRun(next, requestId.current)
      if (matched) {
        requestId.current = null
        setUncertain(false)
        if (actionKind.current === 'run') {
          setActionError('')
          setMessage(marketCollectionRunMessage(matched))
        }
      }
      // A successful POST must not reopen Start while its status read is still stale.
      if (
        !awaitingRun.current ||
        marketCollectionFindRun(next, awaitingRun.current) ||
        marketCollectionActive(next)
      ) {
        awaitingRun.current = null
        setAcceptedActive(false)
        setConfirming(false)
        if (busyConflict.current) {
          busyConflict.current = marketCollectionActive(next)
          setMessage(
            marketCollectionActive(next)
              ? '已有采集正在运行，正在查看当前采集进度。'
              : requestId.current
                ? '上次请求的结果仍待确认，可重试同一请求。'
                : '采集状态已更新，可按需重新发起采集。',
          )
        }
      }
      if (marketCollectionNeedsReload(previous.current, next)) onPublished.current()
      previous.current = next
      setStatus(next)
      setStatusError('')
    },
    () => setStatusError('暂时无法读取采集状态，已有图表仍可查看。'),
    marketCollectionActive(status) || status?.cache_pending || confirming || uncertain
      ? 1500
      : 30000,
  )

  useEffect(() => {
    if (!admin) {
      setConfig(null)
      setSaved(null)
      setPassword('')
      return
    }
    const controller = new AbortController()
    setConfigError('')
    getMarketCollectionConfiguration(controller.signal)
      .then((value) => {
        if (!controller.signal.aborted) {
          setConfig(value)
          setSaved(value)
        }
      })
      .catch(() => {
        if (!controller.signal.aborted) setConfigError('无法读取采集配置，请重试。')
      })
    return () => controller.abort()
  }, [admin, configVersion])

  const running = marketCollectionActive(status) || acceptedActive
  const workerDisabled = !!status?.worker_disabled || status?.status === 'worker_disabled'
  const passwordNeeded =
    !!config &&
    (!saved?.password_set || saved.target_changed || config.username !== saved.username) &&
    !password
  const configurationBlocked =
    !config || !config.transport_secure || !config.username.trim() || passwordNeeded
  const retry =
    ['paused', 'partial', 'blocked', 'failed'].includes(status?.status ?? '') || !!status?.pending

  function pollNow() {
    void refreshStatus().catch(() => {
      if (mounted.current) setStatusError('暂时无法读取采集状态，已有图表仍可查看。')
    })
  }

  async function submit(kind: 'save' | 'test') {
    if (!admin || !config || running || confirming || lock.current || configurationBlocked) return
    lock.current = true
    actionKind.current = kind
    mutationVersion.current++
    const controller = new AbortController()
    operation.current = controller
    setBusy(kind)
    setActionError('')
    setMessage('')
    const body: MarketCollectionInput = {
      revision: config.revision,
      username: config.username.trim(),
      enabled: config.enabled,
      ...(password ? { password } : {}),
    }
    try {
      if (kind === 'save') {
        const next = await saveMarketCollectionConfiguration(body, controller.signal)
        if (!mounted.current || controller.signal.aborted) return
        setConfig(next)
        setSaved(next)
        setPassword('')
        setMessage('日采集配置已保存，市场热度与抱团拥挤度共用此账户。')
      } else {
        const result = await testMarketCollectionConfiguration(body, controller.signal)
        if (!mounted.current || controller.signal.aborted) return
        setMessage(
          `${result.message}${result.permissions_verified ? '' : ' 本次仅读取连接与表，实际写入结果以采集任务为准。'}`,
        )
      }
    } catch (reason) {
      if (mounted.current && !controller.signal.aborted)
        setActionError(marketCollectionErrorMessage(reason instanceof Error ? reason.message : ''))
    } finally {
      lock.current = false
      if (operation.current === controller) operation.current = null
      if (mounted.current) {
        setBusy('')
        pollNow()
      }
    }
  }

  async function run() {
    if (!admin || lock.current || running || confirming || workerDisabled || !status?.configured)
      return
    lock.current = true
    actionKind.current = 'run'
    mutationVersion.current++
    const controller = new AbortController()
    operation.current = controller
    busyConflict.current = false
    setBusy('run')
    setActionError('')
    setMessage('')
    const hadUncertainRequest = requestId.current !== null
    requestId.current ??= crypto.randomUUID()
    try {
      const accepted = await runMarketCollection(requestId.current, controller.signal)
      if (!mounted.current || controller.signal.aborted) return
      requestId.current = null
      setUncertain(false)
      const active = ['queued', 'running'].includes(accepted.status)
      awaitingRun.current = active ? accepted.id : null
      setAcceptedActive(active)
      setConfirming(active)
      setMessage(marketCollectionRunMessage(accepted))
    } catch (reason) {
      if (mounted.current && !controller.signal.aborted) {
        const failure = marketCollectionRunFailure(reason)
        // A rejection of this retry does not resolve an earlier lost response.
        const keepRequest = hadUncertainRequest || failure === 'uncertain'
        if (!keepRequest) requestId.current = null
        setUncertain(keepRequest)
        if (failure === 'busy') {
          awaitingRun.current =
            reason instanceof MarketCollectionRequestError ? (reason.activeRun?.id ?? null) : null
          setAcceptedActive(reason instanceof MarketCollectionRequestError && !!reason.activeRun)
          busyConflict.current = true
          setConfirming(true)
          setMessage('采集服务已有任务占用，正在确认当前进度；请等待当前采集结束。')
        } else {
          setActionError(
            failure === 'uncertain'
              ? '暂时无法确认采集请求的结果，正在核对状态。重试将沿用同一请求，不重复启动任务。'
              : marketCollectionErrorMessage(reason instanceof Error ? reason.message : ''),
          )
        }
      }
    } finally {
      lock.current = false
      if (operation.current === controller) operation.current = null
      if (mounted.current) {
        setBusy('')
        pollNow()
      }
    }
  }

  const total = Math.max(0, status?.progress?.total ?? status?.total ?? 0)
  const completed = Math.max(0, status?.progress?.completed ?? status?.completed ?? 0)
  const showProgress = running || (status?.pending ?? 0) > 0
  return (
    <section className="panel market-collection-panel" aria-label="市场日采集">
      <div className="market-collection-heading">
        <div>
          <h2>
            <Database size={17} /> 市场日采集
          </h2>
          <p>
            日采集使用东方财富目标日报表及公开行情，并核对官方分类数量；以聚宽原脚本为目标口径，热度与拥挤度分别核验发布，证据不足的缺口保留。
          </p>
        </div>
        <span className="market-collection-schedule">
          <Clock3 size={15} /> 交易日 18:30 启动 · 北京时间
        </span>
      </div>
      <div className="market-collection-overview">
        <div role="status">
          <strong>
            {status
              ? running
                ? '采集中'
                : (marketCollectionLabels[status.status] ?? '正在确认任务状态')
              : '正在读取采集状态…'}
          </strong>
          {status && (
            <span>
              {' '}
              ·{' '}
              {workerDisabled
                ? '自动计划待服务启用'
                : status.enabled
                  ? '自动采集已启用'
                  : '自动采集已关闭'}
            </span>
          )}
          {status?.trade_date && (
            <p>
              本轮交易日 {status.trade_date} · 状态更新 {timestamp(status.updated_at)}
            </p>
          )}
          {status && !status.configured && (
            <p>请由管理员完成一次写入账户配置，之后按计划自动采集。</p>
          )}
        </div>
        {admin && (
          <button
            className="button"
            disabled={!!busy || running || confirming || workerDisabled || !status?.configured}
            onClick={() => void run()}
          >
            <RefreshCw size={15} className={busy === 'run' || running ? 'spin' : ''} />
            {busy === 'run'
              ? '提交中…'
              : running
                ? '采集中…'
                : confirming
                  ? '正在确认采集状态…'
                  : uncertain
                    ? '重试同一采集请求'
                    : retry
                      ? '重试未完成采集'
                      : '立即采集'}
          </button>
        )}
      </div>
      <p className="market-collection-note">
        自动与手动采集从两张表各自最后数据日的下一个交易日起补齐，并继续处理已登记的失败缺口；未登记的更早历史缺口不自动补采。18:30前手动触发仅处理此前已结束的交易日。
      </p>
      <p className="market-collection-note">
        首轮先完整处理全部待采集数据，失败项先跳过；首轮结束后，仅对失败项最多再试3轮。仍失败的缺口保留到下次采集继续处理，成功记录不重复发布。
      </p>
      {status?.source_latest && (
        <p className="market-collection-note">
          本轮源库检查：热度截至 {status.source_latest.heat ?? '暂无数据'} · 拥挤度截至{' '}
          {status.source_latest.crowding ?? '暂无数据'}
        </p>
      )}
      {!!status?.gaps?.length && (
        <details className="market-collection-settings">
          <summary>待补齐交易日（最近 {status.gaps.length} 条）</summary>
          {status.gaps.map((gap) => (
            <p className="market-collection-note" key={gap.trade_date}>
              {gap.trade_date} · 热度：{marketResultLabel(gap.heat_status)} · 拥挤度：
              {marketResultLabel(gap.crowding_status)}
              {gap.error && <> · {marketCollectionErrorMessage(gap.error)}</>}
            </p>
          ))}
        </details>
      )}
      {workerDisabled && (
        <p className="market-collection-notice" role="status">
          服务端采集任务未启用。账户配置可以保留，启用服务后才能自动或手动采集。
        </p>
      )}
      {showProgress && (
        <div className="market-collection-progress" role="status">
          <p>
            {phases[status?.progress?.phase ?? ''] ?? '逐股核对当日数据'} · 已处理{' '}
            {count(completed)} / {count(total)} 只 · 待完成 {count(status?.pending)} 只
          </p>
          <progress
            aria-label="市场日采集进度"
            max={total || 1}
            value={Math.min(completed, total || 1)}
          />
          <small>
            首轮失败项先跳过，完整采集一遍后仅重试失败项（最多3轮）；数据完整并通过校验后才发布当日比例。
          </small>
        </div>
      )}
      {status && (
        <div className="market-collection-results">
          <span>市场热度 · {marketResultLabel(status.heat_status)}</span>
          <span>抱团拥挤度 · {marketResultLabel(status.crowding_status)}</span>
          {status.total > 0 && <span>股票总数 {count(status.total)} 只</span>}
        </div>
      )}
      {status?.cache_pending && (
        <p className="market-collection-note" role="status">
          结果已入库，等待图表数据更新…
        </p>
      )}
      {status?.error && (
        <p className="market-collection-notice" role="status">
          {marketCollectionErrorMessage(status.error)} 已有成功记录继续保留。
        </p>
      )}
      {statusError && (
        <div className="market-collection-notice" role="status">
          {statusError}{' '}
          <button className="button button-quiet" onClick={pollNow}>
            重新读取状态
          </button>
        </div>
      )}
      {admin && (
        <details className="market-collection-settings">
          <summary>日采集配置</summary>
          {configError && (
            <p className="market-collection-notice" role="alert">
              {configError}
            </p>
          )}
          {!config ? (
            <button
              className="button button-quiet"
              onClick={() => setConfigVersion((value) => value + 1)}
            >
              重新读取配置
            </button>
          ) : (
            <form
              onSubmit={(event) => {
                event.preventDefault()
                void submit('save')
              }}
            >
              <p className="market-collection-target">
                固定数据库目标{' '}
                <strong>
                  {config.host}:{config.port} / {config.database}
                </strong>
              </p>
              {saved?.target_changed && (
                <p className="market-collection-notice" role="alert">
                  数据库目标已变更，请输入当前目标的写入密码后保存。
                </p>
              )}
              {saved?.source === 'etf' && (
                <p className="market-collection-note">
                  已使用现有 ETF 采集写入账户；如需更换，保存一次即可同时用于这两个市场指标。
                </p>
              )}
              {!config.transport_secure && (
                <p className="market-collection-notice" role="alert">
                  请通过 HTTPS 访问后保存或测试数据库凭据。
                </p>
              )}
              <fieldset disabled={!!busy || running || confirming || !config.transport_secure}>
                <div className="market-collection-fields">
                  <label>
                    采集写入账号
                    <input
                      required
                      autoComplete="off"
                      value={config.username}
                      onChange={(event) => {
                        setConfig({ ...config, username: event.target.value })
                        setPassword('')
                        setMessage('')
                      }}
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
                        saved.username === config.username
                          ? '留空保留已保存密码（账号不变时）'
                          : '请输入密码'
                      }
                      onChange={(event) => setPassword(event.target.value)}
                    />
                  </label>
                </div>
                {passwordNeeded && (
                  <p className="market-collection-note">
                    首次配置或更换写入账号时，请输入对应密码。
                  </p>
                )}
                <label className="market-collection-check">
                  <input
                    type="checkbox"
                    checked={config.enabled}
                    onChange={(event) => setConfig({ ...config, enabled: event.target.checked })}
                  />
                  启用市场自动采集
                </label>
                <p className="market-collection-note">
                  每个交易日北京时间 18:30
                  启动。每轮首采后，失败项最多再试3轮；同一采集请求或同一计划内重复检查、服务重启均不重置次数。新的手动采集请求或下一轮18:30计划会重新开始计次。
                </p>
                <p className="market-collection-note">
                  已登记的失败缺口持续保留，不因后续日期采集成功而丢弃；未登记的更早历史缺口不自动补采，已有成功记录保留。
                </p>
                <div className="market-collection-actions">
                  <button className="button" type="submit" disabled={configurationBlocked}>
                    {busy === 'save' ? '保存中…' : '保存日采集配置'}
                  </button>
                  <button
                    className="button button-quiet"
                    type="button"
                    disabled={configurationBlocked}
                    onClick={() => void submit('test')}
                  >
                    {busy === 'test' ? '测试中…' : '测试数据库连接（只读）'}
                  </button>
                  <button
                    className="button button-quiet"
                    type="button"
                    onClick={() => {
                      setPassword('')
                      setConfig(null)
                      setMessage('')
                      setConfigVersion((value) => value + 1)
                    }}
                  >
                    重置修改
                  </button>
                </div>
              </fieldset>
              {running && (
                <p className="market-collection-note">采集运行期间请等待任务结束后再修改配置。</p>
              )}
            </form>
          )}
        </details>
      )}
      {actionError && !(actionKind.current === 'run' && running) && (
        <p className="market-collection-notice" role="alert">
          {actionError}
        </p>
      )}
      {message && (
        <p className="market-collection-note" role="status">
          {message}
        </p>
      )}
    </section>
  )
}
