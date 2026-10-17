import { Activity, ChartNoAxesCombined, Grid2X2, Thermometer } from 'lucide-react'
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
  illustration: 'heatmap' | 'concentration' | 'temperature' | 'shares'
}

export const researchModules: ResearchModule[] = [
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
