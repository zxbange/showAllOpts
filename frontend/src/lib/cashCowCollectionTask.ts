import { getCashCowLatest, updateCashCow } from '../api/cashCow'
import { createCollectionTask } from './collectionTask'

export const cashCowCollectionTask = createCollectionTask({
  // Pages supply their period-specific reader. All three periods share
  // the same mutation registry and the same ten-year screening worker.
  readStatus: async (signal) => (await getCashCowLatest(signal)).status,
  start: () => updateCashCow('refresh'),
  cancel: () => updateCashCow('cancel'),
  activity: (status) => ({
    running: status.running,
    databaseSyncing: false,
    runId: status.run_id,
    attemptAt: status.last_attempt_at,
    cancelling: status.cancelling,
  }),
  needsHistoryReload: () => false,
  errorText: (e) => (e instanceof Error ? e.message : '暂时无法连接数据服务。'),
  noStartMessage: '',
  confirmByNewRunId: true,
  manualReconcile: true,
  activePollMs: 2000,
  idlePollMs: null,
})
