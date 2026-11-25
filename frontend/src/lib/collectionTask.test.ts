import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  createCollectionTask,
  type CollectionTaskActivity,
  type CollectionTaskAdapter,
  type CollectionTaskReader,
  type CollectionTaskStartResult,
} from './collectionTask'

type Status = CollectionTaskActivity & { revision: number }
const idle: Status = { running: false, databaseSyncing: false, runId: 'old', revision: 0 }
const active = (runId = 'new'): Status => ({ ...idle, running: true, runId })
const terminal = (runId = 'new'): Status => ({ ...idle, runId, revision: 1 })
function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (error: Error) => void
  const promise = new Promise<T>((yes, no) => {
    resolve = yes
    reject = no
  })
  return { promise, resolve, reject }
}

function setup(options: { cancellable?: boolean } & Partial<CollectionTaskAdapter<Status>> = {}) {
  const { cancellable, ...policy } = options
  const readStatus = vi.fn<(signal: AbortSignal) => Promise<Status>>().mockResolvedValue(idle)
  const start = vi
    .fn<() => Promise<CollectionTaskStartResult>>()
    .mockResolvedValue({ started: true, run_id: 'new' })
  const cancel = vi
    .fn<() => Promise<{ cancelled?: boolean }>>()
    .mockResolvedValue({ cancelled: true })
  const task = createCollectionTask({
    readStatus,
    start,
    ...(cancellable ? { cancel } : {}),
    activity: (status) => status,
    needsHistoryReload: (old, next) => old.revision !== next.revision,
    errorText: (error) => String(error),
    noStartMessage: '未启动',
    activePollMs: 2000,
    idlePollMs: 15000,
    ...policy,
  })
  const session = {}
  const controller = task(session)
  return { task, session, controller, readStatus, start, cancel }
}

const flush = () => vi.advanceTimersByTimeAsync(0)

describe('collection command and status lifecycle', () => {
  beforeEach(() => vi.useFakeTimers())
  afterEach(() => vi.useRealTimers())

  it('locks synchronously and requires matching terminal evidence despite stale idle reads', async () => {
    const { controller, readStatus, start } = setup()
    const stop = controller.subscribe(() => {})
    await flush()
    const request = deferred<CollectionTaskStartResult>()
    start.mockReturnValueOnce(request.promise)

    const first = controller.start(true)
    await controller.start(true)
    expect(start).toHaveBeenCalledTimes(1)
    expect(controller.getSnapshot().pending).toBe(true)

    request.resolve({ started: true, run_id: 'new' })
    await first
    expect(controller.getSnapshot().accepted).toBe(true)
    await vi.advanceTimersByTimeAsync(6000)
    expect(controller.getSnapshot().accepted).toBe(true)
    await controller.start(true)
    expect(start).toHaveBeenCalledTimes(1)

    // A terminal response for this run may arrive without ever observing running.
    readStatus.mockResolvedValue(terminal())
    await vi.advanceTimersByTimeAsync(2000)
    expect(controller.getSnapshot()).toMatchObject({
      pending: false,
      accepted: false,
      uncertain: false,
      status: terminal(),
      historyRevision: 2,
    })
    stop()
  })

  it('legacy accepted starts require an observed run and subscribers receive updates', async () => {
    const { controller, readStatus, start } = setup()
    const changed = vi.fn()
    const stop = controller.subscribe(changed)
    await flush()
    start.mockResolvedValueOnce({ started: true })
    await controller.start(true)
    expect(controller.getSnapshot().accepted).toBe(true)
    expect(changed).toHaveBeenCalled()
    readStatus.mockResolvedValue(active())
    await vi.advanceTimersByTimeAsync(2000)
    expect(controller.getSnapshot().accepted).toBe(true)
    readStatus.mockResolvedValue({ ...terminal(), databaseSyncing: true })
    await vi.advanceTimersByTimeAsync(2000)
    expect(controller.getSnapshot().accepted).toBe(true)
    readStatus.mockResolvedValue(terminal())
    await vi.advanceTimersByTimeAsync(2000)
    expect(controller.getSnapshot().accepted).toBe(false)
    stop()
    const notifications = changed.mock.calls.length
    await vi.advanceTimersByTimeAsync(20000)
    expect(changed).toHaveBeenCalledTimes(notifications)
  })

  it('handles a definite no-start separately from a lost response, and prevents viewer commands', async () => {
    const { controller, start } = setup()
    const stop = controller.subscribe(() => {})
    await controller.start(true)
    expect(start).not.toHaveBeenCalled()
    await flush()
    await controller.start(false)
    expect(start).not.toHaveBeenCalled()

    start.mockResolvedValueOnce({ started: false, detail: '未到采集时间' })
    await controller.start(true)
    expect(controller.getSnapshot()).toMatchObject({
      pending: false,
      accepted: false,
      uncertain: false,
      actionError: '未到采集时间',
    })
    start.mockRejectedValueOnce(new Error('network lost'))
    await controller.start(true)
    expect(controller.getSnapshot()).toMatchObject({ uncertain: true, pending: false })
    await controller.start(true)
    expect(start).toHaveBeenCalledTimes(2)
    stop()
  })

  it('maintains a pending POST during route departure and subsequent subscription', async () => {
    const { task, session, controller, readStatus, start } = setup()
    let stop = controller.subscribe(() => {})
    await flush()
    const post = deferred<CollectionTaskStartResult>()
    start.mockReturnValueOnce(post.promise)
    const command = controller.start(true)
    const reads = readStatus.mock.calls.length
    stop()
    expect(controller.getSnapshot().status).toBeNull()

    const same = task(session)
    expect(same).toBe(controller)
    stop = same.subscribe(() => {})
    expect(same.getSnapshot().pending).toBe(true)
    await vi.advanceTimersByTimeAsync(20000)
    expect(readStatus).toHaveBeenCalledTimes(reads)
    await same.start(true)
    expect(start).toHaveBeenCalledTimes(1)

    readStatus.mockResolvedValue(active())
    post.resolve({ started: true, run_id: 'new' })
    await command
    expect(same.getSnapshot()).toMatchObject({ pending: false, accepted: true, status: active() })
    stop()

    // Session replacement must never inherit the prior account's mutation flags.
    const other = task({})
    expect(other.getSnapshot()).toMatchObject({
      pending: false,
      accepted: false,
      uncertain: false,
      status: null,
    })
  })

  it('holds uncertain legacy commands across navigation until active then terminal, not database sync', async () => {
    const { controller, readStatus, start } = setup()
    let stop = controller.subscribe(() => {})
    await flush()
    start.mockRejectedValueOnce(new Error('lost'))
    await controller.start(true)
    stop()
    stop = controller.subscribe(() => {})
    await flush()
    expect(controller.getSnapshot().uncertain).toBe(true)
    readStatus.mockResolvedValue(active())
    await vi.advanceTimersByTimeAsync(2000)
    expect(controller.getSnapshot().uncertain).toBe(true)
    readStatus.mockResolvedValue({ ...idle, databaseSyncing: true })
    await vi.advanceTimersByTimeAsync(2000)
    expect(controller.getSnapshot().uncertain).toBe(true)
    readStatus.mockResolvedValue(terminal())
    await vi.advanceTimersByTimeAsync(2000)
    expect(controller.getSnapshot().uncertain).toBe(false)
    stop()
  })

  it('keeps a slow GET in flight, invalidates it at command start and discards its late result', async () => {
    const { controller, readStatus, start } = setup()
    const stop = controller.subscribe(() => {})
    await flush()
    const stale = deferred<Status>()
    readStatus.mockReturnValueOnce(stale.promise)
    await vi.advanceTimersByTimeAsync(15000)
    const signal = readStatus.mock.calls.at(-1)![0]
    await vi.advanceTimersByTimeAsync(60000)
    expect(readStatus).toHaveBeenCalledTimes(2)
    expect(signal.aborted).toBe(false)

    readStatus.mockResolvedValue(active())
    await controller.start(true)
    expect(signal.aborted).toBe(true)
    expect(controller.getSnapshot().status).toEqual(active())
    stale.resolve(idle)
    await flush()
    expect(controller.getSnapshot().status).toEqual(active())
    expect(controller.getSnapshot().accepted).toBe(true)
    expect(start).toHaveBeenCalledTimes(1)
    stop()
    await vi.advanceTimersByTimeAsync(60000)
    expect(readStatus).toHaveBeenCalledTimes(3)
  })

  it('never authorizes collection on unknown status or database sync; recovers a failed status poll', async () => {
    const { controller, readStatus, start } = setup()
    readStatus.mockRejectedValueOnce(new Error('unavailable'))
    const stop = controller.subscribe(() => {})
    await flush()
    expect(controller.getSnapshot().statusError).toContain('unavailable')
    await controller.start(true)
    readStatus.mockResolvedValue({ ...idle, databaseSyncing: true })
    await vi.advanceTimersByTimeAsync(15000)
    expect(controller.getSnapshot().statusError).toBe('')
    await controller.start(true)
    expect(start).not.toHaveBeenCalled()
    readStatus.mockResolvedValue(idle)
    await vi.advanceTimersByTimeAsync(2000)
    await controller.start(true)
    expect(start).toHaveBeenCalledTimes(1)
    stop()
  })

  it('does not publish a previous visit response after remount even if fetch ignores abort', async () => {
    const { controller, readStatus } = setup()
    const stale = deferred<Status>()
    readStatus.mockReturnValueOnce(stale.promise)
    const stop = controller.subscribe(() => {})
    const signal = readStatus.mock.calls[0][0]
    stop()
    readStatus.mockResolvedValue(active())
    const stopAgain = controller.subscribe(() => {})
    await flush()
    expect(signal.aborted).toBe(true)
    stale.resolve(idle)
    await flush()
    expect(controller.getSnapshot().status).toEqual(active())
    stopAgain()
  })
  it('unlocks a fast-completed legacy accepted start only on a strictly newer attempt', async () => {
    const { controller, readStatus, start } = setup()
    const prior = { ...idle, runId: undefined, attemptAt: '2026-11-24T16:00:00+08:00' }
    const newer = { ...prior, attemptAt: '2026-11-24T16:00:01+08:00' }
    readStatus.mockResolvedValue(prior)
    const stop = controller.subscribe(() => {})
    await flush()
    start.mockResolvedValueOnce({ started: true })
    await controller.start(true)
    expect(controller.getSnapshot().accepted).toBe(true)
    readStatus.mockResolvedValue({ ...prior, attemptAt: '2026-11-24T15:59:59+08:00' })
    await vi.advanceTimersByTimeAsync(2000)
    expect(controller.getSnapshot().accepted).toBe(true)
    expect(controller.getSnapshot().status).toEqual(prior)
    readStatus.mockResolvedValue(newer)
    await vi.advanceTimersByTimeAsync(2000)
    expect(controller.getSnapshot().accepted).toBe(false)
    // A timestamp only confirms that a new attempt was observed, not success.
    expect(controller.getSnapshot().status?.revision).toBe(0)
    stop()
  })

  it('cancel is allowed for a verified running public task, locks immediately, and survives remount', async () => {
    const { controller, readStatus, cancel } = setup({ cancellable: true })
    const before = { ...idle, runId: undefined, attemptAt: '2026-11-24T16:00:00+08:00' }
    const current = { ...before, running: true, attemptAt: '2026-11-24T16:00:01+08:00' }
    readStatus.mockResolvedValue(current)
    let stop = controller.subscribe(() => {})
    await flush()
    await controller.cancel(false)
    expect(cancel).not.toHaveBeenCalled()
    const response = deferred<{ cancelled?: boolean }>()
    cancel.mockReturnValueOnce(response.promise)
    const first = controller.cancel(true)
    await controller.cancel(true)
    expect(cancel).toHaveBeenCalledTimes(1)
    expect(controller.getSnapshot().pending).toBe(true)
    stop()
    stop = controller.subscribe(() => {})
    response.resolve({ cancelled: true })
    await first
    expect(controller.getSnapshot().stopping).toBe(true)
    await controller.cancel(true)
    expect(cancel).toHaveBeenCalledTimes(1)
    stop()
    readStatus.mockResolvedValue(before)
    stop = controller.subscribe(() => {})
    await flush()
    expect(controller.getSnapshot().stopping).toBe(true)
    expect(controller.getSnapshot().status).toBeNull()
    readStatus.mockResolvedValue({ ...current, running: false })
    await vi.advanceTimersByTimeAsync(2000)
    expect(controller.getSnapshot().stopping).toBe(false)
    expect(controller.getSnapshot().status?.running).toBe(false)
    stop()
  })

  it('refuses cancellation during database sync or already cancelling; false response does not invent a stop', async () => {
    const { controller, readStatus, cancel } = setup({ cancellable: true })
    readStatus.mockResolvedValue({ ...active(), databaseSyncing: true })
    const stop = controller.subscribe(() => {})
    await flush()
    await controller.cancel(true)
    expect(cancel).not.toHaveBeenCalled()
    readStatus.mockResolvedValue({ ...active(), cancelling: true })
    await vi.advanceTimersByTimeAsync(2000)
    await controller.cancel(true)
    expect(cancel).not.toHaveBeenCalled()
    readStatus.mockResolvedValue(active())
    await vi.advanceTimersByTimeAsync(2000)
    cancel.mockResolvedValueOnce({ cancelled: false })
    await controller.cancel(true)
    expect(controller.getSnapshot().stopping).toBe(false)
    expect(controller.getSnapshot().status?.running).toBe(true)
    stop()
  })

  it('explicit legacy display refresh reconciles ambiguity only through a new status read', async () => {
    const { controller, readStatus, start } = setup({ manualReconcile: true })
    const prior = { ...idle, runId: undefined, attemptAt: '2026-11-24T16:00:00+08:00' }
    readStatus.mockResolvedValue(prior)
    const stop = controller.subscribe(() => {})
    await flush()
    start.mockRejectedValueOnce(new Error('未能启动，请重试'))
    await controller.start(true)
    const actionError = controller.getSnapshot().actionError
    await vi.advanceTimersByTimeAsync(6000)
    expect(controller.getSnapshot().uncertain).toBe(true)
    readStatus.mockRejectedValueOnce(new Error('status unavailable'))
    await controller.reconcile()
    expect(controller.getSnapshot().uncertain).toBe(true)
    expect(controller.getSnapshot().statusError).toContain('status unavailable')
    await controller.reconcile()
    expect(controller.getSnapshot().uncertain).toBe(false)
    expect(controller.getSnapshot().actionError).toBe(actionError)
    expect(controller.getSnapshot().statusError).toBe('')
    await controller.start(true)
    expect(start).toHaveBeenCalledTimes(2)
    stop()
  })

  it('valuation remains strict: display refresh does not unlock an unknown POST', async () => {
    const { controller, readStatus, start, cancel } = setup()
    const stop = controller.subscribe(() => {})
    await flush()
    start.mockRejectedValueOnce(new Error('lost'))
    await controller.start(true)
    const count = readStatus.mock.calls.length
    await controller.reconcile()
    expect(readStatus).toHaveBeenCalledTimes(count)
    expect(controller.getSnapshot().uncertain).toBe(true)
    await controller.cancel(true)
    expect(cancel).not.toHaveBeenCalled()
    stop()
  })
  it('lets a combined commodity lock read confirm a quick finish without idle history polling', async () => {
    const { controller, readStatus, start } = setup({
      confirmFreshIdle: true,
      reconcileUnknownFreshIdle: true,
      idlePollMs: null,
    })
    const stop = controller.subscribe(() => {})
    await flush()
    await vi.advanceTimersByTimeAsync(60000)
    expect(readStatus).toHaveBeenCalledTimes(1)
    start.mockResolvedValueOnce({ started: true })
    await controller.start(true)
    expect(controller.getSnapshot().accepted).toBe(false)
    expect(readStatus).toHaveBeenCalledTimes(2)
    start.mockRejectedValueOnce(new Error('CCI更新未启动'))
    await controller.start(true)
    expect(controller.getSnapshot()).toMatchObject({
      uncertain: false,
      actionError: 'Error: CCI更新未启动',
    })
    await vi.advanceTimersByTimeAsync(60000)
    expect(readStatus).toHaveBeenCalledTimes(3)
    expect(start).toHaveBeenCalledTimes(2)
    stop()
  })

  it('keeps dividend waiting for a new run ID and reloads only verified terminal results', async () => {
    const { controller, readStatus, start } = setup({
      confirmByNewRunId: true,
      refreshAfterCommand: false,
      reloadOnConfirmation: true,
    })
    const stop = controller.subscribe(() => {})
    await flush()
    start.mockResolvedValueOnce({ started: true })
    await controller.start(true)
    expect(controller.getSnapshot()).toMatchObject({ accepted: true, historyRevision: 0 })
    await vi.advanceTimersByTimeAsync(2000)
    expect(controller.getSnapshot().accepted).toBe(true)
    readStatus.mockResolvedValue({ ...idle, runId: 'new' })
    await vi.advanceTimersByTimeAsync(2000)
    expect(controller.getSnapshot()).toMatchObject({ accepted: false, historyRevision: 1 })
    stop()
  })

  it('distinguishes a shared-busy notice, definite rejection and unavailable source', async () => {
    const definitive = new Error('not due')
    const { controller, readStatus, start } = setup({
      startAllowed: (status) => status.revision === 0,
      definiteRejection: (error) => error === definitive,
      noStartMessage: '',
      noStartNotice: '其他任务进行中',
      refreshAfterCommand: false,
    })
    const stop = controller.subscribe(() => {})
    await flush()
    start.mockResolvedValueOnce({ started: false })
    await controller.start(true)
    expect(controller.getSnapshot()).toMatchObject({ notice: '其他任务进行中', actionError: '' })
    start.mockRejectedValueOnce(definitive)
    await controller.start(true)
    expect(controller.getSnapshot()).toMatchObject({
      notice: '',
      uncertain: false,
      actionError: 'Error: not due',
    })
    readStatus.mockResolvedValue({ ...idle, revision: 1 })
    await vi.advanceTimersByTimeAsync(15000)
    await controller.start(true)
    expect(start).toHaveBeenCalledTimes(2)
    stop()
  })

  it('switches the period reader without discarding a pending shared cash-cow command', async () => {
    const { controller, start } = setup({ confirmByNewRunId: true })
    const ten = vi.fn<CollectionTaskReader<Status>>().mockResolvedValue(idle)
    const five = vi.fn<CollectionTaskReader<Status>>().mockResolvedValue(active())
    let stop = controller.subscribe(() => {}, ten)
    await flush()
    const post = deferred<CollectionTaskStartResult>()
    start.mockReturnValueOnce(post.promise)
    const command = controller.start(true)
    stop()
    stop = controller.subscribe(() => {}, five)
    expect(controller.getSnapshot().pending).toBe(true)
    expect(five).not.toHaveBeenCalled()
    await controller.start(true)
    expect(start).toHaveBeenCalledTimes(1)
    post.resolve({ started: true })
    await command
    expect(five).toHaveBeenCalledTimes(1)
    expect(five.mock.calls[0][1]).toMatchObject({ previous: null, refreshHistory: true })
    expect(controller.getSnapshot().status).toEqual(active())
    expect(ten).toHaveBeenCalledTimes(1)
    stop()
  })

  it('discards a late period reader response after switching views', async () => {
    const { controller } = setup()
    const slow = deferred<Status>()
    const ten = vi.fn<CollectionTaskReader<Status>>().mockReturnValue(slow.promise)
    const five = vi.fn<CollectionTaskReader<Status>>().mockResolvedValue(active())
    const stop = controller.subscribe(() => {}, ten)
    const oldSignal = ten.mock.calls[0][0]
    stop()
    const stopFive = controller.subscribe(() => {}, five)
    await flush()
    expect(oldSignal.aborted).toBe(true)
    slow.resolve(idle)
    await flush()
    expect(controller.getSnapshot().status).toEqual(active())
    stopFive()
  })
  it('cash-cow read retry rechecks the selected view without submitting another command', async () => {
    const { controller, start, readStatus } = setup({ manualReconcile: true, idlePollMs: null })
    const reader = vi.fn<CollectionTaskReader<Status>>().mockResolvedValue(idle)
    const detach = controller.subscribe(() => {}, reader)
    await flush()
    start.mockRejectedValueOnce(new Error('启动失败'))
    await controller.start(true)
    expect(controller.getSnapshot()).toMatchObject({
      actionError: 'Error: 启动失败',
      uncertain: true,
    })
    const reload = deferred<Status>()
    reader.mockReturnValueOnce(reload.promise)
    const retry = controller.recheck()
    expect(reader.mock.calls.at(-1)?.[1].refreshHistory).toBe(true)
    expect(controller.getSnapshot().uncertain).toBe(true)
    reload.resolve(idle)
    await retry
    expect(controller.getSnapshot()).toMatchObject({ actionError: '', uncertain: false })
    expect(start).toHaveBeenCalledTimes(1)
    expect(readStatus).not.toHaveBeenCalled()
    detach()
  })
})
