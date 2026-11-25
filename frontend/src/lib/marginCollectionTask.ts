import { getMarginStatus, updateMargin } from '../api/client'
import { createCollectionTask } from './collectionTask'

export const marginCollectionErrorText = (error: unknown) =>
  error instanceof Error && error.message !== 'Failed to fetch'
    ? error.message
    : '暂时无法连接数据服务，请稍后重试。'

export const marginCollectionTask = createCollectionTask({
  readStatus: getMarginStatus,
  start: () => updateMargin('refresh'),
  cancel: () => updateMargin('cancel'),
  activity: (status) => ({
    running: status.running,
    databaseSyncing: false,
    attemptAt: status.last_attempt_at,
    cancelling: status.cancelling,
  }),
  needsHistoryReload: (previous, next) =>
    (previous.running && !next.running) || previous.last_success_at !== next.last_success_at,
  errorText: marginCollectionErrorText,
  noStartMessage: '',
  manualReconcile: true,
  activePollMs: 2000,
  idlePollMs: 30000,
})
