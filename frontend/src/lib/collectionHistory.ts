import type { CollectionHistoryEntry } from '../api/collectionHistory'
import { dataUpdateInstant, formatDataUpdatedAt } from './dataUpdatedAt'
import { cashCowPeriod } from './cashCow'

const direct = new Set([
  'growth-dividend',
  'market-crowding',
  'market-sentiment',
  'margin-guarantee',
  'valuation-ruler',
  'industry-momentum',
  'etf-momentum',
  'microcap-temperature',
  'microcap-breadth',
  'dividend-selection',
  'institution-ownership',
])
export function collectionHistoryModule(
  pathname: string,
  search: string,
  admin = false,
): string | null {
  const page = pathname.split('/')[1]
  const params = new URLSearchParams(search)
  if (page === 'cash-cow') return `cash-cow-${cashCowPeriod(params.get('years'))}`
  if (page === 'commodities')
    return params.get('instrument') === 'cci' ? 'commodities-cci' : 'commodities-a00'
  if (page === 'personal-holdings') return admin ? page : null
  // ETF uses its richer native run journal; social views have separate source panels.
  return direct.has(page) ? page : null
}
export function collectionHistoryText(record: CollectionHistoryEntry) {
  const labels: Record<string, string> = {
    success: '成功',
    failed: '失败',
    partial: '部分完成，仍有缺口',
    cancelled: '已取消',
    interrupted: '已中断',
    unknown: '结果待确认',
  }
  const action = record.kind === 'database_sync' ? '数据库同步' : '采集'
  const trigger =
    record.trigger === 'manual'
      ? '手动'
      : record.trigger === 'scheduled'
        ? '自动'
        : '触发方式未记录的'
  const time =
    dataUpdateInstant(record.finished_at) != null
      ? formatDataUpdatedAt(record.finished_at)
      : '完成时间未记录'
  const publication =
    record.outcome === 'success' && dataUpdateInstant(record.publication_recovered_at) != null
      ? ` · 缓存随后发布成功（${formatDataUpdatedAt(record.publication_recovered_at)}）`
      : ''
  return `${record.source_name ? `${record.source_name} · ` : ''}${time} · ${trigger}${action} · ${labels[record.outcome] || '状态待确认'}${publication}`
}
