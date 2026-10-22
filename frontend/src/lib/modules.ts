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
  question: string
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
  {
    id: 'institution-ownership',
    name: '个股机构持仓',
    path: '/institution-ownership',
    category: '筹码观察',
    english: 'INSTITUTION OWNERSHIP',
    question: '谁在持有，筹码如何变化？',
    description: '查询七类机构披露持仓与股东户数，发现季度新进的社保组合。',
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
    question: '不同资产的趋势，谁更强？',
    description: '按标的与规模筛选基金，以收盘动量热力图观察强弱，点击查看历史曲线。',
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
    question: '盈利的增长，能否穿越四类报告？',
    description: '年报与三个季度分开检查，寻找归母净利润和 EPS 持续增长的公司。',
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
    question: '分红与估值，能否同时兼顾？',
    description: '并列观察纯高股息与高股息低估值两组 Top25，查看分红、估值及盈利指标。',
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
    question: '微盘股的上涨，扩散到多少股票？',
    description: '跟踪微盘400在四个周期中的新高新低数量，观察市场宽度、强度与行情持续性。',
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
    question: '业绩的变化，如何映射到价格？',
    description: '扫描最新财报季度，追踪全部高开收阳信号，核查公告、估值与价格表现。',
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
    question: '市场的力量，正在流向哪里？',
    description: '俯瞰全行业热力分布，点击下钻，追踪每个行业的动量趋势。',
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
    question: '成交，是集中还是分散？',
    description: '观察前 5% 股票的成交额占比，沿着历史曲线寻找市场的聚与散。',
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
    question: '微盘股，处于怎样的温度？',
    description: '结合估值与交易拥挤度，观察微盘温度、等权指数和财务表现。',
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
    question: '指数基金的份额，如何变化？',
    description: '跟踪沪深300与中证500 ETF 的总份额与份额变化率，沿着时间观察基金规模变化。',
    tags: ['ETF总份额', '沪深300', '中证500'],
    icon: ChartNoAxesCombined,
    tone: 'coral',
    illustration: 'shares',
  },
]

export const moduleGroups = [
  {
    name: '动量观察',
    ids: ['industry-momentum', 'etf-momentum'],
    labels: ['行业动量', 'ETF动量热度'],
  },
  {
    name: '微盘观察',
    ids: ['microcap-temperature', 'microcap-breadth'],
    labels: ['微盘温度计', '微盘新高新低'],
  },
  { name: '业绩选股', ids: ['high-growth', 'earnings-gap'], labels: ['高增长', '净利润断层'] },
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
          description: `在同一工作区切换${group.labels.join('与')}，对照观察不同维度的数据。`,
        }
      : m
  })
