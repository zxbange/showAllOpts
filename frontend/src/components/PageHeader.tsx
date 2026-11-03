import type { ReactNode } from 'react'
import { useLocation } from 'react-router-dom'
import { groupForPath, individualModules } from '../lib/modules'
import { ModuleSwitch } from './ModuleSwitch'
import '../styles/page-header.css'

/** 所有业务页共用：顶部模块书签 → 标题与操作区 → 页面内容。 */
export function PageHeader({ children, switcher }: { children: ReactNode; switcher?: ReactNode }) {
  const { pathname, search } = useLocation()
  const group = groupForPath(pathname)
  const groupItems = group?.ids.flatMap((id) => {
    const module = individualModules.find((item) => item.id === id)
    if (module?.id === 'etf-shares') {
      return ['hs300', 'zz500'].map((index) => ({
        id: `etf-shares-${index}`,
        name: index === 'hs300' ? '沪深300 ETF' : '中证500 ETF',
        description: 'ETF 总份额与每日增减',
        icon: module.icon,
        href: `/etf-shares?group=${index}`,
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
      {switcher ??
        (group && groupItems && (
          <ModuleSwitch
            label={group.name + '模块切换'}
            items={groupItems}
            value={
              pathname === '/etf-shares'
                ? `etf-shares-${new URLSearchParams(search).get('group') === 'zz500' ? 'zz500' : 'hs300'}`
                : pathname.split('/')[1]
            }
          />
        ))}
      <div className="page-heading">{children}</div>
    </header>
  )
}
