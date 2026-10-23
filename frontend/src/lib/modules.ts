import {
  Users,
  TrendingUp,
  Coins,
  ArrowUpDown,
  FileSearch,
  Activity,
  ChartNoAxesCombined,
  Grid2X2,
  Thermometer,
} from 'lucide-react'
import type { LucideIcon } from 'lucide-react'

// Register a new page here to add its overview card, search, category and navigation.
export interface ResearchModule {
  id: string
  name: string
  path: string
  category: string
  english: string
  description: string
  tags: string[]
  icon: LucideIcon
  tone: 'coral' | 'jade' | 'gold'
  illustration:
    | 'heatmap'
    | 'concentration'
    | 'temperature'
    | 'shares'
    | 'earnings'
    | 'breadth'
    | 'dividend'
    | 'growth'
    | 'etf-heat'
    | 'ownership'
}

const individualModules: ResearchModule[] = [
  { id: 'short-term', name: '短线选股', path: '/short-term', category: '趋势观察',
    english: 'SHORT TERM PATTERNS',
    description: '补票龙、红悬停龙、蓄力龙选股结果及K线。',
    tags: ['补票龙', '红悬停龙', '蓄力龙'], icon: ChartNoAxesCombined, tone: 'coral', illustration: 'breadth' },
  {
    id: 'institution-ownership',
    name: '个股机构持仓',
    path: '/institution-ownership',
    category: '筹码观察',
    english: 'INSTITUTION OWNERSHIP',
    description: '七类机构持仓、股东户数、社保新进与加仓。',
    tags: ['机构持仓', '股东户数', '社保新进'],
    icon: Users,
    tone: 'gold',
    illustration: 'ownership',
  },
  {
    id: 'etf-momentum',
    name: 'ETF动量热度',
    path: '/etf-momentum',
    category: '趋势观察',
    english: 'ETF MOMENTUM AT THE CLOSE',
    description: 'ETF / LOF 动量排名及历史热度。',
    tags: ['ETF热力图', '跨资产', '收盘动量'],
    icon: TrendingUp,
    tone: 'coral',
    illustration: 'etf-heat',
  },
  {
    id: 'high-growth',
    name: '高增长',
    path: '/high-growth',
    category: '业绩观察',
    english: 'SUSTAINED EARNINGS GROWTH',
    description: '四类报告的归母净利润与 EPS 连续增长筛选。',
    tags: ['四类财报', '持续增长', '净利润与EPS'],
    icon: TrendingUp,
    tone: 'coral',
    illustration: 'growth',
  },
  {
    id: 'dividend-selection',
    name: '高股息选股',
    path: '/dividend-selection',
    category: '价值观察',
    english: 'DIVIDEND SELECTION',
    description: '纯高股息、高股息低估值，两组各取前25只。',
    tags: ['双策略选股', '股息率', 'PE估值'],
    icon: Coins,
    tone: 'gold',
    illustration: 'dividend',
  },
  {
    id: 'microcap-breadth',
    name: '微盘新高新低',
    path: '/microcap-breadth',
    category: '风格观察',
    english: 'MICROCAP MARKET BREADTH',
    description: '微盘400的新高、新低数量及市场强度。',
    tags: ['新高新低', '微盘400', '每日采样'],
    icon: ArrowUpDown,
    tone: 'jade',
    illustration: 'breadth',
  },
  {
    id: 'earnings-gap',
    name: '净利润断层',
    path: '/earnings-gap',
    category: '业绩观察',
    english: 'EARNINGS GAP OBSERVATORY',
    description: '净利润增长、高开收阳信号及公告明细。',
    tags: ['季度选股', '公告证据', '信号日期'],
    icon: FileSearch,
    tone: 'gold',
    illustration: 'earnings',
  },
  {
    id: 'industry-momentum',
    name: '行业动量',
    path: '/industry-momentum',
    category: '市场结构',
    english: 'INDUSTRY MOMENTUM',
    description: '申万二级行业动量排名与历史趋势。',
    tags: ['行业热力图', '趋势下钻', '每日采样'],
    icon: Grid2X2,
    tone: 'coral',
    illustration: 'heatmap',
  },
  {
    id: 'market-crowding',
    name: '抱团拥挤度',
    path: '/market-crowding',
    category: '市场结构',
    english: 'MARKET CROWDING',
    description: '成交额前5%股票占全市场成交额的比例。',
    tags: ['成交额集中度', '历史曲线', 'EMA 参考'],
    icon: Activity,
    tone: 'jade',
    illustration: 'concentration',
  },
  {
    id: 'microcap-temperature',
    name: '微盘温度计',
    path: '/microcap-temperature',
    category: '风格观察',
    english: 'MICROCAP THERMOMETER',
    description: '微盘1000的估值、拥挤度及等权指数。',
    tags: ['综合温度', '等权指数', '每 5 交易日采样'],
    icon: Thermometer,
    tone: 'gold',
    illustration: 'temperature',
  },
  {
    id: 'etf-shares',
    name: 'ETF份额仪表盘',
    path: '/etf-shares',
    category: '资金观察',
    english: 'ETF SHARE OBSERVATORY',
    description: '沪深300、中证500 ETF 的份额与每日增减。',
    tags: ['ETF总份额', '沪深300', '中证500'],
    icon: ChartNoAxesCombined,
    tone: 'coral',
    illustration: 'shares',
  },
]

export const moduleGroups = [
  {
    name: '动量观察',
    description: '行业强势股集中度与 ETF / LOF 动量排名。',
    ids: ['industry-momentum', 'etf-momentum'],
    labels: ['行业动量', 'ETF动量热度'],
  },
  {
    name: '微盘观察',
    description: '微盘估值、拥挤度与新高新低数量。',
    ids: ['microcap-temperature', 'microcap-breadth'],
    labels: ['微盘温度计', '微盘新高新低'],
  },
  { name: '业绩选股', description: '财报持续增长筛选与公告后的高开收阳信号。', ids: ['high-growth', 'earnings-gap'], labels: ['高增长', '净利润断层'] },
]
export function groupForPath(path: string) {
  return moduleGroups.find((g) => g.ids.includes(path.split('/')[1]))
}
export const researchModules: ResearchModule[] = individualModules
  .filter((m) => !moduleGroups.some((g) => g.ids.slice(1).includes(m.id)))
  .map((m) => {
    const group = moduleGroups.find((g) => g.ids[0] === m.id)
    return group
      ? {
          ...m,
          name: group.name,
          tags: group.labels,
          description: group.description,
        }
      : m
  })
