import { formatDataUpdatedAt } from '../lib/dataUpdatedAt'
import { Children, type ReactNode } from 'react'
import { CalendarDays } from 'lucide-react'
import { useLocation } from 'react-router-dom'
import { groupForPath, individualModules } from '../lib/modules'
import { PageHeaderActionSlot } from './PageHeaderActions'
import { ModuleSwitch } from './ModuleSwitch'
import '../styles/page-header.css'

/** 所有业务页共用：标题与操作区 → 模块切换 → 页面内容。 */
export function PageHeader({
  children,
  switcher,
  dataUpdatedAt,
  dataTimeNaiveBeijing = false,
}: {
  children: ReactNode
  switcher?: ReactNode
  dataUpdatedAt?: string | null
  dataTimeNaiveBeijing?: boolean
}) {
  const { pathname, search } = useLocation()
  const group = groupForPath(pathname)
  const showDataTime = Boolean(
    group ||
    pathname === '/personal-holdings' ||
    individualModules.some((item) => item.path === pathname),
  )
  const [heading, ...headingActions] = Children.toArray(children)
  const groupItems = group?.ids.flatMap((id) => {
    const module = individualModules.find((item) => item.id === id)
    if (module?.id === 'dividend-selection') {
      return [
        ['value', '高股息低估值（非银行业）'],
        ['bank', '高股息低估值（银行业）'],
      ].map(([method, name]) => ({
        id: `dividend-${method}`,
        name,
        description: name,
        icon: module.icon,
        href: `/dividend-selection?method=${method}`,
      }))
    }
    return module
      ? [
          {
            id,
            name: module.name,
            description: module.tags.slice(0, 2).join(' · '),
            icon: module.icon,
            href: module.path,
          },
        ]
      : []
  })
  return (
    <header className="page-header">
      <div className={`page-heading${showDataTime ? ' page-heading-with-time' : ''}`}>
        {showDataTime ? (
          <>
            {heading}
            <div className="heading-actions page-heading-right">
              <span className="date-badge page-data-updated" aria-label="数据更新时间">
                <CalendarDays size={15} aria-hidden="true" />
                <span className="page-data-updated-text">
                  <span className="page-data-updated-label">数据更新时间</span>
                  <span className="page-data-updated-value">
                    {formatDataUpdatedAt(dataUpdatedAt, dataTimeNaiveBeijing)}
                  </span>
                </span>
              </span>
              {headingActions}
            </div>
          </>
        ) : (
          children
        )}
      </div>
      <div className="page-header-toolbar">
        {switcher ??
          (group && groupItems && (
            <ModuleSwitch
              label={group.name + '模块切换'}
              items={groupItems}
              value={
                pathname === '/dividend-selection'
                  ? `dividend-${new URLSearchParams(search).get('method') === 'bank' ? 'bank' : 'value'}`
                  : pathname.split('/')[1]
              }
            />
          ))}
        <PageHeaderActionSlot />
      </div>
    </header>
  )
}
