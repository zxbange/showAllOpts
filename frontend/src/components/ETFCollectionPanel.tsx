import { PageActionButton } from './PageActionButton'
import { dataUpdateInstant, formatDataUpdatedAt } from '../lib/dataUpdatedAt'
import { CollectionHistoryFrame, CollectionHistoryError } from './CollectionHistory'
import { PageHeaderActions } from './PageHeaderActions'
import { useAuth } from './AccountAccess'
import { useQueuedCollectionTask } from '../hooks/useQueuedCollectionTask'
import {
  cancelETFQueuedTask,
  startETFQueuedTask,
  etfQueuedActive,
  etfQueuedDisplay,
  etfQueuedTask,
} from '../lib/etfQueuedTask'
import type { ETFCollectionRun } from '../types/etf'
import { collectionErrorMessage } from '../lib/etf'

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
  abandoned: '已停止补采',
}
export function ETFCollectionPanel({ onCollected }: { onCollected: () => void }) {
  const { session } = useAuth()
  const admin = session?.user?.role === 'admin'
  const { task, state } = useQueuedCollectionTask(etfQueuedTask, session, onCollected)
  const { error, historyUnavailable, busy, uncertain, retryExhausted } = state
  const status = etfQueuedDisplay(state)
  const running = etfQueuedActive(status)
  const manualActive = ['queued', 'running', 'cancelling'].includes(status?.manual?.status ?? '')
  const cancelling = status?.manual?.status === 'cancelling'
  const run = () => startETFQueuedTask(task, admin)
  const cancel = () => cancelETFQueuedTask(task, admin)
  const progress = status?.progress
  const publication = status?.cache_publication
  const recent: ETFCollectionRun[] =
    status?.recent_runs ??
    status?.runs?.map((run, i) => ({
      ...run,
      id: `legacy-${i}`,
      trigger: 'scheduled' as const,
      requested_at: run.slot,
      finished_at: null,
      attempt: null,
      time_basis: 'legacy_request_time' as const,
    })) ??
    []
  if (!admin && !progress && !status?.cache_pending && !recent.length) return null
  return (
    <section className="panel etf-collection-panel" aria-label="ETF采集管理">
      <div className="etf-collection-overview">
        <div role="status">
          {status?.cache_pending && (
            <>
              <p>等待图表缓存更新…</p>
              {publication?.status === 'waiting_lock' && <p>其他同步占用缓存，稍后重试。</p>}
              {publication?.status === 'publishing' && <p>正在核验并发布缓存。</p>}
              {publication?.status === 'failed' && (
                <p>
                  缓存更新失败，保留旧图表，等待重试
                  {publication.error_code ? `（${publication.error_code}）` : ''}。
                </p>
              )}
              {(publication?.required_through ||
                publication?.latest_share_date ||
                publication?.last_attempt_at) && (
                <p>
                  {[
                    publication.required_through && `待核验发布至 ${publication.required_through}`,
                    publication.latest_share_date &&
                      `上次核验缓存份额至 ${publication.latest_share_date}`,
                    publication.last_attempt_at &&
                      `最近尝试 ${formatDataUpdatedAt(publication.last_attempt_at)}`,
                  ]
                    .filter(Boolean)
                    .join(' · ')}
                </p>
              )}
            </>
          )}
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
                  onChange={(e) => task.patch({ retryExhausted: e.target.checked })}
                />
                重新尝试当前目标日已耗尽重试的数据（{status.retry_exhausted_count}项）
              </label>
            )}
            {uncertain && <p>重试会沿用上次请求，不会重复排队。</p>}
            {cancelling && <p>正在等待当前采集步骤结束，取消后可重新开始。</p>}
            <PageHeaderActions>
              <PageActionButton
                disabled={
                  !!busy || !state.status || (!manualActive && (running || !status?.configured))
                }
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
                        : '更新数据'}
              </PageActionButton>
            </PageHeaderActions>
          </div>
        )}
      </div>
      {error && (
        <p role="alert" className="etf-collection-warning">
          {error}
        </p>
      )}
      {!!status?.gap_count && <p>当前交易日缺失 {status.gap_count} 项，可重试。</p>}
      {status?.manual_gap_start && <p>手动补采仍有历史缺口，最早为 {status.manual_gap_start}。</p>}
      <CollectionHistoryFrame
        hasRecords={recent.length > 0}
        loading={!status && !historyUnavailable}
        error={historyUnavailable ? '暂时无法读取最新采集记录' : ''}
      >
        {recent.slice(0, 5).map((run) => (
          <li key={run.id} data-outcome={run.status === 'complete' ? 'success' : run.status}>
            <div className="collection-history-row-text">
              {dataUpdateInstant(run.finished_at || run.requested_at) != null
                ? formatDataUpdatedAt(run.finished_at || run.requested_at)
                : '记录时间未确认'}{' '}
              · {run.trigger === 'manual' ? '手动采集' : '自动采集'}
              {run.attempt && run.attempt > 1 ? `（第${run.attempt}次尝试）` : ''}
              {' · '}
              {labels[run.status] ?? '状态待确认'}
              {run.time_basis === 'legacy_request_time' ? ' · 旧记录，显示请求时间' : ''}
              {run.time_basis === 'recovered_at' ? ' · 显示中断确认时间' : ''}
              {run.error ? ` · ${collectionErrorMessage(run.error)}` : ''}
            </div>
            <CollectionHistoryError
              details={run.error_details}
              failed={run.status === 'failed' || run.status === 'partial'}
            />
          </li>
        ))}
      </CollectionHistoryFrame>
    </section>
  )
}
