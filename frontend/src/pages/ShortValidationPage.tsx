import { useCallback } from 'react'
import { useSearchParams } from 'react-router-dom'
import { getShortTerm } from '../api/client'
import { useResource } from '../hooks/useResource'
import { ShortValidation } from '../components/ShortValidation'
import { ShortTermNavigation } from '../components/ShortTermNavigation'
import { PageHeader } from '../components/PageHeader'
import '../styles/short-term.css'
import '../styles/validation.css'

export function ShortValidationPage() {
  const [params] = useSearchParams()
  const day = params.get('day') || ''
  const loader = useCallback((signal: AbortSignal) => getShortTerm(day, 'bukou', 0, signal), [day])
  const { data, loading, error, reload } = useResource(loader)
  return (
    <div className="short-page validation-page page-enter">
      <PageHeader
        switcher={
          <ShortTermNavigation
            value="validation"
            day={day}
            counts={data?.trade_date ? data.counts : undefined}
            loading={loading}
            error={!!error}
            hasSnapshot={!!data?.trade_date}
          />
        }
      >
        <div>
          <h1>短线胜率验证</h1>
          <p>选择数据库、策略和日期，启动历史信号统计。</p>
        </div>
      </PageHeader>
      {error && (
        <p className="validation-hint" role="status">
          选股数量暂不可用。
          <button className="button button-quiet" onClick={reload}>
            重试数量
          </button>
        </p>
      )}
      <ShortValidation />
    </div>
  )
}
