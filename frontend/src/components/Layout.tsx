import { useLayoutEffect, useRef } from 'react'
import { ArrowUpRight, ChartNoAxesCombined, ChevronRight, House, Layers3 } from 'lucide-react'
import { Link, NavLink, Outlet, useLocation } from 'react-router-dom'
import { CacheControl } from './CacheControl'
import { useAuth } from './AccountAccess'
import { accountRequest } from '../api/http'
import { researchModules, navigationModules, groupForPath } from '../lib/modules'

export function Layout() {
  const { session, signedOut } = useAuth()
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
          <span>发奋墙</span>
        </Link>
        <nav ref={navigation} className="main-nav" aria-label="主导航">
          <NavLink to="/" end aria-label="总览" title="总览">
            <House size={18} />
            <span>总览</span>
            <span className="nav-key">01</span>
          </NavLink>
          {navigationModules.map((module, index) => {
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
          {session?.user?.role === 'admin' && (
            <NavLink to="/personal-holdings" aria-label="个人持仓" title="个人持仓">
              <Layers3 size={18} />
              <span>个人持仓</span>
              <span className="nav-key">私</span>
            </NavLink>
          )}
        </nav>
        <div className="sidebar-bottom">
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
                : location.pathname === '/personal-holdings'
                  ? '个人持仓'
                  : (group?.name ??
                    researchModules.find((module) => location.pathname.startsWith(module.path))
                      ?.name ??
                    '研究模块')}
              {location.pathname.includes('/industries/') ? ' / 趋势详情' : ''}
            </span>
          </div>
          <div className="topbar-right">
            {session?.user?.role === 'admin' && <CacheControl />}
            <div className="account-menu">
              <span className="account-name">{session?.user?.username}</span>
              <Link to="/account">{session?.user?.role === 'admin' ? '用户管理' : '我的账号'}</Link>
              <button
                className="button button-quiet"
                onClick={async () => {
                  try {
                    await accountRequest('/logout', {})
                    signedOut()
                  } catch {
                    window.alert('退出失败，请重试')
                  }
                }}
              >
                退出
              </button>
            </div>
          </div>
        </header>
        <main id="main-content" className="main-content">
          <Outlet />
        </main>
        <footer className="page-footer">
          <span>
            发奋墙 <i /> 研究工作台
          </span>
          <span>
            数据来源 ·{' '}
            {location.pathname === '/commodities'
              ? '长江有色金属网（CCMN）'
              : location.pathname === '/margin-guarantee'
                ? '中证数据'
                : '聚宽采集'}{' '}
            <ArrowUpRight size={12} />
          </span>
        </footer>
      </div>
    </div>
  )
}
