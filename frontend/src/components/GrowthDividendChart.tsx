import { useEffect, useRef, useState, type CSSProperties } from 'react'
import { init, use } from 'echarts/core'
import type { EChartsType } from 'echarts/core'
import { LineChart } from 'echarts/charts'
import {
  GridComponent,
  TooltipComponent,
  DataZoomComponent,
  LegendComponent,
  AriaComponent,
} from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'
import { chartColors } from '../lib/chartTheme'
import {
  growthDividendLines,
  growthDividendSeries,
  growthDividendSpan,
  growthDividendValue,
  type DateSpan,
  type GrowthDividendSelection,
} from '../lib/growthDividend'
import type { GrowthDividendPoint } from '../types/growthDividend'

use([
  LineChart,
  GridComponent,
  TooltipComponent,
  DataZoomComponent,
  LegendComponent,
  AriaComponent,
  CanvasRenderer,
])

export function GrowthDividendChart({
  points,
  selection,
  onRangeChange,
}: {
  points: GrowthDividendPoint[]
  selection: GrowthDividendSelection
  onRangeChange: (range: DateSpan, manual: boolean) => void
}) {
  const element = useRef<HTMLDivElement>(null)
  const chart = useRef<EChartsType | null>(null)
  const selectedSpan = useRef<DateSpan | null>(null)
  const previousSelection = useRef<GrowthDividendSelection | null>(null)
  const manualZoom = useRef(false)
  const latestPoints = useRef(points)
  const rangeCallback = useRef(onRangeChange)
  latestPoints.current = points
  rangeCallback.current = onRangeChange
  const [visible, setVisible] = useState<Record<string, boolean>>(
    Object.fromEntries(growthDividendLines.map((line) => [line.name, true])),
  )

  useEffect(() => {
    if (!element.current) return
    const instance = init(element.current)
    chart.current = instance
    const observer = new ResizeObserver(() => instance.resize())
    observer.observe(element.current)
    const handleZoom = () => {
      const zoom = (
        instance.getOption().dataZoom as Array<{
          startValue: string | number
          endValue: string | number
        }>
      )[0]
      const dateAt = (value: string | number) =>
        typeof value === 'number' ? latestPoints.current[Math.round(value)]?.date : value
      const start = dateAt(zoom.startValue)
      const end = dateAt(zoom.endValue)
      if (!start || !end) return
      selectedSpan.current = { start, end }
      manualZoom.current = true
      rangeCallback.current(selectedSpan.current, true)
    }
    instance.on('datazoom', handleZoom)
    return () => {
      observer.disconnect()
      instance.off('datazoom', handleZoom)
      instance.dispose()
      chart.current = null
    }
  }, [])

  useEffect(() => {
    const reset = previousSelection.current !== selection
    previousSelection.current = selection
    if (reset) manualZoom.current = false
    // Pin a manually selected window by date, even when a new point is appended.
    const span = growthDividendSpan(
      points,
      selection.years,
      !reset && manualZoom.current ? (selectedSpan.current ?? undefined) : selection.span,
    )
    selectedSpan.current = span
    const zoom = span ? { startValue: span.start, endValue: span.end } : {}
    const byDate = new Map(points.map((point) => [point.date, point]))
    chart.current?.setOption(
      {
        animation: false,
        aria: {
          enabled: true,
          description:
            '创业板指、中证红利、科创50以不同颜色的实线显示在左轴；创红比、科创红利比以虚线显示在右轴。可切换图例及缩放日期。',
        },
        grid: { left: 10, right: 10, top: 40, bottom: 78, containLabel: true },
        legend: { show: false, data: growthDividendLines.map((line) => line.name) },
        tooltip: {
          trigger: 'axis',
          renderMode: 'richText',
          confine: true,
          backgroundColor: chartColors.background,
          borderColor: chartColors.border,
          textStyle: { color: chartColors.text, fontSize: 12, lineHeight: 22 },
          formatter: (raw: unknown) => {
            const entries = raw as Array<{ axisValue: string }>
            const point = byDate.get(String(entries[0]?.axisValue))
            if (!point) return ''
            return [
              point.date,
              ...growthDividendLines.map(
                (line) =>
                  `${line.name}  ${growthDividendValue(point[line.key], line.ratio)}${line.ratio ? '' : ' 点'}`,
              ),
            ].join('\n')
          },
        },
        xAxis: {
          type: 'category',
          boundaryGap: false,
          data: points.map((point) => point.date),
          axisLabel: { color: chartColors.muted, hideOverlap: true, fontSize: 11 },
          axisLine: { lineStyle: { color: chartColors.border } },
          axisTick: { show: false },
        },
        yAxis: [
          {
            type: 'value',
            name: '指数点位',
            position: 'left',
            scale: true,
            nameTextStyle: { color: chartColors.muted, padding: [0, 0, 0, 30] },
            axisLabel: { color: chartColors.muted, fontSize: 11 },
            splitLine: { lineStyle: { color: chartColors.grid } },
          },
          {
            type: 'value',
            name: '比值',
            position: 'right',
            scale: true,
            nameTextStyle: { color: chartColors.muted },
            axisLabel: {
              color: chartColors.muted,
              fontSize: 11,
              formatter: (value: number) => value.toFixed(2),
            },
            splitLine: { show: false },
          },
        ],
        dataZoom: [
          {
            id: 'growth-dividend-inside',
            type: 'inside',
            xAxisIndex: 0,
            filterMode: 'filter',
            zoomOnMouseWheel: 'ctrl',
            moveOnMouseMove: true,
            ...zoom,
          },
          {
            id: 'growth-dividend-slider',
            type: 'slider',
            xAxisIndex: 0,
            filterMode: 'filter',
            bottom: 12,
            height: 26,
            borderColor: chartColors.border,
            backgroundColor: chartColors.surface,
            fillerColor: 'rgba(53,104,180,0.12)',
            textStyle: { color: chartColors.muted, fontSize: 11 },
            handleStyle: { color: '#3568b4' },
            ...zoom,
          },
        ],
        series: growthDividendSeries(points),
      },
      { notMerge: false, silent: true },
    )
    if (span) rangeCallback.current(span, manualZoom.current)
  }, [points, selection])

  useEffect(() => {
    chart.current?.setOption({ legend: { selected: visible } }, { silent: true })
  }, [visible])

  return (
    <>
      <div className="growth-dividend-legend" aria-label="指数与比值图例">
        {growthDividendLines.map((line) => (
          <button
            type="button"
            key={line.key}
            aria-label={`${visible[line.name] ? '隐藏' : '显示'}${line.name}`}
            aria-pressed={visible[line.name]}
            className={!visible[line.name] ? 'muted' : ''}
            style={{ '--line-color': line.color } as CSSProperties}
            onClick={() =>
              setVisible((current) => ({ ...current, [line.name]: !current[line.name] }))
            }
          >
            <span className={'line-sample' + (line.ratio ? ' dashed' : '')} aria-hidden="true" />
            <span>{line.name}</span>
            <small>{line.code || '右轴'}</small>
          </button>
        ))}
      </div>
      <div
        ref={element}
        className="trend-chart growth-dividend-chart"
        role="img"
        aria-label="创红比历史折线图：三条指数实线与两条比值虚线，左轴指数点位，右轴比值，支持缩放"
      />
    </>
  )
}
