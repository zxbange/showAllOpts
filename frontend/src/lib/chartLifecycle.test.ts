import { init, type EChartsType } from 'echarts/core'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createChartInstance } from './chartLifecycle'

vi.mock('echarts/core', () => ({ init: vi.fn() }))

class TestResizeObserver {
  static instances: TestResizeObserver[] = []
  observe = vi.fn()
  unobserve = vi.fn()
  disconnect = vi.fn()

  constructor(private readonly callback: ResizeObserverCallback) {
    TestResizeObserver.instances.push(this)
  }

  deliver() {
    this.callback([], this)
  }
}

function chartMock() {
  const chart = { resize: vi.fn(), dispose: vi.fn() }
  vi.mocked(init).mockReturnValueOnce(chart as unknown as EChartsType)
  return chart
}

describe('shared ECharts instance and resize lifetime', () => {
  beforeEach(() => {
    vi.resetAllMocks()
    TestResizeObserver.instances = []
    vi.stubGlobal('ResizeObserver', TestResizeObserver)
  })
  afterEach(() => vi.unstubAllGlobals())

  it('keeps the original init arguments and observes only when the owner is ready', () => {
    const element = {} as HTMLDivElement
    const chart = chartMock()
    const mounted = createChartInstance(element)
    expect(init).toHaveBeenCalledExactlyOnceWith(element)
    expect(mounted.chart).toBe(chart)
    expect(TestResizeObserver.instances).toHaveLength(0)
    mounted.observe()
    mounted.observe()
    const observer = TestResizeObserver.instances[0]
    expect(TestResizeObserver.instances).toHaveLength(1)
    expect(observer.observe).toHaveBeenCalledExactlyOnceWith(element)
    observer.deliver()
    expect(chart.resize).toHaveBeenCalledTimes(1)
    mounted.dispose()
    mounted.dispose()
    expect(observer.disconnect).toHaveBeenCalledTimes(1)
    expect(chart.dispose).toHaveBeenCalledTimes(1)
  })

  it('preserves explicit canvas renderer and custom annotation/cleanup ordering', () => {
    const events: string[] = []
    const element = {} as HTMLDivElement
    const chart = chartMock()
    chart.resize.mockImplementation(() => {
      events.push('resize')
    })
    chart.dispose.mockImplementation(() => {
      events.push('dispose')
    })
    const mounted = createChartInstance(element, 'canvas')
    expect(init).toHaveBeenCalledExactlyOnceWith(element, undefined, { renderer: 'canvas' })
    mounted.observe(() => events.push('annotation'))
    const observer = TestResizeObserver.instances[0]
    observer.disconnect.mockImplementation(() => {
      events.push('disconnect')
    })
    observer.deliver()
    mounted.dispose(() => events.push('unsubscribe datazoom'))
    expect(events).toEqual([
      'resize',
      'annotation',
      'disconnect',
      'unsubscribe datazoom',
      'dispose',
    ])
    observer.deliver()
    expect(events).toHaveLength(5)
  })

  it('a delayed observer response cannot resize a disposed or replacement instance', () => {
    const element = {} as HTMLDivElement
    const first = chartMock()
    const mounted = createChartInstance(element)
    mounted.observe()
    const oldObserver = TestResizeObserver.instances[0]
    mounted.dispose()

    const second = chartMock()
    const replacement = createChartInstance(element)
    replacement.observe()
    oldObserver.deliver()
    expect(first.resize).not.toHaveBeenCalled()
    expect(second.resize).not.toHaveBeenCalled()
    TestResizeObserver.instances[1].deliver()
    expect(second.resize).toHaveBeenCalledTimes(1)
    replacement.dispose()
    expect(first.dispose).toHaveBeenCalledTimes(1)
    expect(second.dispose).toHaveBeenCalledTimes(1)
  })

  it('cleans up an instance even when disposed before observing or a listener cleanup fails', () => {
    const chart = chartMock()
    const mounted = createChartInstance({} as HTMLDivElement)
    expect(() =>
      mounted.dispose(() => {
        throw new Error('listener cleanup failed')
      }),
    ).toThrow('listener cleanup failed')
    expect(chart.dispose).toHaveBeenCalledTimes(1)
    mounted.observe()
    mounted.dispose()
    expect(TestResizeObserver.instances).toHaveLength(0)
    expect(chart.dispose).toHaveBeenCalledTimes(1)
  })
})
