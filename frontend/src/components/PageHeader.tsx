import type { ReactNode } from 'react'
import { useLocation } from 'react-router-dom'
import { groupForPath, individualModules } from '../lib/modules'
import { ModuleSwitch } from './ModuleSwitch'
import '../styles/page-header.css'

/** 所有业务页共用：标题与操作区 → 模块卡片 → 页面内容。 */
export function PageHeader({ children, switcher }: { children: ReactNode; switcher?: ReactNode }) {
  const { pathname } = useLocation()
  const group = groupForPath(pathname)
  const groupItems = group?.ids.flatMap((id) => {
    const module = individualModules.find((item) => item.id === id)
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
      <div className="page-heading">{children}</div>
      {switcher ??
        (group && groupItems && (
          <ModuleSwitch label={group.name + '模块切换'} items={groupItems} />
        ))}
    </header>
  )
}
