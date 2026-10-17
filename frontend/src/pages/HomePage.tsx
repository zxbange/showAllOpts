import { useState } from 'react'
import { ArrowUpRight, Search, X } from 'lucide-react'
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
      ) : kind === 'shares' || kind === 'earnings' ? (
        <div className="illustration-bars">
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
            : kind === 'earnings'
              ? '业绩与价格'
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
      <section
        className="research-directory"
        id="research-modules"
        aria-labelledby="directory-title"
      >
        <div className="directory-heading">
          <div>
            <span className="section-eyebrow">EXPLORE YOUR WORKSPACE</span>
            <h1 id="directory-title">
              全部研究模块
              <span>{researchModules.length.toString().padStart(2, '0')}</span>
            </h1>
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
    </div>
  )
}
