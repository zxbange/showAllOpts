import { useLayoutEffect } from 'react'
import {
  Activity,
  ArrowUpRight,
  ChartNoAxesCombined,
  ChevronRight,
  Grid2X2,
  House,
  Layers3,
  Thermometer,
} from 'lucide-react'
import { Link, NavLink, Outlet, useLocation } from 'react-router-dom'

export function Layout() {
  const location = useLocation()
  useLayoutEffect(() => {
    window.scrollTo(0, 0)
  }, [location.pathname])
  const isHome = location.pathname === '/'
  return (
    <div className="app-shell">
      <a className="skip-link" href="#main-content">
        跳到主要内容
      </a>
      <aside className="sidebar">
        <Link className="brand" to="/" aria-label="ShowAllOpts 首页">
          <span className="brand-mark">
            <ChartNoAxesCombined size={22} strokeWidth={2.4} />
          </span>
          <span>
            ShowAllOpts<span className="brand-caption">MARKET INTELLIGENCE</span>
          </span>
        </Link>
        <div className="nav-label">
          工作空间 <span>WORKSPACE</span>
        </div>
        <nav className="main-nav" aria-label="主导航">
          <NavLink to="/" end>
            <House size={18} />
            <span>总览</span>
            <span className="nav-key">01</span>
          </NavLink>
          <NavLink to="/industry-momentum">
            <Grid2X2 size={18} />
            <span>行业动量</span>
            <span className="nav-key">02</span>
          </NavLink>
          <NavLink to="/market-crowding">
            <Activity size={18} />
            <span>抱团拥挤度</span>
            <span className="nav-key">03</span>
          </NavLink>
          <NavLink to="/microcap-temperature">
            <Thermometer size={18} />
            <span>微盘温度计</span>
            <span className="nav-key">04</span>
          </NavLink>
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
                : location.pathname.startsWith('/microcap-temperature')
                  ? '微盘温度计'
                  : location.pathname.startsWith('/market-crowding')
                    ? '抱团拥挤度'
                    : '行业动量'}
              {location.pathname.includes('/industries/') ? ' / 趋势详情' : ''}
            </span>
          </div>
          <div className="topbar-right">
            <span className="desktop-only">把握趋势 · 看见全局</span>
            <span className="avatar">SQ</span>
          </div>
        </header>
        <main id="main-content" className="main-content">
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
