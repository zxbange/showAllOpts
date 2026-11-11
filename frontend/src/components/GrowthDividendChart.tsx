import { useEffect, useMemo, useRef, useState, type CSSProperties } from 'react'
import { init, use } from 'echarts/core'
import type { EChartsType } from 'echarts/core'
import { LineChart } from 'echarts/charts'
import {
  GridComponent,
  TooltipComponent,
  DataZoomComponent,
  LegendComponent,
  MarkLineComponent,
  AriaComponent,
} from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'
import { chartColors } from '../lib/chartTheme'
import {
  growthDividendLines,
  growthDividendBenchmarks,
  growthDividendSeries,
  growthDividendRatioExtent,
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
  MarkLineComponent,
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
  // Zoom and legend changes never change the historical percentile sample.
  const benchmarks = useMemo(() => growthDividendBenchmarks(points), [points])
  const shownBenchmarks = benchmarks.filter((benchmark) => visible[benchmark.name])

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
    const referenceValues = benchmarks
      .filter((benchmark) => visible[benchmark.name])
      .flatMap((benchmark) => benchmark.levels.map((level) => level.value))
    const ratioExtent = (range: { min: number; max: number }) =>
      growthDividendRatioExtent(range, referenceValues)
    chart.current?.setOption(
      {
        animation: false,
        aria: {
          enabled: true,
          description:
            '创业板指、中证红利、科创50以不同颜色的实线显示在左轴；创红比、科创红利比以虚线显示在右轴。加粗水平实线为各比值完整历史的20%（P20）和80%（P80）分位基准，图中标注比值名称、分位和数值，缩放不改变基准。可切换图例及缩放日期。',
        },
        grid: { left: 10, right: 10, top: 40, bottom: 78, containLabel: true },
        legend: {
          show: false,
          data: growthDividendLines.map((line) => line.name),
          selected: visible,
        },
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
            // A fixed historical benchmark can be outside the visible data's
            // local range. Keep visible ratios' benchmarks inside the axis.
            min: (range: { min: number; max: number }) => ratioExtent(range)?.min,
            max: (range: { min: number; max: number }) => ratioExtent(range)?.max,
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
        series: growthDividendSeries(points, benchmarks),
      },
      { notMerge: false, silent: true },
    )
    // Only a real datazoom event may clear unapplied date input. Re-rendering
    // after a legend toggle or history refresh simply reports the current view.
    if (span) rangeCallback.current(span, false)
  }, [points, selection, benchmarks, visible])

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
        aria-label="创红比历史折线图：三条指数实线、两条比值虚线和历史20%与80%分位基准实线及标识，左轴指数点位，右轴比值，支持缩放"
      />
      <div className="growth-dividend-benchmarks" aria-label="历史分位基准">
        {shownBenchmarks.flatMap((benchmark) =>
          benchmark.levels.map((level) => (
            <span
              className="growth-dividend-benchmark"
              key={`${benchmark.key}-${level.percentile}`}
              style={{ '--line-color': benchmark.color } as CSSProperties}
            >
              <span className="line-sample" aria-hidden="true" />
              {benchmark.name} {level.percentile} {growthDividendValue(level.value, true)}
            </span>
          )),
        )}
      </div>
      <p className="growth-dividend-benchmark-note">
        加粗水平实线为历史20%（P20）与80%（P80）分位，图中标注名称、分位及数值：创红比从2018-01-01起，科创红利比从首个有效数据日起。
        按已加载完整历史计算，缩放不改变基准，新增或补齐数据后更新。
        {shownBenchmarks.some((benchmark) => benchmark.levels.length === 1) &&
          '相同分位值合并为一条基准线。'}
      </p>
    </>
  )
}
