import { describe, expect, it } from 'vitest'
import {
  groupForPath,
  individualModules,
  moduleGroups,
  navigationModules,
  researchModules,
} from './modules'

describe('national team ETF navigation', () => {
  const market = moduleGroups.find((group) => group.name === '市场情绪')!

  it('puts 国家队动态 first and keeps six distinct market tabs in matching label order', () => {
    expect(market.ids).toEqual([
      'etf-shares',
      'growth-dividend',
      'market-crowding',
      'market-sentiment',
      'margin-guarantee',
      'valuation-ruler',
    ])
    expect(market.labels).toEqual([
      '国家队动态',
      '创红比',
      '抱团拥挤度',
      '市场热度',
      '两融担保比例',
      '估值标尺',
    ])
    expect(
      market.ids.map((id) => individualModules.find((module) => module.id === id)?.name),
    ).toEqual(market.labels)
    expect(new Set(market.ids).size).toBe(6)
    for (const id of market.ids) expect(groupForPath('/' + id)).toBe(market)
  })

  it('routes the single overview and main navigation entry to ETF without changing group priority', () => {
    const overview = researchModules.filter((module) => module.name === '市场情绪')
    expect(overview).toHaveLength(1)
    expect(overview[0]).toMatchObject({
      id: 'etf-shares',
      path: '/etf-shares',
      category: '市场结构',
      tags: market.labels,
    })
    expect(navigationModules.slice(0, 3).map((module) => [module.name, module.path])).toEqual([
      ['市场情绪', '/etf-shares'],
      ['动量观察', '/industry-momentum'],
      ['微盘观察', '/microcap-temperature'],
    ])
    expect(researchModules).toHaveLength(7)
    expect(overview[0].searchTerms).toEqual(
      expect.arrayContaining(['国家队动态', 'ETF份额', '流通市值', '抱团拥挤度', 'EMA 参考']),
    )
  })
})

describe('cash cow performance navigation', () => {
  const group = groupForPath('/cash-cow')!

  it('puts cash cow first and keeps every existing performance tab', () => {
    expect(group.name).toBe('业绩选股')
    expect(group.ids).toEqual(['cash-cow', 'high-growth', 'earnings-gap', 'dividend-selection'])
    expect(group.labels).toEqual([
      '奶牛股',
      '高增长',
      '净利润断层',
      '纯高股息',
      '高股息低估值（非银行业）',
      '高股息低估值（银行业）',
    ])
    for (const id of group.ids) expect(groupForPath('/' + id)).toBe(group)
  })

  it('opens cash cow from the single overview and main navigation entry without moving other groups', () => {
    const overview = researchModules.filter((module) => module.name === '业绩选股')
    expect(overview).toHaveLength(1)
    expect(overview[0]).toMatchObject({
      id: 'cash-cow',
      path: '/cash-cow',
      category: '业绩观察',
      tags: group.labels,
    })
    expect(overview[0].searchTerms).toEqual(
      expect.arrayContaining(['奶牛股', '高增长', '净利润断层', '高股息选股']),
    )
    expect(researchModules).toHaveLength(7)
    expect(navigationModules.map((module) => [module.name, module.path])).toEqual([
      ['市场情绪', '/etf-shares'],
      ['动量观察', '/industry-momentum'],
      ['微盘观察', '/microcap-temperature'],
      ['大宗商品', '/commodities'],
      ['个股机构持仓', '/institution-ownership'],
      ['业绩选股', '/cash-cow'],
      ['博主动态监控', '/social-monitor'],
    ])
  })
})
