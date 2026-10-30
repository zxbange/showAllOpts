import { ShortValidation } from '../components/ShortValidation'
import { ShortTermNavigation } from '../components/ShortTermNavigation'
import { PageHeader } from '../components/PageHeader'
import '../styles/short-term.css'

export function ShortValidationPage() {
  return (
    <div className="short-page page-enter">
      <PageHeader switcher={<ShortTermNavigation value="validation" />}>
        <div>
          <h1>短线胜率验证</h1>
          <p>选择数据库、策略和日期，启动历史信号统计。</p>
        </div>
      </PageHeader>
      <ShortValidation />
    </div>
  )
}
