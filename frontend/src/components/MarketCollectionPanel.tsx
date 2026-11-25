import { PageActionButton } from './PageActionButton'
import { PageHeaderActions } from './PageHeaderActions'
import { RefreshCw } from 'lucide-react'
import { useAuth } from './AccountAccess'
import { useQueuedCollectionTask } from '../hooks/useQueuedCollectionTask'
import {
  marketQueuedTask,
  marketQueuedRunning,
  marketWorkerDisabled,
  startMarketQueuedTask,
} from '../lib/marketQueuedTask'
import { marketCollectionErrorMessage } from '../lib/marketCollection'
import '../styles/market-collection.css'

const count = (value: number | null | undefined) =>
  value == null ? '—' : value.toLocaleString('zh-CN')
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
  const { session } = useAuth()
  const admin = session?.user?.role === 'admin'
  const { task, state } = useQueuedCollectionTask(marketQueuedTask, session, onCollected)
  const { status, statusError, actionError, message, busy, uncertain, confirming } = state
  const running = marketQueuedRunning(state)
  const workerDisabled = marketWorkerDisabled(state)
  const retry =
    ['paused', 'partial', 'blocked', 'failed'].includes(status?.status ?? '') || !!status?.pending
  const run = () => startMarketQueuedTask(task, admin)
  const pollNow = () => void task.pollNow()

  const total = Math.max(0, status?.progress?.total ?? status?.total ?? 0)
  const completed = Math.max(0, status?.progress?.completed ?? status?.completed ?? 0)
  const showProgress = running || (status?.pending ?? 0) > 0
  return (
    <>
      {admin && (
        <PageHeaderActions>
          <PageActionButton
            icon={RefreshCw}
            iconSize={15}
            spin={busy === 'run' || running}
            disabled={!!busy || running || confirming || workerDisabled || !status?.configured}
            onClick={() => void run()}
          >
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
                      : '更新数据'}
          </PageActionButton>
        </PageHeaderActions>
      )}

      {showProgress && (
        <p role="status">
          {phases[status?.progress?.phase ?? ''] ?? '采集中'} · 已处理 {count(completed)} /{' '}
          {count(total)} 只
        </p>
      )}
      {status?.error && !actionError && (
        <p role="alert">{marketCollectionErrorMessage(status.error)}</p>
      )}
      {status?.cache_pending && <p role="status">结果已入库，等待图表数据更新…</p>}
      {statusError && (
        <p role="status">
          {statusError}{' '}
          <button className="button button-quiet" onClick={pollNow}>
            重新读取状态
          </button>
        </p>
      )}
      {workerDisabled && <p role="status">服务端采集任务未启用，请管理员检查系统配置。</p>}
      {status && !status.configured && <p role="status">请管理员在系统配置中设置数据库。</p>}
      {actionError && !running && <p role="alert">{actionError}</p>}
      {message && <p role="status">{message}</p>}
    </>
  )
}
