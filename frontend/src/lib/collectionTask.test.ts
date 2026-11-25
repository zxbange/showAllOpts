import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  createCollectionTask,
  type CollectionTaskActivity,
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

function setup() {
  const readStatus = vi.fn<(signal: AbortSignal) => Promise<Status>>().mockResolvedValue(idle)
  const start = vi
    .fn<() => Promise<CollectionTaskStartResult>>()
    .mockResolvedValue({ started: true, run_id: 'new' })
  const task = createCollectionTask({
    readStatus,
    start,
    activity: (status) => status,
    needsHistoryReload: (old, next) => old.revision !== next.revision,
    errorText: (error) => String(error),
    noStartMessage: '未启动',
    activePollMs: 2000,
    idlePollMs: 15000,
  })
  const session = {}
  const controller = task(session)
  return { task, session, controller, readStatus, start }
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
})
