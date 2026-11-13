import { useCallback, useEffect, useRef, useState } from 'react'
import { Database, RefreshCw } from 'lucide-react'
import { useAuth } from './AccountAccess'
import { usePolling } from '../hooks/usePolling'
import {
  MarketCollectionRequestError,
  getMarketCollectionStatus,
  runMarketCollection,
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
import type { MarketCollectionStatus } from '../types/marketCollection'
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

  const running = marketCollectionActive(status) || acceptedActive
  const workerDisabled = !!status?.worker_disabled || status?.status === 'worker_disabled'
  const retry =
    ['paused', 'partial', 'blocked', 'failed'].includes(status?.status ?? '') || !!status?.pending

  function pollNow() {
    void refreshStatus().catch(() => {
      if (mounted.current) setStatusError('暂时无法读取采集状态，已有图表仍可查看。')
    })
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
          <p>统一更新市场热度与抱团拥挤度。</p>
        </div>
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
          {status && !status.configured && <p>请管理员在系统配置中设置数据库。</p>}
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
