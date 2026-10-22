import { useLayoutEffect, useRef } from 'react'
import {
  Activity,
  ArrowUpRight,
  ChartNoAxesCombined,
  ChevronRight,
  House,
  Layers3,
} from 'lucide-react'
import { Link, NavLink, Outlet, useLocation } from 'react-router-dom'
import { CacheControl } from './CacheControl'
import { researchModules, groupForPath } from '../lib/modules'

export function Layout() {
  const location = useLocation()
  const navigation = useRef<HTMLElement>(null)
  useLayoutEffect(() => {
    window.scrollTo(0, 0)
    const nav = navigation.current
    const active = nav?.querySelector<HTMLElement>('[aria-current="page"]')
    if (nav && active && nav.scrollWidth > nav.clientWidth) {
      nav.scrollLeft +=
        active.getBoundingClientRect().left -
        nav.getBoundingClientRect().left -
        (nav.clientWidth - active.clientWidth) / 2
    }
  }, [location.pathname])
  const group = groupForPath(location.pathname)
  const isHome = location.pathname === '/'
  return (
    <div className="app-shell">
      <a className="skip-link" href="#main-content">
        跳到主要内容
      </a>
      <aside className="sidebar">
        <Link className="brand" to="/" aria-label="发奋墙 首页">
          <span className="brand-mark">
            <ChartNoAxesCombined size={22} strokeWidth={2.4} />
          </span>
          <span>
            发奋墙<span className="brand-caption">MARKET INTELLIGENCE</span>
          </span>
        </Link>
        <div className="nav-label">
          工作空间 <span>WORKSPACE</span>
        </div>
        <nav ref={navigation} className="main-nav" aria-label="主导航">
          <NavLink to="/" end aria-label="总览" title="总览">
            <House size={18} />
            <span>总览</span>
            <span className="nav-key">01</span>
          </NavLink>
          {researchModules.map((module, index) => {
            const Icon = module.icon
            return (
              <Link
                key={module.id}
                to={module.path}
                aria-label={module.name}
                title={module.name}
                className={
                  location.pathname.startsWith(module.path) || group?.ids[0] === module.id
                    ? 'active'
                    : ''
                }
                aria-current={
                  location.pathname.startsWith(module.path) || group?.ids[0] === module.id
                    ? 'page'
                    : undefined
                }
              >
                <Icon size={18} />
                <span>{module.name}</span>
                <span className="nav-key">{String(index + 2).padStart(2, '0')}</span>
              </Link>
            )
          })}
        </nav>
        <div className="sidebar-bottom">
          <div className="sidebar-note">
            <Activity size={18} />
            <span>
              让数据形成洞察<small>每一个变化，都值得看见。</small>
            </span>
          </div>
          <div className="sidebar-footer">
            <span className="status-dot" />
            研究工作台 <span>v1.0</span>
          </div>
        </div>
      </aside>
      <div className="workspace">
        <header className="topbar">
          <div className="breadcrumb">
            <Layers3 size={16} />
            <Link to="/">研究工作台</Link>
            <ChevronRight size={13} />
            <span>
              {isHome
                ? '总览'
                : (group?.name ??
                  researchModules.find((module) => location.pathname.startsWith(module.path))
                    ?.name ??
                  '研究模块')}
              {location.pathname.includes('/industries/') ? ' / 趋势详情' : ''}
            </span>
          </div>
          <div className="topbar-right">
            <CacheControl />
            <span className="avatar">SQ</span>
          </div>
        </header>
        <main id="main-content" className="main-content">
          {group && (
            <nav className="module-switch" aria-label={`${group.name}模块切换`}>
              {group.ids.map((id, index) => (
                <NavLink key={id} to={`/${id}`}>
                  {group.labels[index]}
                </NavLink>
              ))}
            </nav>
          )}
          <Outlet />
        </main>
        <footer className="page-footer">
          <span>
            SHOWALLOPTS <i /> RESEARCH WORKSPACE
          </span>
          <span>
            数据来源 · 聚宽采集 <ArrowUpRight size={12} />
          </span>
        </footer>
      </div>
    </div>
  )
}
