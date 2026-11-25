import { getValuationStatus, updateValuation } from '../api/valuationRuler'
import { createCollectionTask } from './collectionTask'

export const valuationErrorText = (reason: unknown) =>
  reason instanceof Error ? reason.message : '暂时无法连接数据服务'

export const valuationCollectionTask = createCollectionTask({
  readStatus: getValuationStatus,
  start: () => updateValuation('refresh'),
  activity: (status) => ({
    running: status.status === 'running',
    databaseSyncing: status.database_syncing ?? false,
    runId: status.run_id,
  }),
  needsHistoryReload: (previous, next) =>
    previous.last_success_at !== next.last_success_at ||
    previous.completed_days !== next.completed_days ||
    (previous.status === 'running' && next.status !== 'running') ||
    (!!previous.database_syncing && !next.database_syncing),
  errorText: valuationErrorText,
  noStartMessage: '本次未启动采集，请核对采集状态。',
  activePollMs: 2000,
  idlePollMs: 15000,
})
