export interface CollectionTaskActivity {
  running: boolean
  databaseSyncing: boolean
  runId?: string | null
}

export interface CollectionTaskStartResult {
  started?: boolean
  run_id?: string
  detail?: string
}

export interface CollectionTaskAdapter<Status> {
  readStatus: (signal: AbortSignal) => Promise<Status>
  start: () => Promise<CollectionTaskStartResult>
  activity: (status: Status) => CollectionTaskActivity
  needsHistoryReload: (previous: Status, next: Status) => boolean
  errorText: (reason: unknown) => string
  noStartMessage: string
  activePollMs: number
  idlePollMs: number
}

export interface CollectionTaskSnapshot<Status> {
  status: Status | null
  statusError: string
  actionError: string
  pending: boolean
  accepted: boolean
  uncertain: boolean
  historyRevision: number
}

/**
 * One command identity per task and authenticated session. GET lifetime follows
 * subscribers; a POST already sent to the server can settle after navigation.
 */
function createController<Status>(adapter: CollectionTaskAdapter<Status>) {
  let snapshot: CollectionTaskSnapshot<Status> = {
    status: null,
    statusError: '',
    actionError: '',
    pending: false,
    accepted: false,
    uncertain: false,
    historyRevision: 0,
  }
  let runId: string | null = null
  let observedRunning = false
  let request: AbortController | null = null
  let timer: ReturnType<typeof setTimeout> | undefined
  let epoch = 0
  const listeners = new Set<() => void>()

  function publish(patch: Partial<CollectionTaskSnapshot<Status>>) {
    snapshot = { ...snapshot, ...patch }
    for (const listener of listeners) listener()
  }

  function stopReading() {
    clearTimeout(timer)
    request?.abort()
    request = null
    epoch++
  }

  function schedule() {
    clearTimeout(timer)
    if (!listeners.size || snapshot.pending) return
    const activity = snapshot.status == null ? null : adapter.activity(snapshot.status)
    const active =
      activity?.running || activity?.databaseSyncing || snapshot.accepted || snapshot.uncertain
    timer = setTimeout(() => void readStatus(), active ? adapter.activePollMs : adapter.idlePollMs)
  }

  async function readStatus() {
    if (!listeners.size || snapshot.pending) return
    stopReading()
    const controller = new AbortController()
    request = controller
    const version = epoch
    try {
      const next = await adapter.readStatus(controller.signal)
      if (controller.signal.aborted || !listeners.size || version !== epoch) return
      const previous = snapshot.status
      const activity = adapter.activity(next)
      const matchingRun = !runId || activity.runId === runId
      if (activity.running && matchingRun) observedRunning = true
      const terminal = !activity.running && !activity.databaseSyncing
      let { accepted, uncertain } = snapshot
      if (accepted && terminal && (runId ? matchingRun : observedRunning)) {
        accepted = false
        runId = null
      }
      if (terminal && observedRunning) {
        uncertain = false
        observedRunning = false
      }
      publish({
        status: next,
        statusError: '',
        accepted,
        uncertain,
        historyRevision:
          previous != null && adapter.needsHistoryReload(previous, next)
            ? snapshot.historyRevision + 1
            : snapshot.historyRevision,
      })
    } catch (reason) {
      if (!controller.signal.aborted && listeners.size && version === epoch)
        publish({ statusError: adapter.errorText(reason) })
    } finally {
      if (version === epoch) {
        request = null
        schedule()
      }
    }
  }

  async function start(allowed: boolean) {
    if (
      !allowed ||
      !listeners.size ||
      snapshot.pending ||
      snapshot.accepted ||
      snapshot.uncertain ||
      snapshot.status == null ||
      snapshot.statusError
    )
      return
    const activity = adapter.activity(snapshot.status)
    if (activity.running || activity.databaseSyncing) return
    // Synchronous lock, before React rendering or any async work.
    stopReading()
    observedRunning = false
    runId = null
    publish({ pending: true, actionError: '' })
    try {
      const result = await adapter.start()
      if (result.started === true) {
        runId = result.run_id ?? null
        publish({ accepted: true })
      } else {
        publish({ actionError: result.detail ?? adapter.noStartMessage })
      }
    } catch (reason) {
      // A lost response is not proof of rejection. Never retry the mutation.
      publish({ uncertain: true, actionError: adapter.errorText(reason) })
    } finally {
      publish({ pending: false })
      if (listeners.size) {
        await readStatus()
        if (listeners.size) publish({ historyRevision: snapshot.historyRevision + 1 })
      }
    }
  }

  return {
    getSnapshot: () => snapshot,
    subscribe(listener: () => void) {
      listeners.add(listener)
      if (listeners.size === 1 && !snapshot.pending) void readStatus()
      return () => {
        listeners.delete(listener)
        if (!listeners.size) {
          stopReading()
          // No stale status from a prior page visit may authorize a new command.
          // Command confirmation, unlike view data, survives the route departure.
          publish({ status: null, statusError: '', actionError: '' })
        }
      }
    },
    start,
  }
}

export function createCollectionTask<Status>(adapter: CollectionTaskAdapter<Status>) {
  // AccountAccess owns a stable session object until login/expiry changes it.
  // Weak keys isolate users and let inactive sessions be collected.
  const controllers = new WeakMap<object, ReturnType<typeof createController<Status>>>()
  return (session: object) => {
    let controller = controllers.get(session)
    if (!controller) {
      controller = createController(adapter)
      controllers.set(session, controller)
    }
    return controller
  }
}
