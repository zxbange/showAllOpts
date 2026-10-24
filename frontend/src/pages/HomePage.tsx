import { useState } from 'react'
import { ArrowUpRight, Search, X } from 'lucide-react'
import { Link } from 'react-router-dom'
import { researchModules } from '../lib/modules'
import type { ResearchModule } from '../lib/modules'
import '../styles/overview.css'

function ModuleIllustration({ kind }: { kind: ResearchModule['illustration'] }) {
  return (
    <div className={`module-illustration illustration-${kind}`} aria-hidden="true">
      {kind === 'ownership' ? (
        <svg className="illustration-share-trends" viewBox="0 0 320 130" fill="none">
          {[70, 160, 250].map((x, i) => (
            <g key={x}>
              <circle
                cx={x}
                cy={35 + i * 9}
                r="14"
                fill={['#eab66a', '#c94835', '#68a696'][i]}
                opacity=".8"
              />
              <path
                d={`M${x - 28} 99 Q${x - 28} ${57 + i * 5} ${x} ${57 + i * 5} Q${x + 28} ${57 + i * 5} ${x + 28} 99`}
                fill={['#eab66a', '#c94835', '#68a696'][i]}
                opacity=".3"
              />
            </g>
          ))}
          <path
            d="M30 112H290M99 39H130M190 48H220"
            stroke="#cdbba6"
            strokeWidth="2"
            strokeDasharray="4 4"
          />
        </svg>
      ) : kind === 'etf-heat' ? (
        <svg className="illustration-share-trends" viewBox="0 0 320 130" fill="none">
          {[0, 1, 2, 3, 4, 5, 6, 7].map((n) => (
            <rect
              key={n}
              x={20 + (n % 4) * 72}
              y={15 + Math.floor(n / 4) * 54}
              width="62"
              height="44"
              rx="8"
              fill={n < 4 ? '#c44837' : '#3f9576'}
              opacity={0.2 + (7 - n) * 0.08}
            />
          ))}
          <path
            d="M38 100 L110 82 L182 54 L254 28"
            stroke="#aa3828"
            strokeWidth="4"
            strokeLinecap="round"
          />
        </svg>
      ) : kind === 'growth' ? (
        <svg
          className="illustration-share-trends"
          viewBox="0 0 260 100"
          fill="none"
          focusable="false"
        >
          {[0, 1, 2, 3].map((i) => (
            <g key={i} transform={`translate(${12 + i * 63},0)`}>
              <path d="M0 84H48" stroke="#dbc4af" />
              {[26, 43, 63].map((h, j) => (
                <rect
                  key={j}
                  x={j * 16}
                  y={84 - h - i * 4}
                  width="11"
                  height={h + i * 4}
                  rx="3"
                  fill={i % 2 ? '#b88c4b' : '#c74736'}
                  opacity={0.4 + j * 0.3}
                />
              ))}
              <circle cx="22" cy="94" r="2" fill="#277d70" />
            </g>
          ))}
        </svg>
      ) : kind === 'dividend' ? (
        <svg
          className="illustration-share-trends"
          viewBox="0 0 260 100"
          fill="none"
          focusable="false"
        >
          {[0, 1, 2].map((i) => (
            <g key={i} transform={`translate(${40 + i * 80},${60 - i * 15})`}>
              {[0, 1, 2, 3].slice(0, i + 2).map((j) => (
                <g key={j} transform={`translate(0,${-j * 10})`}>
                  <path d="M-26 0v8c0 12 52 12 52 0V0" fill="#e9c58b" stroke="#bb8842" />
                  <ellipse rx="26" ry="8" fill="#f7e5be" stroke="#bb8842" />
                </g>
              ))}
              <path d="M-8 -6L0 -10 8 -6 0 -2Z" stroke="#ba5840" />
            </g>
          ))}
        </svg>
      ) : kind === 'breadth' ? (
        <svg
          className="illustration-share-trends"
          viewBox="0 0 260 100"
          fill="none"
          focusable="false"
        >
          <path d="M10 50H250" stroke="#d9c9ba" strokeDasharray="4 5" />
          {[16, 29, 22, 37, 43, 32, 45, 38, 47].map((h, i) => (
            <g key={i}>
              <rect
                x={16 + i * 26}
                y={50 - h}
                width="9"
                height={h}
                rx="3"
                fill="#c44837"
                opacity={0.5 + i * 0.05}
              />
              <rect
                x={26 + i * 26}
                y="52"
                width="7"
                height={45 - h / 1.4}
                rx="3"
                fill="#217b70"
                opacity=".65"
              />
            </g>
          ))}
        </svg>
      ) : kind === 'heatmap' ? (
        <div className="illustration-tiles">
          {Array.from({ length: 24 }, (_, i) => (
            <span key={i} style={{ opacity: 1 - Math.floor(i / 4) * 0.13 }} />
          ))}
        </div>
      ) : kind === 'shares' ? (
        <svg
          className="illustration-share-trends"
          viewBox="0 0 260 100"
          fill="none"
          focusable="false"
        >
          <path className="share-trend-grid" d="M12 20H248M12 45H248M12 70H248M12 95H248" />
          <path
            className="share-trend-area"
            d="M12 78C32 78 38 54 59 60S89 72 111 48S143 59 165 34S203 48 224 19L248 12V95H12Z"
          />
          <path
            className="share-trend-line share-trend-coral"
            d="M12 78C32 78 38 54 59 60S89 72 111 48S143 59 165 34S203 48 224 19L248 12"
          />
          <path
            className="share-trend-line share-trend-jade"
            d="M12 87C35 87 41 73 64 78S101 62 125 68S159 48 183 55S222 39 248 43"
          />
          <path
            className="share-trend-line share-trend-gold"
            d="M12 92C39 92 48 85 72 88S111 77 137 81S176 67 201 72S231 61 248 64"
          />
          <circle className="share-trend-dot share-trend-coral" cx="248" cy="12" r="4" />
          <circle className="share-trend-dot share-trend-jade" cx="248" cy="43" r="4" />
          <circle className="share-trend-dot share-trend-gold" cx="248" cy="64" r="4" />
        </svg>
      ) : kind === 'earnings' ? (
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
        {kind === 'ownership'
          ? '机构持仓 · 股东户数'
          : kind === 'etf-heat'
            ? 'ETF动量'
            : kind === 'growth'
              ? '四类报告 · 持续增长'
              : kind === 'dividend'
                ? '分红与估值'
                : kind === 'heatmap'
                  ? '行业强弱'
                  : kind === 'concentration'
                    ? '成交集中'
                    : kind === 'earnings'
                      ? '业绩与价格'
                      : kind === 'breadth'
                        ? '新高与新低'
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
      [module.name, module.description, ...module.tags]
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

                <h3>{module.name}</h3>
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
            <p>更换关键词或重置筛选。</p>
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
