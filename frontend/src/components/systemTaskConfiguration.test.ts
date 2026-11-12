import { createElement } from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'
import { SystemTaskConfiguration } from './SystemTaskConfiguration'

describe('system task controls', () => {
  const render = () =>
    renderToStaticMarkup(
      createElement(SystemTaskConfiguration, {
        schedules: {
          market: { enabled: true, times: ['00:01'], telegram_notify: false },
          commodities: { enabled: true, times: ['11:00', '18:00'] },
          holdings: { enabled: false, start: '09:25', end: '15:10', interval_seconds: 15 },
          cash_cow: { enabled: true, times: ['21:00'], frequency: 'month_end' },
          social_cubes: { enabled: true, start: '09:00', end: '15:00' },
          social: {
            enabled: false,
            start: '07:00',
            end: '24:00',
            min_interval_minutes: 10,
            max_interval_minutes: 16,
          },
        },
        onChange: () => {},
      }),
    )
  it('separates frequency, time, automatic execution and notifications', () => {
    const html = render()
    for (const title of ['任务', '自动运行', '频率', '运行时间', 'Telegram 通知']) {
      expect(html).toContain(`role="columnheader">${title}`)
    }
    expect(html).toContain('role="switch"')
    expect(html).toContain('通知开关只控制推送')
    expect(html).toContain('修改后保存生效')
    expect(html).toContain('每月最后一天')
    expect(html).toContain('周一至周五')
    expect(html).toContain('每天轮询')
    expect(html).toContain('单独配置')
  })
  it('uses literal HH:mm rather than locale-dependent time inputs', () => {
    const html = render()
    expect(html).not.toContain('type="time"')
    for (const value of ['00:01', '11:00', '18:00', '24:00']) {
      expect(html).toContain(`value="${value}"`)
    }
    expect(html).toContain('HH:mm')
    expect(html).toContain('pattern="(?:[01][0-9]|2[0-3]):[0-5][0-9]|24:00"')
  })
})
