// Test-only Vite entry: use the exact ECharts module identity the app uses.
// No production bundle hook or browser-global chart registry is needed.
export { getInstanceByDom } from 'echarts/core'
