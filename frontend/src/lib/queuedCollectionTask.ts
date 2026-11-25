export interface QueuedCollectionSnapshot<Status> {
  status: Status | null
  busy: '' | 'run' | 'cancel'
  historyRevision: number
}

interface QueuedCollectionAdapter<Status, State extends QueuedCollectionSnapshot<Status>> {
  initial: () => State
  readStatus: (signal: AbortSignal) => Promise<Status>
  observe: (state: State, next: Status) => State
  readFailed: (state: State, error: unknown) => State
  pollMs: (state: State) => number
}

/**
 * Shared transport lifecycle for idempotent queued jobs. The domain adapter
 * owns run identities, textual outcomes, repair choices and publication rules.
 */
function createController<Status, State extends QueuedCollectionSnapshot<Status>>(
  adapter: QueuedCollectionAdapter<Status, State>,
) {
  let state = adapter.initial()
  let request: AbortController | null = null
  let timer: ReturnType<typeof setTimeout> | undefined
  let generation = 0
  const listeners = new Set<() => void>()

  function publish(next: State) {
    state = next
    for (const listener of listeners) listener()
  }
  function patch(next: Partial<State>) {
    publish({ ...state, ...next })
  }
  function stopReading() {
    clearTimeout(timer)
    request?.abort()
    request = null
    generation++
  }
  async function pollNow() {
    if (!listeners.size || state.busy) return
    stopReading()
    const controller = new AbortController()
    request = controller
    const version = generation
    try {
      const next = await adapter.readStatus(controller.signal)
      if (!controller.signal.aborted && listeners.size && version === generation)
        publish(adapter.observe(state, next))
    } catch (error) {
      if (!controller.signal.aborted && listeners.size && version === generation)
        publish(adapter.readFailed(state, error))
    } finally {
      if (version === generation) {
        request = null
        if (listeners.size && !state.busy) timer = setTimeout(pollNow, adapter.pollMs(state))
      }
    }
  }
  async function execute(
    kind: 'run' | 'cancel',
    allowed: (state: State) => boolean,
    operation: (state: State, patch: (next: Partial<State>) => void) => Promise<void>,
  ) {
    if (!listeners.size || state.busy || !allowed(state)) return
    const before = state
    // Old observations cannot reopen a mutation lock, even if fetch ignores abort.
    stopReading()
    publish({ ...state, busy: kind })
    try {
      await operation(before, patch)
    } finally {
      // A dispatched POST survives navigation. Its domain result is retained
      // for the next view in this session; only status reads are view-owned.
      publish({ ...state, busy: '' })
      if (listeners.size) await pollNow()
    }
  }
  return {
    getSnapshot: () => state,
    subscribe(listener: () => void) {
      listeners.add(listener)
      if (listeners.size === 1 && !state.busy) void pollNow()
      return () => {
        listeners.delete(listener)
        if (!listeners.size) {
          stopReading()
          publish({ ...state, status: null })
        }
      }
    },
    patch,
    execute,
    pollNow,
  }
}

export function createQueuedCollectionTask<Status, State extends QueuedCollectionSnapshot<Status>>(
  adapter: QueuedCollectionAdapter<Status, State>,
) {
  const sessions = new WeakMap<object, ReturnType<typeof createController<Status, State>>>()
  return (session: object) => {
    let task = sessions.get(session)
    if (!task) {
      task = createController(adapter)
      sessions.set(session, task)
    }
    return task
  }
}
