import type { CollectionOutcome } from '../api/collectionOutcomes'

// Exact collection identities, never an aggregate navigation category.
export const collectionModuleKeys: Record<string, string> = Object.fromEntries(
  [
    'etf-shares',
    'growth-dividend',
    'market-crowding',
    'market-sentiment',
    'margin-guarantee',
    'valuation-ruler',
    'industry-momentum',
    'etf-momentum',
    'microcap-temperature',
    'microcap-breadth',
    'cash-cow',
    'dividend-selection',
    'institution-ownership',
  ].map((key) => [key, key]),
)
collectionModuleKeys['dividend-value'] = 'dividend-selection'
collectionModuleKeys['dividend-bank'] = 'dividend-selection'

export function collectionOutcomePresentation(item?: CollectionOutcome, unavailable = false) {
  const action = item?.kind === 'database_sync' ? '数据库同步' : '采集'
  if (unavailable)
    return { tone: 'unknown', symbol: '?', label: `${action}状态暂不可用，不能确认最近结果` }
  if (!item || !item.finished_at)
    return {
      tone: 'unknown',
      symbol: '·',
      label: item?.running ? `正在${action}；尚无已完成${action}记录` : `尚无已完成${action}记录`,
    }
  const labels: Record<string, string> = {
    unknown: '结果待确认（未确认有效数据）',
    success: '成功',
    failed: '失败',
    partial: '部分完成',
    cancelled: '已取消',
    interrupted: '已中断',
  }
  const state = labels[item.outcome]
  if (!state) return { tone: 'unknown', symbol: '?', label: '采集结果待确认' }
  const timestamp = Date.parse(item.finished_at)
  if (!Number.isFinite(timestamp))
    return { tone: 'unknown', symbol: '?', label: '采集结果时间待确认' }
  const when = new Date(timestamp).toLocaleString('zh-CN', {
    timeZone: 'Asia/Shanghai',
    hour12: false,
  })
  const trigger = item.trigger === 'manual' ? '手动' : item.trigger === 'scheduled' ? '自动' : ''
  const recovered =
    item.outcome === 'success' && item.publication_recovered_at
      ? Date.parse(item.publication_recovered_at)
      : NaN
  const publication = Number.isFinite(recovered)
    ? `；图表缓存随后于 ${new Date(recovered).toLocaleString('zh-CN', {
        timeZone: 'Asia/Shanghai',
        hour12: false,
      })} 发布成功`
    : ''
  const label = `${item.running ? `正在${action}；` : ''}最近一次已结束${trigger}${action}：${state}；${when}（北京时间）${publication}`
  if (item.running) return { tone: 'running', symbol: '…', label }
  if (item.outcome === 'success') return { tone: 'success', symbol: '✓', label }
  if (item.outcome === 'failed' || item.outcome === 'partial')
    return { tone: 'failed', symbol: '!', label }
  return { tone: 'unknown', symbol: '−', label }
}
