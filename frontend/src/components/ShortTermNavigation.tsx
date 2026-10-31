import { ArrowUpRight, Waves, Flame, Zap, ChartNoAxesCombined } from 'lucide-react'
import { ModuleSwitch } from './ModuleSwitch'
import type { ShortMethod } from '../types/shortTerm'

export const methods = [
  {
    id: 'bukou' as ShortMethod,
    name: '补票龙',
    subtitle: '趋势中的回踩再起',
    icon: Waves,
    note: 'BBI向上 · 长RSV强势 · 短RSV回落再上行 · DIF为正',
  },
  {
    id: 'redhover' as ShortMethod,
    name: '红悬停龙',
    subtitle: '涨停后的高位承接',
    icon: Flame,
    note: '两日前涨停 · 后两日低点不低于该涨停价 · 当日未涨停',
  },
  {
    id: 'xuli' as ShortMethod,
    name: '蓄力龙',
    subtitle: '缩量整理后的突破',
    icon: Zap,
    note: '阳—阴—阳 · 缩量回撤后放量 · MA10 > MA20 > MA30 > MA60',
  },
  {
    id: 'oversold' as ShortMethod,
    name: '超跌后止跌反弹',
    subtitle: '超跌后的收盘确认',
    icon: ArrowUpRight,
    note: '信号前3日跌幅≥8% · 收盘超过前日最高价且收阳 · 原始量≥此前5日均量 · 沪深A股上市≥365天 · 成交额≥5000万元 · 60日完整价格历史',
  },
]

export function ShortTermNavigation({
  value,
  counts,
  day = '',
  loading = false,
  error = false,
  hasSnapshot = true,
}: {
  value: ShortMethod | 'validation'
  counts?: Partial<Record<ShortMethod, number>>
  day?: string
  loading?: boolean
  error?: boolean
  hasSnapshot?: boolean
}) {
  return (
    <ModuleSwitch
      label="短线选股战法"
      value={value}
      items={[
        ...methods.map((m) => ({
          id: m.id,
          name: m.name,
          description: m.subtitle,
          icon: m.icon,
          href: '/short-term?' + new URLSearchParams({ method: m.id, ...(day ? { day } : {}) }),
          count: (
            <span
              title={
                loading
                  ? '数量加载中'
                  : error
                    ? '数量加载失败'
                    : !hasSnapshot
                      ? '暂无选股快照'
                      : counts?.[m.id] == null
                        ? '此快照尚无该策略数量'
                        : `选股快照：${day || '最新可用日'}`
              }
            >
              {loading || error || !hasSnapshot ? '—' : (counts?.[m.id] ?? '—')}
            </span>
          ),
        })),
        {
          id: 'validation',
          name: '胜率验证',
          description: '选择策略与历史区间',
          icon: ChartNoAxesCombined,
          href: '/short-term/validation' + (day ? '?' + new URLSearchParams({ day }) : ''),
        },
      ]}
    />
  )
}
