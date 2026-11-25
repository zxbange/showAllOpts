import { init } from 'echarts/core'

export type ChartRenderer = 'canvas' | 'svg'

/**
 * Own a single ECharts instance and its resize observer. Callers choose when
 * to apply options and subscribe to business-specific chart events.
 */
export function createChartInstance(element: HTMLDivElement, renderer?: ChartRenderer) {
  const chart = renderer ? init(element, undefined, { renderer }) : init(element)
  let observer: ResizeObserver | null = null
  let disposed = false

  function observe(afterResize?: () => void) {
    if (observer || disposed) return
    observer = new ResizeObserver(() => {
      // A queued observer callback from a departed view must not touch a chart
      // that has already been disposed or a replacement chart instance.
      if (disposed) return
      chart.resize()
      afterResize?.()
    })
    observer.observe(element)
  }

  function dispose(beforeDispose?: () => void) {
    if (disposed) return
    disposed = true
    observer?.disconnect()
    observer = null
    try {
      beforeDispose?.()
    } finally {
      chart.dispose()
    }
  }

  return { chart, observe, dispose }
}
