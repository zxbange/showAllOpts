import { lazy, Suspense } from 'react'
import { BrowserRouter, Link, Route, Routes } from 'react-router-dom'
import { Layout } from './components/Layout'
import { HomePage } from './pages/HomePage'
import { MomentumPage } from './pages/MomentumPage'
import { LoadingState } from './components/States'

const IndustryPage = lazy(() =>
  import('./pages/IndustryPage').then((module) => ({ default: module.IndustryPage })),
)

const CrowdingPage = lazy(() =>
  import('./pages/CrowdingPage').then((module) => ({ default: module.CrowdingPage })),
)

export default function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route element={<Layout />}>
          <Route index element={<HomePage />} />
          <Route path="industry-momentum" element={<MomentumPage />} />
          <Route
            path="market-crowding"
            element={
              <Suspense fallback={<LoadingState />}>
                <CrowdingPage />
              </Suspense>
            }
          />
          <Route
            path="industry-momentum/industries/:code"
            element={
              <Suspense fallback={<LoadingState />}>
                <IndustryPage />
              </Suspense>
            }
          />
          <Route
            path="*"
            element={
              <div className="state-panel">
                <h1>页面没有找到</h1>
                <p>这个地址暂时没有对应的研究内容。</p>
                <Link className="button button-primary" to="/">
                  返回首页
                </Link>
              </div>
            }
          />
        </Route>
      </Routes>
    </BrowserRouter>
  )
}
