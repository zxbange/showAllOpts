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

  it('puts 国家队动态 first and keeps four distinct market tabs in matching label order', () => {
    expect(market.ids).toEqual([
      'etf-shares',
      'market-crowding',
      'market-sentiment',
      'margin-guarantee',
    ])
    expect(market.labels).toEqual(['国家队动态', '抱团拥挤度', '市场情绪跟踪', '两融担保比例'])
    expect(
      market.ids.map((id) => individualModules.find((module) => module.id === id)?.name),
    ).toEqual(market.labels)
    expect(new Set(market.ids).size).toBe(4)
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

it('keeps cash cow inside the single performance group and searchable from the overview', () => {
  const group = groupForPath('/cash-cow')!
  expect(group.name).toBe('业绩选股')
  expect(group.labels).toHaveLength(6)
  const overview = researchModules.filter((module) => module.name === '业绩选股')
  expect(overview).toHaveLength(1)
  expect(overview[0].searchTerms).toContain('奶牛股')
  expect(researchModules.some((module) => module.id === 'cash-cow')).toBe(false)
})
