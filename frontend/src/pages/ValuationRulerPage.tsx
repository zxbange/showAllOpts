import { HistoryRangeSwitch } from '../components/HistoryRangeSwitch'
import { DisplayRefreshButton, PageActionButton } from '../components/PageActionButton'
import { PageHeaderActions } from '../components/PageHeaderActions'
import { useCallback, useEffect, useState } from 'react'
import { RefreshCw, Gauge, Layers3 } from 'lucide-react'
import { useAuth } from '../components/AccountAccess'
import { PageHeader } from '../components/PageHeader'
import { StatCard } from '../components/StatCard'
import { ValuationRulerChart } from '../components/ValuationRulerChart'
import { getValuationHistory } from '../api/valuationRuler'
import type { ValuationHistory } from '../types/valuationRuler'
import { useCollectionTask } from '../hooks/useCollectionTask'
import { valuationCollectionTask, valuationErrorText } from '../lib/valuationCollectionTask'
import { valuationNumber, valuationSource } from '../lib/valuationRuler'
import '../styles/valuation-ruler.css'

const phaseLabels: Record<string, string> = {
  collecting: '采集中',
  saving: '保存数据库与本地',
  refresh: '准备采集',
  'sync-joinquant': '同步聚宽历史',
  import: '同步聚宽历史',
  complete: '已完成',
  cancelled: '已停止',
  failed: '未完成',
  interrupted: '任务中断',
}
export function ValuationRulerPage() {
  const { session } = useAuth()
  const admin = session?.user?.role === 'admin'
  const {
    status,
    statusError,
    actionError,
    pending: busy,
    uncertain,
    accepted,
    historyRevision,
    act,
  } = useCollectionTask(valuationCollectionTask, session, admin)
  const [data, setData] = useState<ValuationHistory | null>(null)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)
  const [days, setDays] = useState(0)
  const [indexCode, setIndexCode] = useState('000300.XSHG')
  const [basis, setBasis] = useState<'ttm' | 'lyr'>('ttm')
  const [revision, setRevision] = useState(0)
  const reload = useCallback(() => setRevision((n) => n + 1), [])
  useEffect(() => {
    const controller = new AbortController()
    setLoading(true)
    getValuationHistory(controller.signal, indexCode, basis)
      .then((next) => {
        if (!controller.signal.aborted) {
          setData(next)
          setError('')
        }
      })
      .catch((reason) => {
        if (!controller.signal.aborted) setError(valuationErrorText(reason))
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false)
      })
    return () => controller.abort()
  }, [revision, historyRevision, indexCode, basis])
  const databaseSyncing = status?.database_syncing ?? false
  const running = !databaseSyncing && (accepted || status?.status === 'running')
  const disabled = databaseSyncing || !admin || busy || uncertain || !status || !!statusError
  const latest = data?.latest
  const benchmark = data?.benchmarks
  const comparison = status?.comparison
  const v2 = !!data?.basis
  const indexName = data?.index_name ?? '沪深300'
  const peLabel = data?.basis === 'lyr' ? '静态' : '滚动'
  return (
    <div className="page-enter valuation-page">
      <PageHeader dataTimeNaiveBeijing dataUpdatedAt={data?.updated_at}>
        <div>
          <h1>
            市场情绪 <span className="heading-tag">估值标尺</span>
          </h1>
          <p>指数走势与成分股估值。</p>
        </div>
        <div className="heading-actions">
          <DisplayRefreshButton
            ariaLabel="刷新估值展示数据"
            onClick={reload}
            loading={loading}
            iconButton={false}
          />
        </div>
      </PageHeader>
      {actionError && <p role="alert">{actionError}</p>}
      <div className="valuation-filters">
        <label>
          跟踪指数{' '}
          <select
            aria-label="估值指数"
            value={indexCode}
            onChange={(e) => setIndexCode(e.target.value)}
          >
            <option value="000300.XSHG">沪深300</option>
            <option value="000016.XSHG">上证50</option>
            <option value="000852.XSHG">中证1000</option>
          </select>
        </label>
        <div className="range-switch" aria-label="市盈率口径">
          {(['lyr', 'ttm'] as const).map((value) => (
            <button
              key={value}
              aria-pressed={basis === value}
              className={basis === value ? 'active' : ''}
              onClick={() => setBasis(value)}
            >
              {value === 'lyr' ? '静态市盈率' : '滚动市盈率'}
            </button>
          ))}
        </div>
      </div>
      <div className="stats-grid valuation-stats">
        <StatCard
          label={indexName + '指数'}
          value={valuationNumber(latest?.index_close)}
          note={latest?.date ?? '等待数据'}
          icon={<Layers3 size={18} />}
        />
        <StatCard
          label={v2 ? peLabel + '等权PE' : '成分股平均PE-TTM'}
          value={valuationNumber(v2 ? latest?.mean_pe : latest?.mean_pe_ttm)}
          suffix="倍"
          note={`有效正PE ${latest?.positive_count ?? '—'} 只 · 负PE ${latest?.loss_count ?? '—'} 只 · 缺失PE ${latest?.missing_pe_count ?? (latest ? 0 : '—')} 只`}
          icon={<Gauge size={18} />}
          accent
        />
        <StatCard
          label={v2 && basis === 'lyr' ? '静态整体PE' : '全历史 P20'}
          value={valuationNumber(v2 && basis === 'lyr' ? latest?.overall_pe : benchmark?.p20)}
          suffix="倍"
          note={v2 && basis === 'lyr' ? '总市值 / 上年利润合计' : '等权PE历史20%分位'}
          icon={<Gauge size={18} />}
        />
        <StatCard
          label={v2 ? peLabel + '中位数PE' : '全历史 P80'}
          value={valuationNumber(v2 ? latest?.median_pe : benchmark?.p80)}
          suffix="倍"
          note={v2 ? '等权与中位数样本：0 < PE ≤ 300' : '已采集完整日均值的80%分位'}
          icon={<Gauge size={18} />}
        />
      </div>
      {error && (
        <div className="valuation-error" role="alert">
          {error}。已保存的展示数据不会被清空。
        </div>
      )}
      <section className="panel valuation-trend" aria-busy={loading}>
        <div className="panel-heading">
          <div>
            <h2>指数与估值 · 自{data?.coverage.requested_start.slice(0, 4) ?? '2010'}年起</h2>
            <p>左轴指数 · 右轴PE倍数</p>
          </div>
          <HistoryRangeSwitch
            label="估值历史范围"
            options={[
              { value: 365, label: '近1年' },
              { value: 1095, label: '近3年' },
              { value: 0, label: '全部' },
            ]}
            value={days}
            onChange={setDays}
          />
        </div>
        {data?.points.length ? (
          <ValuationRulerChart data={data} days={days} />
        ) : (
          <div className="valuation-empty">
            {loading ? '正在读取估值历史…' : '暂无完整估值记录。请先导入聚宽历史或采集可用交易日。'}
          </div>
        )}
        <div className="panel-bottom">
          <span>
            {v2
              ? 'P20 / P50 / P80：已采集等权PE历史分位。'
              : 'P20/P80 基于2010年以来全部已采集完整交易日的日均值，缩放不会重新计算。'}
          </span>
          <span>
            样本 {benchmark?.sample_days ?? 0} 日 · {benchmark?.start_date ?? '—'} 至{' '}
            {benchmark?.end_date ?? '—'}
          </span>
        </div>
      </section>
      <details className="panel valuation-method method-card">
        <summary>统计口径与覆盖</summary>
        {v2 ? (
          <p>
            等权与中位数取0＜PE≤300；整体PE由总市值与个股PE反推利润。东方财富按采集日成分计算，历史使用聚宽当日成分；普通停牌不剔除，暂停上市状态未独立核验。
          </p>
        ) : (
          <p>
            按每个交易日实际沪深300成分名单，正 PE-TTM
            等权算术平均；负PE与缺失PE分别统计，均不参与均值。保留300只成分，存在有效正PE的日期进入分位样本；缺失交易日不填充、不跨缺口连线。
          </p>
        )}
        <p>
          请求起点 {data?.coverage.requested_start ?? '2010-01-01'}；实际完整记录{' '}
          {data?.coverage.first_date ?? '—'} 至 {data?.coverage.last_date ?? '—'}。
          {data?.coverage.calendar_complete
            ? `交易日历已核验，缺失 ${data.coverage.missing_count ?? '未知'} 日。`
            : `交易日历覆盖未完整核验；已知缺失 ${data?.coverage.known_missing_count ?? 0} 日，不代表全部缺失数量。`}
        </p>
        {!v2 && (
          <p>
            中证最新成分文件不能替代历史成分；日期过期或历史不可用会保留失败记录，请通过聚宽 MySQL
            历史同步补齐。聚宽与公开接口口径尚不能视为等价，不覆盖已有完整记录。
          </p>
        )}
        <p>
          实际来源：
          {data?.sources.length ? data.sources.map(valuationSource).join('、') : '暂无记录'}
        </p>
        {!!data?.gap_dates.length && (
          <details>
            <summary>查看缺失日期（{data.gap_dates.length}）</summary>
            <p>{data.gap_dates.join('、')}</p>
          </details>
        )}
      </details>
      <>
        <PageHeaderActions>
          <PageActionButton
            icon={RefreshCw}
            iconSize={15}
            spin={busy || running}
            disabled={disabled || running}
            onClick={() => void act()}
          >
            {databaseSyncing
              ? '数据库同步中'
              : running
                ? '采集中…'
                : busy
                  ? '正在提交…'
                  : '更新数据'}
          </PageActionButton>
        </PageHeaderActions>
        {(running || databaseSyncing) && (
          <p role="status">
            {databaseSyncing
              ? '数据库同步中'
              : `采集中 · ${status?.status === 'running' && status.phase ? (phaseLabels[status.phase] ?? status.phase) : '等待确认本次采集状态'}`}
          </p>
        )}
        {(statusError || (!actionError && status?.error)) && (
          <p role="alert">{statusError || status?.error}</p>
        )}
        {uncertain && (
          <p role="alert">上次操作结果不确定，已暂停重复提交。正在读取服务器状态；请勿重复启动。</p>
        )}

        {!v2 && (
          <section className="panel valuation-comparison">
            <h2>重叠交易日来源对照</h2>
            <p>仅比较实际重叠日期，不覆盖已保存日；单日一致也不证明两来源长期等价。</p>
            {comparison ? (
              <>
                <p>
                  {comparison.date} · 已保存：{valuationSource(comparison.published_source)} ·
                  候选：
                  {valuationSource(comparison.candidate_source)}
                </p>
                <p>
                  {comparison.match ? '本次样本一致' : '本次样本存在差异'} · 均值差{' '}
                  {comparison.mean_pe_delta} · 指数差 {comparison.index_close_delta} · 最大个股PE差{' '}
                  {comparison.max_abs_pe_delta}
                </p>
                <p>
                  变更PE {comparison.changed_member_count} 只 · 新增成分{' '}
                  {comparison.added_members.join('、') || '无'} · 移除成分{' '}
                  {comparison.removed_members.join('、') || '无'}
                </p>
                <details>
                  <summary>查看实际差异明细</summary>
                  <pre>{JSON.stringify(comparison.differences, null, 2)}</pre>
                </details>
                <p>对照时间：{comparison.compared_at}</p>
              </>
            ) : (
              <p>尚未完成重叠样本对照，不能宣称来源一致。</p>
            )}
            {status?.comparison_error && (
              <p className="valuation-error">
                {status.comparison_attempted_date ?? '—'} 对照失败：{status.comparison_error}
                。已完整保存的聚宽记录仍然有效，对照失败不等于该日缺失。
              </p>
            )}
          </section>
        )}
      </>
    </div>
  )
}
