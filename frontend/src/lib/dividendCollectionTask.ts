import { DividendRequestError, getDividendStatus, updateDividend } from '../api/dividend'
import { createCollectionTask } from './collectionTask'

export const dividendCollectionTask = createCollectionTask({
  readStatus: getDividendStatus,
  start: () => updateDividend('refresh'),
  activity: (status) => ({
    running: status.running,
    databaseSyncing: false,
    attemptAt: status.last_attempt_at,
    runId: status.run_id,
  }),
  startAllowed: (status) => status.available,
  definiteRejection: (error) =>
    error instanceof DividendRequestError && error.status >= 400 && error.status < 500,
  confirmByNewRunId: true,
  refreshAfterCommand: false,
  reloadOnConfirmation: true,
  needsHistoryReload: (old, next) =>
    (old.running && !next.running) || old.last_success_at !== next.last_success_at,
  errorText: (reason) => (reason instanceof Error ? reason.message : '操作失败'),
  noStartMessage: '',
  noStartNotice: '其他业绩选股任务正在运行，请等待完成后再更新。',
  activePollMs: 2000,
  idlePollMs: 2000,
})
