import { useSyncExternalStore } from 'react'
import type { createCollectionTask } from '../lib/collectionTask'

const anonymousScope = {}

export function useCollectionTask<Status>(
  task: ReturnType<typeof createCollectionTask<Status>>,
  session: object | null,
  canStart: boolean,
) {
  const controller = task(session ?? anonymousScope)
  const state = useSyncExternalStore(controller.subscribe, controller.getSnapshot)
  const allowed = session != null && canStart
  return {
    ...state,
    act: () => controller.start(allowed),
    cancel: () => controller.cancel(allowed),
    reconcile: controller.reconcile,
  }
}
