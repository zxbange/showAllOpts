import { useEffect, useRef } from 'react'
import type { EChartsType } from 'echarts/core'
import { createChartInstance, type ChartRenderer } from '../lib/chartLifecycle'

/** One instance per mounted chart; subsequent option effects do not remount it. */
export function useEChart(renderer?: ChartRenderer) {
  const element = useRef<HTMLDivElement>(null)
  const chart = useRef<EChartsType | null>(null)

  useEffect(() => {
    if (!element.current) return
    const mounted = createChartInstance(element.current, renderer)
    chart.current = mounted.chart
    mounted.observe()
    return () => {
      mounted.dispose()
      chart.current = null
    }
  }, [renderer])

  return { element, chart }
}
