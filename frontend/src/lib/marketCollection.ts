import type { MarketCollectionStatus, MarketPointSource } from '../types/marketCollection'

export const marketCollectionActive = (status: MarketCollectionStatus | null | undefined) =>
  !status?.worker_disabled && ['queued', 'running'].includes(status?.status ?? '')

export function marketCollectionPublished(status: MarketCollectionStatus | null | undefined) {
  if (!status || marketCollectionActive(status) || status.cache_pending) return false
  return [status.heat_status, status.crowding_status].some((value) =>
    ['written', 'existing_preserved'].includes(value),
  )
}

/** State heartbeats never reload charts; only a newly published terminal result does. */
export function marketCollectionNeedsReload(
  previous: MarketCollectionStatus | null,
  next: MarketCollectionStatus,
) {
  if (!marketCollectionPublished(next)) return false
  // The first history request may have started just before this result was published.
  if (!previous) return true
  return (
    marketCollectionActive(previous) ||
    previous.cache_pending ||
    JSON.stringify([
      previous.run_id,
      previous.trade_date,
      previous.updated_at,
      previous.heat_status,
      previous.crowding_status,
    ]) !==
      JSON.stringify([
        next.run_id,
        next.trade_date,
        next.updated_at,
        next.heat_status,
        next.crowding_status,
      ])
  )
}

export const marketCollectionLabels: Record<string, string> = {
  idle: '等待下一次采集',
  disabled: '自动采集已关闭',
  worker_disabled: '服务端采集任务未启用',
  configuration_error: '采集配置待完善',
  queued: '等待执行',
  running: '采集中',
  paused: '采集已暂停，可重试',
  partial: '部分完成，待补齐',
  complete: '本轮采集完成',
  blocked: '校验未通过',
  failed: '本轮采集失败',
}

export const marketResultLabel = (value: string | undefined) =>
  ({
    pending: '等待完整数据',
    written: '已入库',
    existing_preserved: '保留已有记录',
    blocked: '未通过校验，未发布',
  })[value ?? ''] ?? '等待采集'

export function marketCollectionErrorMessage(value: string): string {
  const messages: Record<string, string> = {
    worker_disabled: '服务端采集任务未启用，请联系管理员启用服务后再采集。',
    missing_writer_configuration: '请先配置采集写入账号。',
    invalid_writer_configuration: '采集写入配置无效，请重新检查并保存。',
    configuration_unavailable: '暂时无法读取采集配置，请重试。',
    configuration_changed: '采集配置已更新，请重新读取配置后重试。',
    writer_permission_denied: '采集账号权限不足，请检查市场数据表的读写权限。',
    writer_table_missing: '市场数据表尚未准备好，请检查采集账号的建表权限。',
    writer_database_missing: '目标数据库不存在，请检查服务配置。',
    writer_connection_failed: '暂时无法连接数据库，请检查采集账号和网络。',
    writer_write_failed: '数据写入未完成，保留上次成功结果，请重试。',
    writer_target_mismatch: '写入目标与页面数据库不一致，请重新读取配置。',
    source_unavailable: '新浪数据暂不可用，请稍后重试。',
    source_not_ready: '新浪当日数据尚未准备好，稍后会继续采集。',
    source_rate_limited: '接口暂时限频，已保留进度，可稍后重试。',
    source_forbidden: '数据接口暂时拒绝访问，保留已有结果，请稍后重试。',
    source_partial: '当日数据尚未全部取得，已保留进度。',
    calendar_unavailable: '暂时无法读取交易日历，请稍后重试。',
    calendar_incomplete: '交易日历尚不完整，暂不发布新结果。',
    calendar_regressed: '交易日历校验未通过，暂不发布新结果。',
    snapshot_invalid: '当日全A快照未通过校验，暂不发布新结果。',
    snapshot_quote_mismatch: '当天股票列表与行情价量不一致，已保留记录，将按计划重新核对。',
    historical_snapshot_missing: '缺少目标交易日的完整快照，保留已有历史记录。',
    historical_snapshot_unverified:
      '缺少该日完整且带日期的原始快照，暂不发布该日结果；不能用今天行情补历史。',
    quote_unavailable: '部分股票数据暂不可用，已保留进度。',
    quote_mismatch: '股票数据日期或代码不一致，暂不发布当日比例。',
    unknown_missing_ma: '部分股票缺少可确认的MA20，暂不发布当日比例。',
    heat_methodology_unverified:
      '新浪市场热度尚未通过聚宽原脚本口径核验，暂不发布；抱团拥挤度按自身校验结果单独处理。',
    heat_methodology_mismatch: '已取得均线记录未通过聚宽口径核验，不作为新热度使用。',
    quote_stale: '部分股票仍是较早交易日数据，暂不发布当日比例。',
    writer_journal_incomplete: '结果同步记录尚未完整，保留上次成功结果。',
    writer_methodology_mismatch: '本次结果的统计口径未通过校验，未写入；已有数据保留。',
    writer_value_mismatch: '本次结果的计数、比例或金额不一致，未写入；已有数据保留。',
    writer_result_invalid: '结果未通过写入校验，保留上次成功结果。',
    before_collection_time: '当日采集从北京时间18:00开始，请在该时间后重试。',
    rate_limited: '接口暂时限频，已保留进度，可稍后重试。',
    incomplete_snapshot: '当日股票数据尚未齐全，暂不发布比例。',
    incomplete_market_data: '当日股票数据尚未齐全，暂不发布比例。',
    service_interrupted: '采集被中断，已保留进度，可重新采集。',
    cancelled: '采集已暂停，已保留进度。',
  }
  return (
    messages[value] ??
    (/[\u4e00-\u9fff]/.test(value) ? value : '操作未完成，请检查采集配置后重试。')
  )
}

export const heatDenominator = (point?: MarketPointSource | null) =>
  point?.denominator ?? 'eligible'

export function heatSourceLabel(point?: MarketPointSource | null) {
  const source = point?.source === 'sina' ? '新浪接口' : '聚宽历史'
  if (point?.source !== 'sina' && !point?.price_basis) return `${source} · 前复权MA20`
  return point.price_basis === 'qfq'
    ? `${source} · 前复权MA20`
    : point.price_basis === 'bfq'
      ? `${source} · 不复权MA20`
      : `${source} · MA20`
}

export const crowdingSourceLabel = (point?: MarketPointSource | null) =>
  point?.source === 'sina' ? '新浪接口 · 全A成交额' : '聚宽历史 · 全A成交额'

/** Consecutive methods are independent lines; a provider change is not a price move. */
export function marketSourceSegments<T extends MarketPointSource>(
  points: T[],
  kind: 'heat' | 'crowding',
) {
  const result: Array<{ start: number; end: number }> = []
  let previous = ''
  points.forEach((point, index) => {
    const source = point.source === 'sina' ? 'sina' : 'joinquant'
    const key =
      kind === 'heat'
        ? `${source}:${point.price_basis ?? (source === 'joinquant' ? 'qfq' : '')}:${heatDenominator(point)}`
        : source
    if (key !== previous) result.push({ start: index, end: index })
    else result[result.length - 1].end = index
    previous = key
  })
  return result
}
