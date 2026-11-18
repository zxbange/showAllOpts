/** A date alone is not a data-change timestamp. Never use the browser's local zone. */
export function dataUpdateInstant(value?: string | null, naiveBeijing = false): number | null {
  if (
    !value ||
    !/^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?$/.test(value)
  )
    return null
  const parts = value
    .slice(0, 19)
    .split(/[-T :]/)
    .map(Number)
  const [year, month, day, hour, minute, second] = parts
  if (
    month < 1 ||
    month > 12 ||
    day < 1 ||
    day > new Date(Date.UTC(year, month, 0)).getUTCDate() ||
    hour > 23 ||
    minute > 59 ||
    second > 59
  )
    return null
  const zoned = /(?:Z|[+-]\d{2}:\d{2})$/.test(value)
  if (!zoned && !naiveBeijing) return null
  const instant = Date.parse(value.replace(' ', 'T') + (zoned ? '' : '+08:00'))
  return Number.isFinite(instant) ? instant : null
}
export function formatDataUpdatedAt(value?: string | null, naiveBeijing = false): string {
  const instant = dataUpdateInstant(value, naiveBeijing)
  if (instant == null) return '未提供可确认的秒级数据时间'
  return new Intl.DateTimeFormat('sv-SE', {
    timeZone: 'Asia/Shanghai',
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hourCycle: 'h23',
  }).format(instant)
}
export function latestDataUpdatedAt(
  values: (string | null | undefined)[],
  naiveBeijing = false,
): string | null {
  return values.reduce<string | null>((latest, value) => {
    const stamp = dataUpdateInstant(value, naiveBeijing)
    return stamp != null && (latest == null || stamp > dataUpdateInstant(latest, naiveBeijing)!)
      ? value!
      : latest
  }, null)
}
