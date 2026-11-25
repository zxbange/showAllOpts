/** Controlled history range buttons. Each page keeps its own range semantics. */
export function HistoryRangeSwitch<Value extends string | number>({
  options,
  value,
  onChange,
  label,
}: {
  options: readonly { value: Value; label: string }[]
  value: Value | null
  onChange: (value: Value) => void
  label: string
}) {
  return (
    <div className="range-switch" aria-label={label} role="group">
      {options.map((option) => (
        <button
          type="button"
          key={option.value}
          aria-pressed={value === option.value}
          className={value === option.value ? 'active' : ''}
          onClick={() => onChange(option.value)}
        >
          {option.label}
        </button>
      ))}
    </div>
  )
}

export const standardHistoryRanges = [
  { value: 30, label: '近 1 月' },
  { value: 90, label: '近 3 月' },
  { value: 180, label: '近半年' },
  { value: 365, label: '近 1 年' },
  { value: 0, label: '全部' },
]

export const compactHistoryRanges = standardHistoryRanges.map((range) => ({
  ...range,
  label: range.label.replaceAll(' ', ''),
}))
