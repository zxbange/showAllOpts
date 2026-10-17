import { useState } from 'react'
import { ArrowDown, ArrowUpRight, Compass, Layers3, Search, Sparkles, X } from 'lucide-react'
import { Link } from 'react-router-dom'
import { researchModules } from '../lib/modules'
import type { ResearchModule } from '../lib/modules'
import '../styles/overview.css'

function ModuleIllustration({ kind }: { kind: ResearchModule['illustration'] }) {
  return (
    <div className={`module-illustration illustration-${kind}`} aria-hidden="true">
      {kind === 'heatmap' ? (
        <div className="illustration-tiles">
          {Array.from({ length: 24 }, (_, i) => (
            <span key={i} style={{ opacity: 1 - Math.floor(i / 4) * 0.13 }} />
          ))}
        </div>
      ) : kind === 'shares' ? (
        <div className="illustration-shares">
          {[38, 55, 45, 72, 61, 88, 100].map((height, i) => (
            <span key={i} style={{ height: `${height}%` }} />
          ))}
        </div>
      ) : kind === 'concentration' ? (
        <div className="illustration-rings">
          <span />
          <span />
          <span />
          <i />
        </div>
      ) : (
        <div className="illustration-gauge">
          <div />
          <span />
          <i />
        </div>
      )}
      <span className="illustration-caption">
        {kind === 'heatmap'
          ? '行业强弱'
          : kind === 'concentration'
            ? '成交集中'
            : kind === 'shares'
              ? '份额变化'
              : '估值与交易'}{' '}
        · 视觉示意
      </span>
    </div>
  )
}

export function HomePage() {
  const [search, setSearch] = useState('')
  const [category, setCategory] = useState('全部模块')
  const categories = ['全部模块', ...new Set(researchModules.map((module) => module.category))]
  const visible = researchModules.filter(
    (module) =>
      (category === '全部模块' || module.category === category) &&
      [module.name, module.english, module.description, ...module.tags]
        .join(' ')
        .toLowerCase()
        .includes(search.trim().toLowerCase()),
  )
  return (
    <div className="overview-page page-enter">
      <div className="overview-intro">
        <span className="section-eyebrow">
          <span className="tiny-line" /> THE RESEARCH COLLECTION
        </span>
        <span>多一个视角，多一份洞察</span>
      </div>
      <section className="overview-hero">
        <div className="overview-hero-copy">
          <span className="overview-kicker">
            <Sparkles size={14} /> 欢迎回到你的市场研究工作台
          </span>
          <h1>
            把市场的万千变化，
            <br />
            <em>看得更清楚。</em>
          </h1>
          <p>
            从行业强弱、成交集中到微盘温度。
            <br />
            让每一个研究视角，都有自己的位置。
          </p>
          <a className="button button-primary overview-browse" href="#research-modules">
            浏览全部模块 <ArrowDown size={16} />
          </a>
          <div className="overview-hero-meta">
            <Layers3 size={14} />
            <strong>{researchModules.length}</strong> 个研究模块
            <span />
            独立视角 · 自由探索
          </div>
        </div>
        <div className="research-orbit" aria-hidden="true">
          <div className="orbit-disc" />
          <div className="orbit-path" />
          <div className="orbit-path orbit-path-inner" />
          <div className="orbit-center">
            <Compass size={34} strokeWidth={1.25} />
            <strong>发现 · 洞察</strong>
            <small>MARKET PERSPECTIVES</small>
          </div>
          <div className="orbit-label orbit-label-momentum">
            <span>
              <Layers3 size={20} />
            </span>
            <div>
              行业动量<small>看方向</small>
            </div>
          </div>
          <div className="orbit-label orbit-label-crowding">
            <span>
              <Sparkles size={20} />
            </span>
            <div>
              抱团拥挤度<small>看集中</small>
            </div>
          </div>
          <div className="orbit-label orbit-label-microcap">
            <span>
              <Compass size={20} />
            </span>
            <div>
              微盘温度计<small>看温度</small>
            </div>
          </div>
          <i className="orbit-spark spark-one" />
          <i className="orbit-spark spark-two" />
          <i className="orbit-spark spark-three" />
          <span className="orbit-footnote">CONNECT THE SIGNALS. FIND YOUR PERSPECTIVE.</span>
        </div>
      </section>
      <section
        className="research-directory"
        id="research-modules"
        aria-labelledby="directory-title"
      >
        <div className="directory-heading">
          <div>
            <span className="section-eyebrow">EXPLORE YOUR WORKSPACE</span>
            <h2 id="directory-title">
              选择一个视角，开始研究
              <span>{researchModules.length.toString().padStart(2, '0')}</span>
            </h2>
          </div>
          <label className="directory-search">
            <Search size={17} />
            <input
              aria-label="搜索研究模块"
              placeholder="搜索模块、指标或关键词"
              value={search}
              onChange={(event) => setSearch(event.target.value)}
            />
            {search && (
              <button aria-label="清空模块搜索" onClick={() => setSearch('')}>
                <X size={15} />
              </button>
            )}
          </label>
        </div>
        <div className="directory-tools">
          <div className="directory-filters" aria-label="模块分类">
            {categories.map((item) => (
              <button
                key={item}
                className={category === item ? 'active' : ''}
                aria-pressed={category === item}
                onClick={() => setCategory(item)}
              >
                {item}
                <span>
                  {item === '全部模块'
                    ? researchModules.length
                    : researchModules.filter((module) => module.category === item).length}
                </span>
              </button>
            ))}
          </div>
          <span className="directory-count" role="status">
            找到 {visible.length} 个模块
          </span>
        </div>
        <div className="research-grid">
          {visible.map((module) => {
            const Icon = module.icon
            return (
              <Link
                key={module.id}
                to={module.path}
                className={`research-card tone-${module.tone}`}
                aria-label={`进入${module.name}`}
              >
                <div className="research-card-top">
                  <span className="research-icon">
                    <Icon size={24} strokeWidth={1.7} />
                  </span>
                  <span className="research-category">{module.category}</span>
                  <ArrowUpRight className="research-arrow" size={21} />
                </div>
                <ModuleIllustration kind={module.illustration} />
                <span className="research-english">{module.english}</span>
                <h3>{module.name}</h3>
                <strong className="research-question">{module.question}</strong>
                <p>{module.description}</p>
                <div className="research-tags">
                  {module.tags.map((tag) => (
                    <span key={tag}>{tag}</span>
                  ))}
                </div>
                <div className="research-card-footer">
                  <span>进入研究</span>
                  <ArrowUpRight size={17} />
                </div>
              </Link>
            )
          })}
        </div>
        {!visible.length && (
          <div className="directory-empty">
            <Search size={28} />
            <h3>没有找到匹配的模块</h3>
            <p>试试“行业”“成交”或“微盘”，也可以重置筛选。</p>
            <button
              className="button button-quiet"
              onClick={() => {
                setSearch('')
                setCategory('全部模块')
              }}
            >
              查看全部模块
            </button>
          </div>
        )}
      </section>
      <div className="overview-note">
        <span className="overview-note-icon">
          <Layers3 size={22} />
        </span>
        <div>
          <strong>研究视野，持续生长。</strong>
          <p>新的研究工具将在这里汇集。先从感兴趣的问题出发，找到属于你的市场观察方式。</p>
        </div>
        <span className="overview-note-mark" aria-hidden="true">
          MORE PERSPECTIVES, MORE POSSIBILITIES.
        </span>
      </div>
    </div>
  )
}
