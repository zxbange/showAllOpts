import { test, expect, adminSession } from './fixtures'

test('private configuration, baseline, grouped positions and daily history survive navigation', async ({
  page,
}, info) => {
  let configured = false
  let enabled = false
  let saved: any = null
  let version = 0
  let rows: any[] = []
  let events: any[] = []
  const requests: any[] = []
  await page.route('**/api/v1/**', async (route) => {
    const url = new URL(route.request().url())
    if (!url.pathname.includes('/personal-holdings/'))
      return route.fulfill({ json: { modules: [] } })
    const path = url.pathname.split('/').pop()
    if (route.request().method() === 'POST') {
      expect(route.request().headers()['x-csrf-token']).toBe('test-csrf')
      requests.push({ path, body: route.request().postDataJSON() })
      if (path === 'configuration') {
        const { password, ...fields } = route.request().postDataJSON()
        saved = { ...fields, password_set: true }
        configured = true
        return route.fulfill({ json: saved })
      }
      if (path === 'test') return route.fulfill({ json: { ok: true } })
      if (path === 'start') enabled = true
      if (path === 'stop') enabled = false
    }
    if (path === 'configuration') return route.fulfill({ json: saved })
    if (path === 'history')
      return route.fulfill({
        json: {
          days: ['2026-11-02'],
          events: url.searchParams.get('day') === '2026-11-02' ? events : [],
          next_before: null,
        },
      })
    return route.fulfill({
      json: {
        configured,
        enabled,
        baseline: version > 0,
        version,
        rows,
        window_open: true,
        trading_day: true,
        error: null,
        calendar_error: null,
        observed_at: version ? '2026-11-02T01:30:00Z' : null,
      },
    })
  })
  await page.clock.install({ time: new Date('2026-11-02T01:30:00Z') })
  await page.goto('/personal-holdings')
  await expect(page.getByRole('heading', { name: '个人持仓', exact: true })).toBeVisible()
  await page.getByLabel('主机', { exact: true }).fill('mock.db.test')
  await page.getByLabel('数据库', { exact: true }).fill('mock_positions')
  await page.getByLabel('用户名', { exact: true }).fill('readonly')
  await page.getByLabel('持仓表名', { exact: true }).fill('positions')
  await page.getByLabel('密码', { exact: true }).fill('mock-secret-never-store')
  await page.getByRole('button', { name: '测试连接', exact: true }).click()
  await expect(page.getByRole('button', { name: '测试连接成功' })).toBeVisible()
  expect(requests).toHaveLength(1)
  await page.getByRole('button', { name: '保存', exact: true }).click()
  await expect(page.getByRole('button', { name: '开始监控', exact: true })).toBeEnabled()
  expect(enabled).toBe(false)
  await page.getByText('数据库配置', { exact: true }).click()
  await expect(page.getByLabel('密码', { exact: true })).toHaveAttribute('placeholder', '********')
  await expect(page.getByLabel('密码', { exact: true })).toHaveValue('')
  await page.getByRole('button', { name: '测试连接', exact: true }).click()
  await expect(page.getByRole('button', { name: '测试连接成功' })).toBeVisible()
  expect(requests.at(-1).body.password).toBe(null)
  await page.getByRole('button', { name: '开始监控', exact: true }).click()
  await expect(page.getByRole('button', { name: '停止监控', exact: true })).toBeEnabled()
  rows = [
    { strategy2: '趋势策略', code: '000001', name: '模拟银行', amt: '100' },
    { strategy2: '价值策略', code: '000001', name: '模拟银行', amt: '300' },
  ]
  version++
  await page.clock.runFor(16000)
  await expect(page.getByRole('heading', { name: '趋势策略' })).toBeVisible()
  await expect(page.getByRole('heading', { name: '价值策略' })).toBeVisible()
  await expect(page.getByRole('cell', { name: '000001', exact: true })).toHaveCount(2)
  events = [
    {
      id: 1,
      strategy2: '趋势策略',
      code: '000001',
      name: '模拟银行',
      delta: '100',
      before: '100',
      after: '200',
      kind: 'add',
      observed_at: '2026-11-02T01:31:00Z',
    },
  ]
  rows[0] = { ...rows[0], amt: '200' }
  version++
  await page.clock.runFor(16000)
  await expect(page.getByText('加仓', { exact: true })).toBeVisible()
  await expect(page.getByText('+100', { exact: true })).toBeVisible()
  await page.getByText('数据库配置', { exact: true }).click()
  await expect(page.locator('#main-content')).toHaveCount(1)
  await page.evaluate(() => {
    ;(document.activeElement as HTMLElement)?.blur()
    window.scrollTo(0, 0)
  })
  await page.screenshot({ path: info.outputPath('holdings.png'), fullPage: true })
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
  expect(
    await page.evaluate(() => JSON.stringify({ ...localStorage, ...sessionStorage })),
  ).not.toContain('mock-secret')
  await page.getByRole('button', { name: '停止监控', exact: true }).click()
  await expect(page.getByText('监控已停止', { exact: true })).toBeVisible()
  await page.reload()
  await expect(page.getByText('加仓', { exact: true })).toBeVisible()
})

test('viewer has neither navigation nor private data requests on deep link', async ({ page }) => {
  await page.route('**/api/auth/**', (route) =>
    route.fulfill({ json: { ...adminSession, user: { ...adminSession.user, role: 'viewer' } } }),
  )
  const calls: string[] = []
  await page.route('**/api/v1/personal-holdings/**', (route) => {
    calls.push(route.request().url())
    return route.fulfill({ status: 403, json: { detail: '管理员权限' } })
  })
  await page.goto('/personal-holdings')
  await expect(page.getByText('仅管理员可访问')).toBeVisible()
  await expect(page.getByRole('link', { name: '个人持仓', exact: true })).toHaveCount(0)
  expect(calls).toHaveLength(0)
})

test('calendar unavailable and failed polling retain last holdings without fake liquidation', async ({
  page,
}) => {
  let failure = false
  let calendar = false
  let revoked = false
  await page.route('**/api/v1/**', (route) => {
    const path = new URL(route.request().url()).pathname
    if (path.endsWith('/configuration')) return route.fulfill({ json: null })
    if (path.endsWith('/history'))
      return route.fulfill({ json: { days: [], events: [], next_before: null } })
    if (path.endsWith('/status') && path.includes('personal-holdings')) {
      if (revoked) return route.fulfill({ status: 403, json: { detail: '此功能需要管理员权限' } })
      if (failure) return route.fulfill({ status: 503, json: { detail: '临时连接错误' } })
      return route.fulfill({
        json: {
          configured: true,
          enabled: true,
          baseline: true,
          version: 1,
          rows: [{ strategy2: '保留策略', code: '000001', name: '模拟股票', amt: '100' }],
          window_open: !calendar,
          trading_day: calendar ? null : true,
          calendar_error: calendar
            ? '交易日历未覆盖今天，已停止源库轮询，请更新官方年度日历'
            : null,
          error: null,
          observed_at: '2026-11-02T01:30:00Z',
        },
      })
    }
    return route.fulfill({ json: { modules: [] } })
  })
  await page.clock.install({ time: new Date('2026-11-02T01:30:00Z') })
  await page.goto('/personal-holdings')
  await expect(page.getByRole('heading', { name: '保留策略' })).toBeVisible()
  failure = true
  await page.clock.runFor(16000)
  await expect(page.getByRole('alert')).toContainText('临时连接错误')
  await expect(page.getByRole('cell', { name: '模拟股票', exact: true })).toBeVisible()
  failure = false
  calendar = true
  await page.clock.runFor(16000)
  await expect(page.getByRole('alert')).toContainText('交易日历未覆盖今天')
  await expect(page.getByText('临时连接错误')).toHaveCount(0)
  await expect(page.getByRole('cell', { name: '模拟股票', exact: true })).toBeVisible()
  await expect(page.getByText('清仓', { exact: true })).toHaveCount(0)
  revoked = true
  await page.clock.runFor(16000)
  await expect(page.getByRole('button', { name: '登录', exact: true })).toBeVisible()
  await expect(page.getByRole('cell', { name: '模拟股票', exact: true })).toHaveCount(0)
})

test('pending stop suppresses background reads and late pre-operation status cannot overwrite start', async ({
  page,
}) => {
  let enabled = true
  let statusReads = 0
  let releaseStop!: () => void
  let releaseOld!: () => void
  const stopGate = new Promise<void>((resolve) => {
    releaseStop = resolve
  })
  const oldGate = new Promise<void>((resolve) => {
    releaseOld = resolve
  })
  let holdRead = false
  const value = () => ({
    configured: true,
    enabled,
    baseline: true,
    version: 1,
    rows: [],
    window_open: true,
    trading_day: true,
    error: null,
    calendar_error: null,
    observed_at: '2026-11-02T01:30:00Z',
  })
  await page.route('**/api/v1/**', async (route) => {
    const path = new URL(route.request().url()).pathname
    if (!path.includes('personal-holdings')) return route.fulfill({ json: { modules: [] } })
    if (path.endsWith('/configuration')) return route.fulfill({ json: null })
    if (path.endsWith('/history'))
      return route.fulfill({ json: { days: [], events: [], next_before: null } })
    if (path.endsWith('/stop')) {
      await stopGate
      enabled = false
      return route.fulfill({ json: value() })
    }
    if (path.endsWith('/start')) {
      enabled = true
      return route.fulfill({ json: value() })
    }
    statusReads++
    const captured = value()
    if (holdRead) await oldGate
    return route.fulfill({ json: captured })
  })
  await page.clock.install({ time: new Date('2026-11-02T01:30:00Z') })
  try {
    await page.goto('/personal-holdings')
    await expect(page.getByRole('button', { name: '停止监控', exact: true })).toBeEnabled()
    const initialReads = statusReads // React StrictMode can issue an aborted initial read.
    await page.getByRole('button', { name: '停止监控', exact: true }).click()
    await page.clock.runFor(16000)
    await expect(page.getByRole('button', { name: '停止监控', exact: true })).toBeDisabled()
    expect(statusReads).toBe(initialReads)
    releaseStop()
    await expect(page.getByRole('button', { name: '开始监控', exact: true })).toBeEnabled()
    holdRead = true
    await page.clock.runFor(16000)
    await expect.poll(() => statusReads).toBe(initialReads + 1)
    await page.getByRole('button', { name: '开始监控', exact: true }).click()
    await expect(page.getByRole('button', { name: '停止监控', exact: true })).toBeEnabled()
    releaseOld()
    await page.clock.runFor(500)
    await expect(page.getByRole('button', { name: '停止监控', exact: true })).toBeEnabled()
  } finally {
    releaseStop()
    releaseOld()
  }
})

test('manual sync outside trading window keeps monitoring off and survives navigation', async ({page}, info) => {
  let manual: any = null
  let calls = 0
  let version = 0
  const config = {host:'mock.db.test',port:3306,database:'mock',username:'reader',table:'positions',password_set:true}
  await page.route('**/api/v1/personal-holdings/**', async route => {
    const path = new URL(route.request().url()).pathname.split('/').pop()
    if (path === 'configuration') return route.fulfill({json:config})
    if (path === 'history') return route.fulfill({json:{days:[],events:[],next_before:null}})
    if (path === 'sync') {
      calls++
      expect(route.request().headers()['x-csrf-token']).toBe('test-csrf')
      expect(route.request().postDataJSON().request_id).toMatch(/^[a-f0-9-]+$/)
      manual = {id:'mock',status:'pending',error:null}
    }
    return route.fulfill({json:{configured:true,enabled:false,baseline:version>0,version,
      rows:version ? [{strategy2:'模拟策略',name:'模拟银行',code:'000001',amt:'100'}] : [],
      window_open:false,trading_day:false,calendar_error:null,error:null,observed_at:null,manual}})
  })
  await page.goto('/personal-holdings')
  await page.getByRole('button',{name:'立即同步',exact:true}).click()
  await expect(page.getByRole('button',{name:'同步中…'})).toBeDisabled()
  expect(calls).toBe(1)
  await page.reload()
  await expect(page.getByRole('button',{name:'同步中…'})).toBeDisabled()
  manual.status='completed';version=1
  await expect(page.getByRole('cell',{name:'000001',exact:true})).toBeVisible({timeout:10000})
  await expect(page.getByRole('button',{name:'开始监控',exact:true})).toBeEnabled()
  await expect(page.getByRole('status')).toContainText('同步完成')
  expect(calls).toBe(1)
  await page.screenshot({path:info.outputPath('manual.png'),fullPage:true})
  await page.getByRole('button',{name:'立即同步',exact:true}).click()
  await expect(page.getByRole('button',{name:'同步中…'})).toBeDisabled()
  expect(calls).toBe(2)
  manual.status='completed'
  await expect(page.getByRole('button',{name:'立即同步',exact:true})).toBeEnabled({timeout:10000})
})

test('compact data precedes controls and history is isolated by Shanghai date', async ({page}, info) => {
  await page.route('**/api/v1/personal-holdings/**', async route => {
    const url=new URL(route.request().url())
    if(url.pathname.endsWith('/configuration')) return route.fulfill({json:{host:'mock.test',port:3306,database:'mock',username:'reader',table:'positions',password_set:true}})
    if(url.pathname.endsWith('/history')) {
      const day=url.searchParams.get('day')!
      return route.fulfill({json:{days:['2026-11-02','2026-11-01'],next_before:null,events:[{id:day.endsWith('02')?2:1,strategy2:'模拟策略',name:day.endsWith('02')?'今日股票':'昨日股票',code:'000001',delta:'100',after:'200',before:'100',kind:'add',observed_at:day.endsWith('02')?'2026-11-01T16:05:00Z':'2026-11-01T07:00:00Z'}]}})
    }
    return route.fulfill({json:{configured:true,enabled:false,baseline:true,version:1,rows:['趋势策略','价值策略','反弹策略'].map(strategy2=>({strategy2,code:'000001',name:'模拟银行',amt:'200'})),window_open:false,trading_day:true,calendar_error:null,error:null,observed_at:'2026-11-01T16:05:00Z'}})
  })
  await page.goto('/personal-holdings')
  await page.getByLabel('历史日期',{exact:true}).selectOption('2026-11-02')
  await expect(page.getByRole('heading',{name:'2026-11-02 北京时间'})).toBeVisible()
  await expect(page.getByRole('cell',{name:'今日股票',exact:true})).toBeVisible()
  await expect(page.getByRole('cell',{name:'00:05:00',exact:true})).toBeVisible()
  await page.getByLabel('历史日期',{exact:true}).selectOption('2026-11-01')
  await expect(page.getByRole('heading',{name:'2026-11-01 北京时间'})).toBeVisible()
  await expect(page.getByRole('cell',{name:'昨日股票',exact:true})).toBeVisible()
  await expect(page.getByText('今日股票',{exact:true})).toHaveCount(0)
  const columnCount=await page.locator('.holdings-groups').evaluate(el=>getComputedStyle(el).gridTemplateColumns.split(' ').length)
  expect(columnCount).toBe(info.project.name==='desktop'?3:1)
  const positions=await page.getByRole('region',{name:'当前持仓',exact:true}).boundingBox()
  const history=await page.getByRole('region',{name:'仓位变动',exact:true}).boundingBox()
  const toolbar=await page.locator('.holdings-toolbar').boundingBox()
  const config=await page.locator('.holdings-configuration').boundingBox()
  expect(positions!.y).toBeLessThan(history!.y)
  expect(history!.y+history!.height).toBeLessThanOrEqual(toolbar!.y)
  expect(toolbar!.y+toolbar!.height).toBeLessThanOrEqual(config!.y)
  expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth)).toBe(true)
  await page.screenshot({path:info.outputPath('compact.png'),fullPage:true})
})

test('bounded history separates stock columns, scrolls by keyboard and keeps pagination', async ({page}, info) => {
  const record=(id:number)=>({id,strategy2:'模拟策略',name:`模拟股票${id}`,code:String(id).padStart(6,'0'),delta:'100',after:'200',before:'100',kind:'add',observed_at:'2026-11-02T02:00:00Z'})
  await page.route('**/api/v1/personal-holdings/**',async route=>{
    const url=new URL(route.request().url())
    if(url.pathname.endsWith('/configuration')) return route.fulfill({json:null})
    if(url.pathname.endsWith('/history')) return route.fulfill({json:{days:['2026-11-02'],events:url.searchParams.has('before')?[record(1)]:Array.from({length:40},(_,i)=>record(41-i)),next_before:url.searchParams.has('before')?null:2}})
    return route.fulfill({json:{configured:false,enabled:false,baseline:false,version:0,rows:[],window_open:false,trading_day:true,calendar_error:null,error:null,observed_at:null}})
  })
  await page.goto('/personal-holdings')
  await page.getByLabel('历史日期',{exact:true}).selectOption('2026-11-02')
  const section=page.getByRole('region',{name:'仓位变动',exact:true})
  await expect(page.getByLabel('记录日期',{exact:true})).toHaveCount(0)
  await expect(section.getByRole('columnheader',{name:'股票',exact:true})).toBeVisible()
  await expect(section.getByRole('columnheader',{name:'代码',exact:true})).toBeVisible()
  const scroll=page.getByRole('region',{name:'仓位变动表，可上下左右滚动',exact:true})
  const size=await scroll.evaluate(el=>({h:el.clientHeight,full:el.scrollHeight}))
  expect(size.h).toBeLessThanOrEqual(480)
  expect(size.full).toBeGreaterThan(size.h)
  await scroll.focus();await page.keyboard.press('PageDown')
  await expect.poll(()=>scroll.evaluate(el=>el.scrollTop)).toBeGreaterThan(0)
  await page.getByRole('button',{name:'加载更多'}).click()
  await expect(section.getByRole('row')).toHaveCount(42)
  await expect(page.getByRole('button',{name:'加载更多'})).toHaveCount(0)
  expect((await scroll.boundingBox())!.height).toBeLessThanOrEqual(480)
  await scroll.evaluate(el=>{el.scrollTop=0})
  await expect(page.getByText('按策略查看持仓，保存每次观测到的仓位变化。',{exact:true})).toHaveCount(0)
  await expect(page.getByText('记录的是两次完整快照间的净变化，不代表逐笔成交；时间为北京时间的实际检测时间。',{exact:true})).toHaveCount(0)
  await expect(page.getByText('每15秒查询，包含午间。关页后后台继续；首次完整读取建立基准，不记买入。',{exact:true})).toHaveCount(0)
  expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth)).toBe(true)
  await page.screenshot({path:info.outputPath('bounded-history.png'),fullPage:true})
})
