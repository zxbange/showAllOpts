export interface CollectionTaskActivity {
  running: boolean
  databaseSyncing: boolean
  runId?: string | null
  attemptAt?: string | null
  cancelling?: boolean
}

export interface CollectionTaskStartResult {
  started?: boolean
  run_id?: string
  detail?: string
}

export interface CollectionTaskAdapter<Status> {
  readStatus: (signal: AbortSignal) => Promise<Status>
  start: () => Promise<CollectionTaskStartResult>
  cancel?: () => Promise<{ cancelled?: boolean }>
  manualReconcile?: boolean
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
  stopping: boolean
  historyRevision: number
}

// Legacy public-collection APIs expose an attempt timestamp, not a run ID.
// Only a strictly newer server timestamp counts as a new attempt; no inference
// of success or publication is made from it.
function attemptAdvanced(before?: string | null, after?: string | null) {
  const next = Date.parse(after ?? '')
  return Number.isFinite(next) && (!before || next > Date.parse(before))
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
    stopping: false,
    historyRevision: 0,
  }
  let runId: string | null = null
  let observedRunning = false
  let baselineAttempt: string | null | undefined
  let latestAttemptAt: string | undefined
  let cancelTarget: CollectionTaskActivity | null = null
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
      activity?.running ||
      activity?.databaseSyncing ||
      snapshot.accepted ||
      snapshot.uncertain ||
      snapshot.stopping
    timer = setTimeout(() => void readStatus(), active ? adapter.activePollMs : adapter.idlePollMs)
  }

  async function readStatus(manuallyReconcile = false, refreshHistory = false) {
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
      // Remember the latest server attempt across route changes too. A legacy
      // idle snapshot older than an observed attempt must not reopen controls.
      if (
        latestAttemptAt &&
        (!Number.isFinite(Date.parse(activity.attemptAt ?? '')) ||
          attemptAdvanced(activity.attemptAt, latestAttemptAt))
      ) {
        publish({
          statusError: '采集状态已过期，正在重新读取。',
          historyRevision: snapshot.historyRevision + (refreshHistory ? 1 : 0),
        })
        return
      }
      if (activity.attemptAt && Number.isFinite(Date.parse(activity.attemptAt)))
        latestAttemptAt = activity.attemptAt
      const matchingRun = !runId || activity.runId === runId
      if (activity.running && matchingRun) observedRunning = true
      const terminal = !activity.running && !activity.databaseSyncing
      const changedAttempt = attemptAdvanced(baselineAttempt, activity.attemptAt)
      let { accepted, uncertain, stopping } = snapshot
      if (accepted && terminal && (runId ? matchingRun : observedRunning || changedAttempt)) {
        accepted = false
        runId = null
      }
      if (
        terminal &&
        (observedRunning ||
          (uncertain && changedAttempt) ||
          (uncertain && manuallyReconcile && adapter.manualReconcile))
      ) {
        uncertain = false
        observedRunning = false
      }
      if (cancelTarget) {
        const replaced = attemptAdvanced(cancelTarget.attemptAt, activity.attemptAt)
        const matchesTarget = !cancelTarget.runId || activity.runId === cancelTarget.runId
        if (replaced || (terminal && matchesTarget)) {
          stopping = false
          uncertain = false
          cancelTarget = null
        }
      }
      publish({
        status: next,
        statusError: '',
        accepted,
        uncertain,
        stopping,
        historyRevision:
          refreshHistory || (previous != null && adapter.needsHistoryReload(previous, next))
            ? snapshot.historyRevision + 1
            : snapshot.historyRevision,
      })
    } catch (reason) {
      if (!controller.signal.aborted && listeners.size && version === epoch)
        publish({
          statusError: adapter.errorText(reason),
          historyRevision: snapshot.historyRevision + (refreshHistory ? 1 : 0),
        })
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
      snapshot.stopping ||
      snapshot.status == null ||
      snapshot.statusError
    )
      return
    const activity = adapter.activity(snapshot.status)
    if (activity.running || activity.databaseSyncing) return
    // Synchronous lock, before React rendering or any async work.
    stopReading()
    observedRunning = false
    baselineAttempt = activity.attemptAt
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
      if (listeners.size) await readStatus(false, true)
    }
  }

  async function cancel(allowed: boolean) {
    if (
      !adapter.cancel ||
      !allowed ||
      !listeners.size ||
      snapshot.pending ||
      snapshot.stopping ||
      snapshot.uncertain ||
      snapshot.status == null ||
      snapshot.statusError
    )
      return
    const activity = adapter.activity(snapshot.status)
    if (!activity.running || activity.databaseSyncing || activity.cancelling) return
    stopReading()
    cancelTarget = activity
    observedRunning = true
    publish({ pending: true, actionError: '' })
    try {
      const result = await adapter.cancel()
      if (result.cancelled === true) publish({ stopping: true })
      else cancelTarget = null
    } catch (reason) {
      publish({ uncertain: true, actionError: adapter.errorText(reason) })
    } finally {
      publish({ pending: false })
      if (listeners.size) await readStatus(false, true)
    }
  }

  async function reconcile() {
    // Existing display-refresh actions may explicitly recheck an ambiguous
    // legacy command. Ordinary history reads and background polling never
    // clear that lock merely because an old idle response is returned.
    if (adapter.manualReconcile && snapshot.uncertain && !snapshot.pending) await readStatus(true)
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
    cancel,
    reconcile,
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
