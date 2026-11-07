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
    if (module?.id === 'dividend-selection') {
      return [
        ['classic', '纯高股息'],
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
      {switcher ??
        (group && groupItems && (
          <ModuleSwitch
            label={group.name + '模块切换'}
            items={groupItems}
            value={
              pathname === '/dividend-selection'
                ? `dividend-${['value', 'bank'].includes(new URLSearchParams(search).get('method') ?? '') ? new URLSearchParams(search).get('method') : 'classic'}`
                : pathname.split('/')[1]
            }
          />
        ))}
      <div className="page-heading">{children}</div>
    </header>
  )
}
