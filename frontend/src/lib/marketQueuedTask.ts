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
  marketCollectionNeedsReload,
} from './marketCollection'
import type { MarketCollectionStatus } from '../types/marketCollection'
import { createQueuedCollectionTask, type QueuedCollectionSnapshot } from './queuedCollectionTask'

interface MarketTaskState extends QueuedCollectionSnapshot<MarketCollectionStatus> {
  statusError: string
  actionError: string
  message: string
  uncertain: boolean
  confirming: boolean
  acceptedActive: boolean
  requestId: string | null
  awaitingRun: string | null
  busyConflict: boolean
}

export const marketQueuedTask = createQueuedCollectionTask<MarketCollectionStatus, MarketTaskState>(
  {
    initial: () => ({
      status: null,
      busy: '',
      historyRevision: 0,
      statusError: '',
      actionError: '',
      message: '',
      uncertain: false,
      confirming: false,
      acceptedActive: false,
      requestId: null,
      awaitingRun: null,
      busyConflict: false,
    }),
    readStatus: getMarketCollectionStatus,
    observe(state, next) {
      const matched = state.requestId && marketCollectionFindRun(next, state.requestId)
      let value = { ...state }
      if (matched) {
        value.requestId = null
        value.uncertain = false
        value.actionError = ''
        value.message = marketCollectionRunMessage(matched)
      }
      if (
        !value.awaitingRun ||
        marketCollectionFindRun(next, value.awaitingRun) ||
        marketCollectionActive(next)
      ) {
        value.awaitingRun = null
        value.acceptedActive = false
        value.confirming = false
        if (value.busyConflict) {
          value.busyConflict = marketCollectionActive(next)
          value.message = value.busyConflict
            ? '已有采集正在运行，正在查看当前采集进度。'
            : value.requestId
              ? '上次请求的结果仍待确认，可重试同一请求。'
              : '采集状态已更新，可按需重新发起采集。'
        }
      }
      if (marketCollectionNeedsReload(state.status, next)) value.historyRevision++
      value = { ...value, status: next, statusError: '' }
      return value
    },
    readFailed: (state) => ({ ...state, statusError: '暂时无法读取采集状态，已有图表仍可查看。' }),
    pollMs: (state) =>
      marketCollectionActive(state.status) ||
      state.status?.cache_pending ||
      state.confirming ||
      state.uncertain
        ? 1500
        : 30000,
  },
)

export const marketQueuedRunning = (state: MarketTaskState) =>
  marketCollectionActive(state.status) || state.acceptedActive

export const marketWorkerDisabled = (state: MarketTaskState) =>
  !!state.status?.worker_disabled || state.status?.status === 'worker_disabled'

export async function startMarketQueuedTask(
  task: ReturnType<typeof marketQueuedTask>,
  admin: boolean,
) {
  await task.execute(
    'run',
    (state) =>
      admin &&
      !marketQueuedRunning(state) &&
      !state.confirming &&
      !marketWorkerDisabled(state) &&
      !!state.status?.configured,
    async (state, patch) => {
      const hadUncertainRequest = state.requestId !== null
      const requestId = state.requestId ?? crypto.randomUUID()
      patch({ requestId, actionError: '', message: '', busyConflict: false })
      try {
        const accepted = await runMarketCollection(requestId)
        const active = ['queued', 'running'].includes(accepted.status)
        patch({
          requestId: null,
          uncertain: false,
          awaitingRun: active ? accepted.id : null,
          acceptedActive: active,
          confirming: active,
          message: marketCollectionRunMessage(accepted),
        })
      } catch (reason) {
        const failure = marketCollectionRunFailure(reason)
        const keepRequest = hadUncertainRequest || failure === 'uncertain'
        patch({ requestId: keepRequest ? requestId : null, uncertain: keepRequest })
        if (failure === 'busy') {
          patch({
            awaitingRun:
              reason instanceof MarketCollectionRequestError
                ? (reason.activeRun?.id ?? null)
                : null,
            acceptedActive: reason instanceof MarketCollectionRequestError && !!reason.activeRun,
            busyConflict: true,
            confirming: true,
            message: '采集服务已有任务占用，正在确认当前进度；请等待当前采集结束。',
          })
        } else {
          patch({
            actionError:
              failure === 'uncertain'
                ? '暂时无法确认采集请求的结果，正在核对状态。重试将沿用同一请求，不重复启动任务。'
                : marketCollectionErrorMessage(reason instanceof Error ? reason.message : ''),
          })
        }
      }
    },
  )
}
