import { useState } from 'react'

type Metric = Record<string, string | number | null>
const names: Record<string, string> = {
  bukou: '补票龙',
  redhover: '红悬停',
  xuli: '蓄力龙',
  oversold: '超跌后止跌反弹',
}
const display = (value: string | number | null | undefined) =>
  value == null
    ? '—'
    : typeof value === 'number'
      ? value.toLocaleString('zh-CN', { maximumFractionDigits: 2 })
      : value
function Metrics({ rows, columns, label }: { rows: Metric[]; columns?: string[]; label: string }) {
  if (!rows.length) return <p className="validation-hint">暂无此期限的统计结果。</p>
  const keys = columns
    ? columns.filter((key) => rows.some((row) => key in row))
    : Object.keys(rows[0])
  return (
    <div className="validation-table" role="region" aria-label={label} tabIndex={0}>
      <table>
        <caption className="sr-only">{label}</caption>
        <thead>
          <tr>
            {keys.map((key) => (
              <th scope="col" key={key}>
                {key}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row, index) => (
            <tr key={index}>
              {keys.map((key) => (
                <td key={key} className={key === '胜率(%)' ? 'validation-rate' : undefined}>
                  {key === '策略'
                    ? (names[String(row[key])] ?? display(row[key]))
                    : display(row[key])}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
export function ValidationResults({ summary, windows }: { summary: Metric[]; windows: Metric[] }) {
  const [horizon, setHorizon] = useState(1)
  return (
    <div className="validation-results">
      <div className="validation-results-heading">
        <div>
          <h4>策略表现对比</h4>
          <p>以信号日收盘为基准，各期限独立统计有效样本。</p>
        </div>
        <div className="validation-horizons" role="group" aria-label="结果观察期限">
          {[1, 2, 3].map((day) => (
            <button
              type="button"
              key={day}
              aria-pressed={horizon === day}
              onClick={() => setHorizon(day)}
            >
              T+{day}
            </button>
          ))}
        </div>
      </div>
      <Metrics
        rows={summary.filter((row) => Number(row['后续交易日']) === horizon)}
        columns={['策略', '有效样本', '胜率(%)', '平均收益(%)', '中位收益(%)']}
        label={`T+${horizon}策略对比`}
      />
      <p className="validation-hint">
        “—”表示没有有效样本，不代表零胜率。收益未计交易费用，不代表可成交收益。
      </p>
      <details className="validation-disclosure">
        <summary>完整统计明细</summary>
        <p className="validation-hint">
          包括全部期限、上涨/持平/下跌次数、收益极值及缺失样本。表格可独立横向滚动。
        </p>
        <Metrics rows={summary} label="全部期限完整统计" />
      </details>
      <details className="validation-disclosure">
        <summary>三日窗口统计</summary>
        <p className="validation-hint">
          仅使用完整三日样本，统计任一收盘上涨率与最好、最差收盘收益均值。
        </p>
        <Metrics rows={windows} label="三日窗口统计" />
      </details>
    </div>
  )
}
