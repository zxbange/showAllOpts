import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createQueuedCollectionTask, type QueuedCollectionSnapshot } from './queuedCollectionTask'
import type { ETFCollectionStatus, ETFManualRun } from '../types/etf'
import type { MarketCollectionRun, MarketCollectionStatus } from '../types/marketCollection'
import { getETFCollectionStatus } from '../api/client'
import { cancelCollection, runCollection } from '../api/etfCollection'
import { getMarketCollectionStatus, runMarketCollection } from '../api/marketCollection'
import {
  cancelETFQueuedTask,
  etfQueuedDisplay,
  etfQueuedTask,
  startETFQueuedTask,
} from './etfQueuedTask'
import { marketQueuedRunning, marketQueuedTask, startMarketQueuedTask } from './marketQueuedTask'

vi.mock('../api/client', () => ({ getETFCollectionStatus: vi.fn() }))
vi.mock('../api/etfCollection', () => ({ runCollection: vi.fn(), cancelCollection: vi.fn() }))
vi.mock('../api/marketCollection', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../api/marketCollection')>()),
  getMarketCollectionStatus: vi.fn(),
  runMarketCollection: vi.fn(),
}))

const flush = () => vi.advanceTimersByTimeAsync(0)
function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>((done) => {
    resolve = done
  })
  return { promise, resolve }
}
const marketIdle: MarketCollectionStatus = {
  enabled: true,
  configured: true,
  status: 'idle',
  timezone: 'Asia/Shanghai',
  schedule: [],
  trade_date: null,
  total: 0,
  completed: 0,
  pending: 0,
  above_count: 0,
  heat_status: 'pending',
  crowding_status: 'pending',
  cache_pending: false,
  updated_at: null,
  error: null,
}
const etfReady: ETFCollectionStatus = { configured: true, status: 'configured' }

describe('shared queued command transport', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    vi.resetAllMocks()
  })
  afterEach(() => vi.useRealTimers())

  it('keeps a dispatched command across unmount and discards stale observations', async () => {
    type State = QueuedCollectionSnapshot<string> & { commandId: string | null }
    const read = vi.fn<(signal: AbortSignal) => Promise<string>>().mockResolvedValue('idle')
    const registry = createQueuedCollectionTask<string, State>({
      initial: () => ({ status: null, busy: '', historyRevision: 0, commandId: null }),
      readStatus: read,
      observe: (state, next) => ({ ...state, status: next }),
      readFailed: (state) => state,
      pollMs: () => 2000,
    })
    const scope = {}
    const task = registry(scope)
    let detach = task.subscribe(() => {})
    await flush()
    const stale = deferred<string>()
    read.mockReturnValueOnce(stale.promise)
    await vi.advanceTimersByTimeAsync(2000)
    const signal = read.mock.calls.at(-1)![0]
    const command = deferred<string>()
    const execute = vi.fn(async (_state: State, patch: (next: Partial<State>) => void) => {
      patch({ commandId: await command.promise })
    })
    const first = task.execute('run', () => true, execute)
    await task.execute('run', () => true, execute)
    expect(execute).toHaveBeenCalledTimes(1)
    expect(signal.aborted).toBe(true)
    detach()
    detach = registry(scope).subscribe(() => {})
    expect(task.getSnapshot().busy).toBe('run')
    await vi.advanceTimersByTimeAsync(20000)
    expect(read).toHaveBeenCalledTimes(2)
    read.mockResolvedValue('running')
    command.resolve('same-command')
    await first
    stale.resolve('stale')
    await flush()
    expect(task.getSnapshot()).toMatchObject({
      status: 'running',
      busy: '',
      commandId: 'same-command',
    })
    expect(registry({}).getSnapshot().commandId).toBeNull()
    detach()
    const reads = read.mock.calls.length
    await vi.advanceTimersByTimeAsync(60000)
    expect(read).toHaveBeenCalledTimes(reads)
  })

  it('persists market same-request recovery across heat and crowding visits', async () => {
    const read = vi.mocked(getMarketCollectionStatus).mockResolvedValue(marketIdle)
    const run = vi.mocked(runMarketCollection).mockRejectedValueOnce(new Error('lost response'))
    const session = {}
    const task = marketQueuedTask(session)
    let detach = task.subscribe(() => {})
    await flush()
    await startMarketQueuedTask(task, true)
    const id = run.mock.calls[0][0]
    expect(task.getSnapshot()).toMatchObject({ requestId: id, uncertain: true })
    detach()
    detach = marketQueuedTask(session).subscribe(() => {})
    await flush()
    run.mockResolvedValueOnce({ id, status: 'queued' })
    await startMarketQueuedTask(task, true)
    expect(run.mock.calls[1][0]).toBe(id)
    expect(marketQueuedRunning(task.getSnapshot())).toBe(true)
    await startMarketQueuedTask(task, true)
    expect(run).toHaveBeenCalledTimes(2)
    read.mockResolvedValue({ ...marketIdle, manual: { id, status: 'running' }, running: true })
    await vi.advanceTimersByTimeAsync(1500)
    expect(marketQueuedRunning(task.getSnapshot())).toBe(true)
    detach()
  })

  it('does not discard a market acknowledgement when its initiating view disappears', async () => {
    vi.mocked(getMarketCollectionStatus).mockResolvedValue(marketIdle)
    const response = deferred<MarketCollectionRun>()
    const run = vi.mocked(runMarketCollection).mockReturnValue(response.promise)
    const task = marketQueuedTask({})
    let detach = task.subscribe(() => {})
    await flush()
    const operation = startMarketQueuedTask(task, true)
    const id = run.mock.calls[0][0]
    detach()
    detach = task.subscribe(() => {})
    expect(task.getSnapshot().busy).toBe('run')
    response.resolve({ id, status: 'queued' })
    await operation
    expect(task.getSnapshot()).toMatchObject({
      awaitingRun: id,
      acceptedActive: true,
      confirming: true,
    })
    expect(run).toHaveBeenCalledTimes(1)
    detach()
  })

  it('preserves ETF repair choice and request ID across visits, retains receipt through stale idle', async () => {
    const read = vi
      .mocked(getETFCollectionStatus)
      .mockResolvedValue({ ...etfReady, retry_exhausted_count: 2 })
    const run = vi.mocked(runCollection).mockRejectedValueOnce(new Error('connection lost'))
    const task = etfQueuedTask({})
    let detach = task.subscribe(() => {})
    await flush()
    task.patch({ retryExhausted: true })
    await startETFQueuedTask(task, true)
    const id = run.mock.calls[0][0]
    expect(run.mock.calls[0]).toEqual([id, true])
    detach()
    detach = task.subscribe(() => {})
    await flush()
    run.mockResolvedValueOnce({ id, status: 'queued' })
    await startETFQueuedTask(task, true)
    expect(run.mock.calls[1]).toEqual([id, true])
    expect(etfQueuedDisplay(task.getSnapshot())?.manual).toEqual({ id, status: 'queued' })
    await startETFQueuedTask(task, true)
    expect(run).toHaveBeenCalledTimes(2)
    read.mockResolvedValue({ ...etfReady, manual: { id, status: 'running' } })
    await vi.advanceTimersByTimeAsync(2000)
    expect(task.getSnapshot().receipt).toBeNull()
    expect(task.getSnapshot().status?.manual?.id).toBe(id)
    detach()
  })

  it('ETF cancellation remains targeted and stale running status cannot undo a cancellation receipt', async () => {
    const manual: ETFManualRun = { id: 'job-1', status: 'running' }
    const read = vi.mocked(getETFCollectionStatus).mockResolvedValue({ ...etfReady, manual })
    const cancel = vi
      .mocked(cancelCollection)
      .mockResolvedValue({ ...manual, status: 'cancelling' })
    const task = etfQueuedTask({})
    const detach = task.subscribe(() => {})
    await flush()
    await cancelETFQueuedTask(task, true)
    expect(cancel).toHaveBeenCalledWith('job-1')
    expect(etfQueuedDisplay(task.getSnapshot())?.manual?.status).toBe('cancelling')
    await cancelETFQueuedTask(task, true)
    expect(cancel).toHaveBeenCalledTimes(1)
    read.mockResolvedValue({ ...etfReady, manual: { ...manual, status: 'cancelled' } })
    await vi.advanceTimersByTimeAsync(2000)
    expect(etfQueuedDisplay(task.getSnapshot())?.manual?.status).toBe('cancelled')
    expect(task.getSnapshot().receipt).toBeNull()
    detach()
  })

  it('ETF publication waits for cache completion and reloads once only', async () => {
    const run: ETFManualRun = { id: 'job', status: 'running' }
    const read = vi.mocked(getETFCollectionStatus).mockResolvedValue({ ...etfReady, manual: run })
    const task = etfQueuedTask({})
    const detach = task.subscribe(() => {})
    await flush()
    read.mockResolvedValue({
      ...etfReady,
      manual: { ...run, status: 'complete' },
      cache_pending: true,
    })
    await vi.advanceTimersByTimeAsync(2000)
    expect(task.getSnapshot().historyRevision).toBe(0)
    read.mockResolvedValue({
      ...etfReady,
      manual: { ...run, status: 'complete' },
      cache_pending: false,
    })
    await vi.advanceTimersByTimeAsync(2000)
    expect(task.getSnapshot().historyRevision).toBe(1)
    await vi.advanceTimersByTimeAsync(30000)
    expect(task.getSnapshot().historyRevision).toBe(1)
    detach()
  })
  it('a mismatched ETF acknowledgement cannot discard the request identity', async () => {
    vi.mocked(getETFCollectionStatus).mockResolvedValue(etfReady)
    const run = vi
      .mocked(runCollection)
      .mockResolvedValueOnce({ id: 'different', status: 'queued' })
    const task = etfQueuedTask({})
    const detach = task.subscribe(() => {})
    await flush()
    await startETFQueuedTask(task, true)
    const id = run.mock.calls[0][0]
    expect(task.getSnapshot()).toMatchObject({ requestId: id, uncertain: true, receipt: null })
    run.mockResolvedValueOnce({ id, status: 'queued' })
    await startETFQueuedTask(task, true)
    expect(run.mock.calls[1][0]).toBe(id)
    expect(etfQueuedDisplay(task.getSnapshot())?.manual?.id).toBe(id)
    detach()
  })
})
