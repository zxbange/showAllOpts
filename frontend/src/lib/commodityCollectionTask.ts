import { apiFetch } from '../api/http'
import { createCollectionTask, type CollectionTaskStartResult } from './collectionTask'

/** The commodity APIs combine saved history and the live worker lock status. */
export function createCommodityCollectionTask<Data extends { status: { running: boolean } }>(
  instrument: 'a00' | 'cci',
  readMessage: string,
  startMessage: string,
) {
  const path = '/api/v1/commodities/' + instrument
  return createCollectionTask<Data>({
    async readStatus(signal) {
      const result = await apiFetch(path, { signal, cache: 'no-store' })
      if (!result.ok) throw new Error(readMessage)
      return (await result.json()) as Data
    },
    async start() {
      const result = await apiFetch(path + '/refresh', { method: 'POST' })
      if (!result.ok) throw new Error(startMessage)
      return (await result.json()) as CollectionTaskStartResult
    },
    activity: (data) => ({ running: data.status.running, databaseSyncing: false }),
    needsHistoryReload: () => false,
    errorText: (error) => (error instanceof Error ? error.message : '暂时无法连接数据服务。'),
    noStartMessage: '',
    // The GET checks the server's actual collector lock. A fast-completed
    // job need not expose a running sample. A verified idle read also permits
    // a user-directed retry after a lost reply; never retry POST automatically.
    confirmFreshIdle: true,
    reconcileUnknownFreshIdle: true,
    activePollMs: 2000,
    idlePollMs: null,
  })
}
