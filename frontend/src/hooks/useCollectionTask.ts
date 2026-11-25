import { useCallback, useSyncExternalStore } from 'react'
import type { CollectionTaskReader, createCollectionTask } from '../lib/collectionTask'

const anonymousScope = {}

export function useCollectionTask<Status>(
  task: ReturnType<typeof createCollectionTask<Status>>,
  session: object | null,
  canStart: boolean,
  reader?: CollectionTaskReader<Status>,
) {
  const controller = task(session ?? anonymousScope)
  const subscribe = useCallback(
    (listener: () => void) => controller.subscribe(listener, reader),
    [controller, reader],
  )
  const state = useSyncExternalStore(subscribe, controller.getSnapshot)
  const allowed = session != null && canStart
  return {
    ...state,
    act: () => controller.start(allowed),
    cancel: () => controller.cancel(allowed),
    reconcile: controller.reconcile,
    recheck: controller.recheck,
  }
}
