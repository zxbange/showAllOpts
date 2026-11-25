import { useEffect, useRef } from 'react'
import { PageHeaderActions } from './PageHeaderActions'
import { RefreshCw } from 'lucide-react'
import { useAuth } from './AccountAccess'
import { useCollectionTask } from '../hooks/useCollectionTask'
import { dividendCollectionTask } from '../lib/dividendCollectionTask'

const stageLabels: Record<string, string> = {
  collecting: '准备采集',
  universe: '核验股票池',
  quotes: '采集收盘行情与停牌状态',
  valuations: '采集并核验估值',
  dividends: '采集分红方案',
  ranking: '筛选银行与非银行候选',
  history: '核验历史股息率',
  validating: '校验完整结果',
  publishing: '写入共享数据库',
  syncing_local: '同步并校验本地缓存',
  complete: '更新完成',
  failed: '采集失败',
  publication_pending: '等待恢复同步',
}
const stageLabel = (stage?: string | null) => (stage ? stageLabels[stage] || stage : '准备采集')

export function DividendCollectionControls({ onComplete }: { onComplete: () => void }) {
  const { session } = useAuth()
  const {
    status,
    actionError: error,
    statusError: pollError,
    notice,
    pending: busy,
    accepted,
    uncertain,
    historyRevision,
    act,
  } = useCollectionTask(dividendCollectionTask, session, session?.user?.role === 'admin')
  const seenRevision = useRef(historyRevision)
  useEffect(() => {
    if (seenRevision.current !== historyRevision) {
      seenRevision.current = historyRevision
      onComplete()
    }
  }, [historyRevision, onComplete])
  const awaiting = accepted || uncertain
  const running = !!status?.running || awaiting
  const stageStarted = Date.parse(status?.stage_started_at || '')
  const stageSeconds = Number.isFinite(stageStarted)
    ? Math.max(0, Math.floor((Date.now() - stageStarted) / 1000))
    : null

  return (
    <section className="panel dividend-collection" aria-label="高股息数据采集">
      <div className="panel-heading">
        <div>
          <h2>数据采集</h2>
          <p>一次更新银行与非银行选股结果</p>
        </div>
        <PageHeaderActions>
          <button
            className="button button-primary"
            disabled={busy || !status || running || !!pollError || !status.available}
            onClick={() => void act()}
          >
            <RefreshCw size={16} className={busy || running ? 'spin' : ''} />
            {running ? '采集中…' : busy ? '提交中…' : '更新数据'}
          </button>
        </PageHeaderActions>
      </div>
      {status && (
        <div className="growth-dividend-progress" aria-live="polite">
          <div className="growth-dividend-progress-label">
            <span>
              {awaiting && !status.running
                ? '等待确认本次采集状态'
                : status.interrupted
                  ? '上次采集中断'
                  : status.stage
                    ? stageLabel(status.stage)
                    : '等待更新'}
            </span>
            {status.stage === 'history' && status.total != null && status.processed != null && (
              <span>
                已处理 {status.processed} / {status.total} 只（含历史数据不可用）
              </span>
            )}
          </div>
          {running && <progress aria-label="高股息采集更新进度" />}
          {running && (
            <p>
              按实际阶段更新，耗时取决于数据源和数据库响应。
              {stageSeconds != null && ` 当前阶段已用时 ${stageSeconds} 秒。`}
            </p>
          )}
          {!running && status.failed_stage && <p>停止阶段：{stageLabel(status.failed_stage)}</p>}
          {status.last_success_at && (
            <p>
              上次成功更新：
              {new Date(status.last_success_at).toLocaleString('zh-CN', {
                timeZone: 'Asia/Shanghai',
                hour12: false,
              })}
            </p>
          )}
        </div>
      )}
      {(error || pollError || status?.error) && (
        <p role="alert">{error || pollError || status?.error}</p>
      )}
      {notice && !running && <p role="status">{notice}</p>}
      {status?.publication_pending && !running && (
        <p role="status">已有待发布结果，更新数据将优先恢复数据库及本地同步。</p>
      )}
      {uncertain && awaiting && (
        <p role="status">提交结果待确认，正在读取采集状态，请勿重复启动。</p>
      )}
      {status && !status.available && (
        <p role="status">{status.unavailable_reason || '采集暂不可用'}</p>
      )}
      {status?.interrupted && !status.error && <p role="status">上次采集中断，可以重新更新。</p>}
    </section>
  )
}
