import { useEffect, useId, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { Info } from 'lucide-react'
import '../styles/dividend-yield-history.css'

import type { AnnualYield } from '../types/dividend'

export function DividendYieldHistory({ history }: { history?: AnnualYield[] | null }) {
  const id = useId()
  const anchor = useRef<HTMLButtonElement>(null)
  const popup = useRef<HTMLDivElement>(null)
  const pinned = useRef(false)
  const [position, setPosition] = useState<{ left: number; top: number } | null>(null)
  const open = () => {
    const rect = anchor.current?.getBoundingClientRect()
    if (rect)
      setPosition({
        left: Math.max(8, Math.min(rect.left, window.innerWidth - 248)),
        top: rect.bottom + 188 < window.innerHeight ? rect.bottom + 8 : Math.max(8, rect.top - 188),
      })
  }
  useEffect(() => {
    if (!position) return
    const close = () => {
      pinned.current = false
      setPosition(null)
    }
    const outside = (event: PointerEvent) => {
      if (
        !anchor.current?.contains(event.target as Node) &&
        !popup.current?.contains(event.target as Node)
      )
        close()
    }
    const escape = (event: KeyboardEvent) => {
      if (event.key === 'Escape') close()
    }
    const scroll = () => {
      const rect = anchor.current?.getBoundingClientRect()
      if (rect && rect.bottom > 0 && rect.top < window.innerHeight) open()
      else close()
    }
    window.addEventListener('pointerdown', outside)
    window.addEventListener('keydown', escape)
    window.addEventListener('resize', close)
    window.addEventListener('scroll', scroll, true)
    return () => {
      window.removeEventListener('pointerdown', outside)
      window.removeEventListener('keydown', escape)
      window.removeEventListener('resize', close)
      window.removeEventListener('scroll', scroll, true)
    }
  }, [position])
  return (
    <>
      <button
        ref={anchor}
        type="button"
        className="dividend-history-trigger"
        aria-label="查看近三年股息率"
        aria-describedby={position ? id : undefined}
        onMouseEnter={open}
        onFocus={open}
        onBlur={() => {
          if (!pinned.current) setPosition(null)
        }}
        onMouseLeave={(event) => {
          if (
            !pinned.current &&
            (!(event.relatedTarget instanceof Node) ||
              !popup.current?.contains(event.relatedTarget))
          )
            setPosition(null)
        }}
        onClick={(event) => {
          event.preventDefault()
          event.stopPropagation()
          pinned.current = true
          open()
        }}
      >
        <Info size={15} />
      </button>
      {position &&
        createPortal(
          <div
            ref={popup}
            id={id}
            role="tooltip"
            className="dividend-history-popup"
            style={position}
            onMouseLeave={() => {
              if (!pinned.current) setPosition(null)
            }}
          >
            <strong>近三年股息率</strong>
            {history?.length ? (
              <dl>
                {history.map((item) => (
                  <div key={item.fiscal_year}>
                    <dt>{item.fiscal_year} 财年</dt>
                    <dd>{item.yield_pct == null ? '—' : `${item.yield_pct.toFixed(2)}%`}</dd>
                  </div>
                ))}
              </dl>
            ) : (
              <p>待新版采集更新</p>
            )}
            <small>各财年已实施分红 ÷ 当前价格或市值</small>
          </div>,
          document.body,
        )}
    </>
  )
}
