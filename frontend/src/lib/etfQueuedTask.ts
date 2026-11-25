import { getETFCollectionStatus } from '../api/client'
import { cancelCollection, runCollection } from '../api/etfCollection'
import type { ETFCollectionStatus, ETFManualRun } from '../types/etf'
import { collectionErrorMessage, withManualRun } from './etf'
import { createQueuedCollectionTask, type QueuedCollectionSnapshot } from './queuedCollectionTask'

const manualRunning = (manual?: ETFManualRun | null) =>
  ['queued', 'running', 'cancelling'].includes(manual?.status ?? '')

export const etfQueuedActive = (status?: ETFCollectionStatus | null) =>
  manualRunning(status?.manual) || status?.status === 'running' || !!status?.progress

interface ETFTaskState extends QueuedCollectionSnapshot<ETFCollectionStatus> {
  error: string
  historyUnavailable: boolean
  uncertain: boolean
  retryExhausted: boolean
  retryChoice: boolean
  requestId: string | null
  receipt: ETFManualRun | null
}

export function etfQueuedDisplay(state: ETFTaskState) {
  return state.receipt
    ? withManualRun(state.status ?? { status: 'configured' }, state.receipt)
    : state.status
}

function containsRun(status: ETFCollectionStatus, id: string) {
  return (
    status.manual?.id === id ||
    status.latest_result?.id === id ||
    !!status.recent_runs?.some((run) => run.id === id)
  )
}

function receiptConfirmed(receipt: ETFManualRun, next: ETFCollectionStatus) {
  const historical = [next.latest_result, ...(next.recent_runs ?? [])].find(
    (run) => run?.id === receipt.id && !['queued', 'running', 'cancelling'].includes(run.status),
  )
  if (historical) return true
  if (next.manual?.id !== receipt.id) {
    // A terminal mutation response is authoritative even when it drops out of
    // the latest status. An active receipt must wait to see its own identity.
    return !manualRunning(receipt)
  }
  if (!manualRunning(receipt) && manualRunning(next.manual)) return false
  if (receipt.status === 'cancelling' && ['queued', 'running'].includes(next.manual.status))
    return false
  return true
}

export const etfQueuedTask = createQueuedCollectionTask<ETFCollectionStatus, ETFTaskState>({
  initial: () => ({
    status: null,
    busy: '',
    historyRevision: 0,
    error: '',
    historyUnavailable: false,
    uncertain: false,
    retryExhausted: false,
    retryChoice: false,
    requestId: null,
    receipt: null,
  }),
  readStatus: getETFCollectionStatus,
  observe(state, next) {
    let value = { ...state, status: next, historyUnavailable: false }
    if (state.requestId && containsRun(next, state.requestId)) {
      value = { ...value, requestId: null, uncertain: false, retryExhausted: false, error: '' }
    }
    if (state.receipt && receiptConfirmed(state.receipt, next)) value.receipt = null
    const old = state.status
    if (
      old &&
      !next.cache_pending &&
      (old.cache_pending ||
        (etfQueuedActive(old) && !etfQueuedActive(next)) ||
        (!!next.manual &&
          (old.manual?.id !== next.manual.id || old.manual?.status !== next.manual.status) &&
          !etfQueuedActive(next)) ||
        (!!next.runs?.[0] &&
          (old.runs?.[0]?.slot !== next.runs[0].slot ||
            old.runs?.[0]?.status !== next.runs[0].status) &&
          !etfQueuedActive(next)))
    )
      value.historyRevision++
    return value
  },
  readFailed: (state) => ({ ...state, historyUnavailable: true }),
  pollMs: (state) =>
    etfQueuedActive(etfQueuedDisplay(state)) || state.status?.cache_pending ? 2000 : 15000,
})

function verifyReceipt(result: ETFManualRun, requestId: string) {
  if (!result || result.id !== requestId || typeof result.status !== 'string' || !result.status)
    throw new Error('暂时无法确认采集请求的结果。')
  return result
}

export async function startETFQueuedTask(task: ReturnType<typeof etfQueuedTask>, admin: boolean) {
  await task.execute(
    'run',
    (state) => admin && !etfQueuedActive(etfQueuedDisplay(state)) && !!state.status?.configured,
    async (state, patch) => {
      const requestId =
        state.requestId ??
        Array.from(crypto.getRandomValues(new Uint8Array(16)), (byte) =>
          byte.toString(16).padStart(2, '0'),
        ).join('')
      const retryChoice = state.requestId
        ? state.retryChoice
        : state.retryExhausted && !!state.status?.retry_exhausted_count
      patch({ requestId, retryChoice, error: '' })
      try {
        const result = verifyReceipt(await runCollection(requestId, retryChoice), requestId)
        patch({ requestId: null, uncertain: false, retryExhausted: false, receipt: result })
      } catch (error) {
        patch({
          uncertain: true,
          error: `${error instanceof Error ? collectionErrorMessage(error.message) : '请求失败'} 可重试同一请求，避免重复采集。`,
        })
      }
    },
  )
}

export async function cancelETFQueuedTask(task: ReturnType<typeof etfQueuedTask>, admin: boolean) {
  await task.execute(
    'cancel',
    (state) => {
      const manual = etfQueuedDisplay(state)?.manual
      return admin && !!state.status && manualRunning(manual) && manual?.status !== 'cancelling'
    },
    async (state, patch) => {
      const manual = etfQueuedDisplay(state)!.manual!
      patch({ error: '' })
      try {
        const result = verifyReceipt(await cancelCollection(manual.id), manual.id)
        patch({ receipt: result })
      } catch (error) {
        patch({
          error: `${error instanceof Error ? collectionErrorMessage(error.message) : '取消请求失败'} 取消结果待确认，将继续查询状态；可重试取消同一任务。`,
        })
      }
    },
  )
}
