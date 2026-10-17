import {
  ArrowDownRight,
  ArrowRight,
  ArrowUpRight,
  CircleDot,
  Grid2X2,
  Layers3,
  MoveUpRight,
  Radar,
  Sparkles,
} from 'lucide-react'
import { Link } from 'react-router-dom'
import { getModules, getSnapshot } from '../api/client'
import { useResource } from '../hooks/useResource'
import { displayName, formatDate, formatScore, heatColor } from '../lib/format'

export function HomePage() {
  const snapshot = useResource(getSnapshot)
  const modules = useResource(getModules)
  const count = snapshot.data?.summary.industry_count
  const top = snapshot.data?.industries[0]
  return (
    <div className="home-page page-enter">
      <div className="section-eyebrow">
        <span className="tiny-line" /> YOUR MARKET, IN PERSPECTIVE
      </div>
      <section className="home-hero">
        <div className="hero-copy">
          <span className="pill">
            <span className="status-dot" />
            量化视角 · 全局洞察
          </span>
          <h1>
            看见市场的<span>温度与方向。</span>
          </h1>
          <p>
            把分散的市场信号，汇聚成清晰的研究视野。
            <br />
            从行业强弱出发，发现趋势正在发生的地方。
          </p>
          <Link to="/industry-momentum" className="button button-primary hero-button">
            探索行业动量
            <ArrowUpRight size={18} />
          </Link>
          <div className="hero-footnote">
            <CircleDot size={13} /> 基于真实入库数据，连接观察与研究。
          </div>
        </div>
        <div className="hero-art" aria-hidden="true">
          <div className="orbital orbit-one" />
          <div className="orbital orbit-two" />
          <div className="art-cross cross-a">+</div>
          <div className="art-cross cross-b">+</div>
          <div className="art-caption">
            MARKET PULSE<span>行业信号 · 全景视野</span>
          </div>
          <div className="signal-bars">
            {[25, 40, 34, 58, 45, 72, 63, 91, 78, 100, 88, 114].map((height, i) => (
              <span key={i} style={{ height: `${height}px`, animationDelay: `${i * 80}ms` }} />
            ))}
          </div>
          <div className="art-axis">
            <span>OBSERVE</span>
            <span>UNDERSTAND</span>
            <MoveUpRight size={16} />
          </div>
          <span className="floating-label">
            <Sparkles size={13} /> INSIGHT STARTS HERE
          </span>
        </div>
      </section>
      <div className="home-data-strip">
        <div>
          <span className="strip-icon">
            <Layers3 size={19} />
          </span>
          <span>
            研究模块
            <strong>
              {modules.data?.modules.length ?? 2}
              <small>个模块已就绪</small>
            </strong>
          </span>
        </div>
        <div>
          <span className="strip-icon">
            <Grid2X2 size={19} />
          </span>
          <span>
            覆盖行业
            <strong>
              {count ?? '—'}
              <small>申万二级</small>
            </strong>
          </span>
        </div>
        <div>
          <span className="strip-icon">
            <Radar size={19} />
          </span>
          <span>
            最新数据
            <strong className="date-value">
              {formatDate(snapshot.data?.trade_date)}
              <small>{snapshot.error ? '数据暂不可用' : '按交易日更新'}</small>
            </strong>
          </span>
        </div>
      </div>
      <section className="modules-section">
        <div className="section-heading">
          <div>
            <span className="section-eyebrow">RESEARCH MODULES</span>
            <h2>你的研究，从这里开始</h2>
          </div>
          <span className="subtle-tag">
            持续拓展的研究空间
            <ArrowDownRight size={15} />
          </span>
        </div>
        <div className="module-layout">
          <Link to="/industry-momentum" className="module-card">
            <div className="module-copy">
              <div className="module-top">
                <span className="module-icon">
                  <Grid2X2 size={23} />
                </span>
                <span className="pill pill-small">
                  <span className="status-dot" />
                  已开放
                </span>
              </div>
              <span className="section-eyebrow">INDUSTRY MOMENTUM</span>
              <h3>行业动量</h3>
              <p>
                一张热力图，看清行业强弱。
                <br />
                深入每个行业，追踪近一年的动量变化。
              </p>
              <div className="module-tags">
                <span>全行业热力图</span>
                <span>历史趋势</span>
                <span>交互下钻</span>
              </div>
              <div className="module-link">
                进入模块
                <ArrowRight size={18} />
              </div>
            </div>
            <div className="module-preview">
              <div className="preview-head">
                <span>SECTOR HEAT</span>
                <ArrowUpRight size={15} />
              </div>
              <div className="mini-heatmap">
                {Array.from({ length: 35 }, (_, index) => {
                  const score = snapshot.data?.industries[index]?.momentum
                  const max = snapshot.data?.summary.max_momentum ?? 12
                  const color = heatColor(score ?? (35 - index) / 3, max)
                  return <span key={index} style={{ background: color.background }} />
                })}
              </div>
              <div className="preview-caption">
                {top ? (
                  <>
                    当前领跑 · {displayName(top.name)} <b>{formatScore(top.momentum)}</b>
                  </>
                ) : (
                  <>
                    热力分布示意 <b>↗</b>
                  </>
                )}
              </div>
            </div>
          </Link>
          <Link to="/market-crowding" className="next-module crowding-module">
            <div className="next-module-art">
              <Radar size={40} strokeWidth={1} />
              <span />
              <span />
            </div>
            <span className="section-eyebrow">MARKET CROWDING</span>
            <h3>抱团拥挤度</h3>
            <p>
              前 5% 股票，占据多少成交？
              <br />
              沿着历史曲线，观察市场的集中与分散。
            </p>
            <span className="module-link">
              进入模块 <ArrowRight size={18} />
            </span>
          </Link>
        </div>
      </section>
    </div>
  )
}
