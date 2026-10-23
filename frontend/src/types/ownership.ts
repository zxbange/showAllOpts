export interface OwnershipStock {
  code: string
  name: string
  industry: string | null
}
export interface Holding {
  shares: number | null
  market_value: number | null
  holder_count: number | null
  scope: string
  value_method: string
}
export interface OwnershipReport {
  date: string
  payload: {
    categories: Record<string, Holding | null>
    float_pub_date: string | null
    float_complete: boolean
  }
}
export interface HolderPoint {
  date: string
  pub_date: string | null
  payload: { a_share_holders: number | null; share_holders: number | null }
}
export interface OwnershipDetail {
  stock: OwnershipStock
  reports: OwnershipReport[]
  holders: HolderPoint[]
  holder_change: number | null
  holder_change_pct: number | null
}
export interface SocialMember {
  id: string | null
  name: string
  shares: number | null
  ratio: number | null
  market_value: number | null
}
export interface SocialEntry extends OwnershipStock {
  previous_shares?: number | null
  current_shares?: number | null
  change_shares?: number | null
  change_pct?: number | null
  new_combinations: SocialMember[]
  new_shares: number | null
  new_market_value: number | null
  report_date: string
  pub_date: string | null
}
export interface SocialEntries {
  period: string
  periods: string[]
  items: SocialEntry[]
  comparable_count: number
  unknown_count: number
  collected_count: number
}
