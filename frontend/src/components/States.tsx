import { Database, RefreshCw, SearchX, Unplug } from 'lucide-react'

export function ErrorState({ message, retry }: { message: string; retry: () => void }) {
  return (
    <div className="state-panel" role="alert">
      <span className="state-icon">
        <Unplug size={27} />
      </span>
      <h2>数据加载失败</h2>
      <p>{message}</p>
      <button className="button button-primary" onClick={retry}>
        <RefreshCw size={16} />
        重新加载
      </button>
    </div>
  )
}
export function EmptyState({
  title = '暂无行业动量数据',
  description = '请先在聚宽采集入库，再通过“数据同步”更新本模块。',
  search = false,
}: {
  title?: string
  description?: string
  search?: boolean
}) {
  return (
    <div className="state-panel">
      <span className="state-icon">{search ? <SearchX size={27} /> : <Database size={27} />}</span>
      <h2>{title}</h2>
      <p>{description}</p>
    </div>
  )
}
export function LoadingState() {
  return (
    <div className="loading-state" aria-label="正在加载数据" role="status">
      <div className="stats-grid">
        {[0, 1, 2, 3].map((i) => (
          <div key={i} className="skeleton skeleton-stat" />
        ))}
      </div>
      <div className="skeleton skeleton-chart" />
      <span className="sr-only">正在加载数据…</span>
    </div>
  )
}
