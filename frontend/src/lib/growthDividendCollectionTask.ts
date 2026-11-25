import { getGrowthDividendStatus, updateGrowthDividend } from '../api/growthDividend'
import { createCollectionTask } from './collectionTask'

export const growthCollectionErrorText = (error: unknown) =>
  error instanceof Error && error.message !== 'Failed to fetch'
    ? error.message
    : '暂时无法连接数据服务，请稍后重试。'

export const growthDividendCollectionTask = createCollectionTask({
  readStatus: getGrowthDividendStatus,
  start: () => updateGrowthDividend('refresh'),
  cancel: () => updateGrowthDividend('cancel'),
  activity: (status) => ({
    running: status.running,
    databaseSyncing: status.database_syncing ?? false,
    attemptAt: status.last_attempt_at,
    cancelling: status.cancelling,
  }),
  needsHistoryReload: (previous, next) =>
    (previous.running && !next.running) ||
    (!!previous.database_syncing && !next.database_syncing) ||
    previous.last_success_at !== next.last_success_at ||
    (!next.running && previous.last_attempt_at !== next.last_attempt_at),
  errorText: growthCollectionErrorText,
  noStartMessage: '',
  manualReconcile: true,
  activePollMs: 2000,
  idlePollMs: 30000,
})
