import { lazy, Suspense } from 'react'
import { BrowserRouter, Link, Route, Routes } from 'react-router-dom'
import { Layout } from './components/Layout'
import { HomePage } from './pages/HomePage'
import { MomentumPage } from './pages/MomentumPage'
import { LoadingState } from './components/States'

const ShortTermPage = lazy(() =>
  import('./pages/ShortTermPage').then((m) => ({ default: m.ShortTermPage })),
)
const SocialMonitorPage = lazy(() =>
  import('./pages/SocialMonitorPage').then((m) => ({ default: m.SocialMonitorPage })),
)

const OwnershipPage = lazy(() =>
  import('./pages/OwnershipPage').then((m) => ({ default: m.OwnershipPage })),
)

const ETFHeatPage = lazy(() =>
  import('./pages/ETFHeatPage').then((m) => ({ default: m.ETFHeatPage })),
)
const ETFHeatDetailPage = lazy(() =>
  import('./pages/ETFHeatPage').then((m) => ({ default: m.ETFHeatDetailPage })),
)

const GrowthPage = lazy(() => import('./pages/GrowthPage').then((m) => ({ default: m.GrowthPage })))

const DividendPage = lazy(() =>
  import('./pages/DividendPage').then((m) => ({ default: m.DividendPage })),
)

const IndustryPage = lazy(() =>
  import('./pages/IndustryPage').then((module) => ({ default: module.IndustryPage })),
)

const SentimentPage = lazy(() =>
  import('./pages/SentimentPage').then((module) => ({ default: module.SentimentPage })),
)

const CrowdingPage = lazy(() =>
  import('./pages/CrowdingPage').then((module) => ({ default: module.CrowdingPage })),
)

const BreadthPage = lazy(() =>
  import('./pages/BreadthPage').then((m) => ({ default: m.BreadthPage })),
)

const MicrocapPage = lazy(() =>
  import('./pages/MicrocapPage').then((module) => ({ default: module.MicrocapPage })),
)

const ETFPage = lazy(() =>
  import('./pages/ETFPage').then((module) => ({ default: module.ETFPage })),
)

const EarningsPage = lazy(() =>
  import('./pages/EarningsPage').then((m) => ({ default: m.EarningsPage })),
)
const EarningsDetailPage = lazy(() =>
  import('./pages/EarningsDetailPage').then((m) => ({ default: m.EarningsDetailPage })),
)

export default function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route element={<Layout />}>
          <Route index element={<HomePage />} />
          <Route
            path="social-monitor"
            element={
              <Suspense fallback={<LoadingState />}>
                <SocialMonitorPage />
              </Suspense>
            }
          />
          <Route
            path="short-term"
            element={
              <Suspense fallback={<LoadingState />}>
                <ShortTermPage />
              </Suspense>
            }
          />
          <Route
            path="institution-ownership"
            element={
              <Suspense fallback={<LoadingState />}>
                <OwnershipPage />
              </Suspense>
            }
          />
          <Route
            path="etf-momentum"
            element={
              <Suspense fallback={<LoadingState />}>
                <ETFHeatPage />
              </Suspense>
            }
          />
          <Route
            path="etf-momentum/funds/:code"
            element={
              <Suspense fallback={<LoadingState />}>
                <ETFHeatDetailPage />
              </Suspense>
            }
          />
          <Route
            path="high-growth"
            element={
              <Suspense fallback={<LoadingState />}>
                <GrowthPage />
              </Suspense>
            }
          />
          <Route
            path="dividend-selection"
            element={
              <Suspense fallback={<LoadingState />}>
                <DividendPage />
              </Suspense>
            }
          />
          <Route
            path="earnings-gap"
            element={
              <Suspense fallback={<LoadingState />}>
                <EarningsPage />
              </Suspense>
            }
          />
          <Route
            path="earnings-gap/stocks/:code"
            element={
              <Suspense fallback={<LoadingState />}>
                <EarningsDetailPage />
              </Suspense>
            }
          />
          <Route
            path="etf-shares"
            element={
              <Suspense fallback={<LoadingState />}>
                <ETFPage />
              </Suspense>
            }
          />
          <Route
            path="microcap-breadth"
            element={
              <Suspense fallback={<LoadingState />}>
                <BreadthPage />
              </Suspense>
            }
          />
          <Route path="industry-momentum" element={<MomentumPage />} />
          <Route
            path="microcap-temperature"
            element={
              <Suspense fallback={<LoadingState />}>
                <MicrocapPage />
              </Suspense>
            }
          />
          <Route
            path="market-sentiment"
            element={
              <Suspense fallback={<LoadingState />}>
                <SentimentPage />
              </Suspense>
            }
          />
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
