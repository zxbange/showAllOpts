export type ShortMethod = 'bukou' | 'redhover' | 'xuli'
export interface ShortBar {
  date: string; open: number; close: number; low: number; high: number; volume: number
  ma: Record<string, number | null>
}
export interface ShortStock {
  code: string; name: string; methods: ShortMethod[]; industry: string | null
  close: number; pct_change: number | null; volume: number
  pe: number | null; pb: number | null; market_cap: number | null; bars: ShortBar[]
}
export interface ShortOverview {
  trade_date: string | null; dates: string[]; method: ShortMethod
  counts: Record<ShortMethod, number>; meta: { universe_count?: number; evaluated_count?: number }
  total: number; offset: number; limit: number; items: ShortStock[]
}
