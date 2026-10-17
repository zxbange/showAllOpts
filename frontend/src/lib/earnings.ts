export const sourceNames: Record<string, string> = {
  express: '业绩快报',
  forecast: '业绩预告',
  formal: '正式财报',
}
export const decimal = (n: number | null | undefined, digits = 2) =>
  n == null
    ? '—'
    : n.toLocaleString('zh-CN', { minimumFractionDigits: digits, maximumFractionDigits: digits })
export const percent = (n: number | null | undefined) => (n == null ? '—' : decimal(n) + '%')
export const quarter = (period: string) =>
  period.slice(0, 4) + ' 年 Q' + Math.ceil(Number(period.slice(5, 7)) / 3)
export const industryLevel = (level: string | null) =>
  ({ sw_l3: '申万三级', sw_l2: '申万二级', sw_l1: '申万一级' })[level ?? ''] ?? '行业未提供'
