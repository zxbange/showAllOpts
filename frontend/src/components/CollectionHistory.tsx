import { useContext, useEffect, useState, type ReactNode } from 'react'
import { useLocation } from 'react-router-dom'
import {
  getPageCollectionHistory,
  type CollectionHistoryEntry,
  type CollectionErrorDetails,
} from '../api/collectionHistory'
import { collectionHistoryModule, collectionHistoryText } from '../lib/collectionHistory'
import { CollectionOutcomeContext } from './CollectionOutcome'
import '../styles/collection-history.css'
import { pollCollectionHistory } from '../lib/pollCollectionHistory'

export function CollectionHistoryFrame({
  children,
  hasRecords,
  loading = false,
  error = '',
  title = '最近采集记录',
}: {
  children?: ReactNode
  hasRecords: boolean
  loading?: boolean
  error?: string
  title?: string
}) {
  return (
    <details className="collection-history">
      <summary>{title}</summary>
      <p className="collection-history-note">最多显示最近 5 次记录 · 北京时间</p>
      {error && (
        <p className="collection-history-error">
          {error}
          {hasRecords ? '，以下为上次读取的记录。' : '，请稍后重试。'}
        </p>
      )}
      {loading && !hasRecords && <p>正在读取采集记录…</p>}
      {!loading && !error && !hasRecords && <p>暂无已完成的采集或同步记录。</p>}
      {hasRecords && <ul>{children}</ul>}
    </details>
  )
}

function isStrictDate(value: unknown): value is string {
  if (typeof value !== 'string' || !/^\d{4}-\d{2}-\d{2}$/.test(value)) return false
  const instant = Date.parse(`${value}T00:00:00Z`)
  return Number.isFinite(instant) && new Date(instant).toISOString().slice(0, 10) === value
}

/** Render only the safe error contract, never raw payloads or HTML. */
export function CollectionHistoryError({
  details,
  failed = false,
}: {
  details?: CollectionErrorDetails | null
  failed?: boolean
}) {
  if (!details || typeof details.message !== 'string' || !details.message.trim()) {
    return failed ? <p className="collection-history-note">未记录详细错误原因</p> : null
  }
  const stages: Record<string, string> = {
    fetch: '获取数据',
    validate: '校验数据',
    database_write: '写入数据库',
    cache_publish: '更新本地缓存',
    collection: '采集',
    database_sync: '数据库同步',
  }
  return (
    <details className="collection-history-error-detail">
      <summary>错误详情</summary>
      <p>{details.message}</p>
      {typeof details.http_status === 'number' &&
        Number.isInteger(details.http_status) &&
        details.http_status >= 100 &&
        details.http_status <= 599 && <p>HTTP 状态码：{details.http_status}</p>}
      {isStrictDate(details.trade_date) && <p>交易日期：{details.trade_date}</p>}
      {isStrictDate(details.start_date) && <p>窗口开始日期：{details.start_date}</p>}
      {isStrictDate(details.end_date) && <p>窗口结束日期：{details.end_date}</p>}
      {details.stage && stages[details.stage] && <p>失败阶段：{stages[details.stage]}</p>}
      {typeof details.mysql_errno === 'number' && Number.isInteger(details.mysql_errno) && (
        <p>MySQL 错误码：{details.mysql_errno}</p>
      )}
      {typeof details.sqlite_errno === 'number' && Number.isInteger(details.sqlite_errno) && (
        <p>SQLite 错误码：{details.sqlite_errno}</p>
      )}
    </details>
  )
}

function CollectionHistoryContent({ moduleKey, title }: { moduleKey: string; title?: string }) {
  const [items, setItems] = useState<CollectionHistoryEntry[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const { data } = useContext(CollectionOutcomeContext)
  const latest =
    moduleKey === 'social-feed'
      ? `${data.items['social-weibo']?.finished_at}/${data.items['social-xueqiu']?.finished_at}`
      : data.items[moduleKey]?.finished_at
  useEffect(
    () =>
      pollCollectionHistory(
        moduleKey,
        getPageCollectionHistory,
        (result) => {
          setItems(result.items)
          setError('')
          setLoading(false)
        },
        () => {
          setError(
            moduleKey === 'social-feed'
              ? '微博 / 雪球记录未能全部读取，无法确认合并的最近 5 次记录'
              : '暂时无法读取最新采集记录',
          )
          setLoading(false)
        },
      ),
    [moduleKey, latest],
  )
  return (
    <section className="panel collection-history-panel" data-history-module={moduleKey}>
      <CollectionHistoryFrame
        title={title}
        hasRecords={items.length > 0}
        loading={loading}
        error={error}
      >
        {items.slice(0, 5).map((item, index) => (
          <li key={`${item.run_key}-${index}`}>
            {collectionHistoryText(item)}
            <CollectionHistoryError
              details={item.error_details}
              failed={item.outcome === 'failed' || item.outcome === 'partial'}
            />
          </li>
        ))}
      </CollectionHistoryFrame>
    </section>
  )
}

export function CollectionHistoryPanel(props: { moduleKey: string; title?: string }) {
  return <CollectionHistoryContent key={props.moduleKey} {...props} />
}

export function PageCollectionHistory({ admin = false }: { admin?: boolean }) {
  const { pathname, search } = useLocation()
  const moduleKey = collectionHistoryModule(pathname, search, admin)
  return moduleKey ? <CollectionHistoryPanel key={moduleKey} moduleKey={moduleKey} /> : null
}
