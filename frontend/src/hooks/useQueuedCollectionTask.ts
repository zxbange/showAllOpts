import { useEffect, useRef, useSyncExternalStore } from 'react'
import type {
  createQueuedCollectionTask,
  QueuedCollectionSnapshot,
} from '../lib/queuedCollectionTask'

const anonymousScope = {}

export function useQueuedCollectionTask<Status, State extends QueuedCollectionSnapshot<Status>>(
  registry: ReturnType<typeof createQueuedCollectionTask<Status, State>>,
  session: object | null,
  onCollected: () => void,
) {
  const task = registry(session ?? anonymousScope)
  const state = useSyncExternalStore(task.subscribe, task.getSnapshot, task.getSnapshot)
  const seen = useRef(state.historyRevision)
  useEffect(() => {
    if (state.historyRevision !== seen.current) {
      seen.current = state.historyRevision
      onCollected()
    }
  }, [state.historyRevision, onCollected])
  return { task, state }
}
