import { useEffect, useRef, useState, useImperativeHandle, forwardRef } from 'react'
import { validationRequest, type ValidationAuth } from '../api/validation'
export type SourceRef = { id: string; revision: number }
type Source = SourceRef & {
  name: string
  host: string
  port: number
  database: string
  username: string
  password_set: boolean
}
const empty = {
  name: '研究数据库',
  host: '',
  port: 3306,
  database: '',
  username: '',
}
const sourceForm = ({ name, host, port, database, username }: typeof empty) => ({
  name,
  host,
  port,
  database,
  username,
})
export type SourceEditorHandle = { prepare: () => Promise<SourceRef | null> }
export const ValidationSourceEditor = forwardRef<
  SourceEditorHandle,
  {
    adminKey: ValidationAuth
    selected: SourceRef | null
    onSelect: (source: SourceRef | null) => void
  }
>(function ValidationSourceEditor({ adminKey, selected, onSelect }, ref) {
  const [sources, setSources] = useState<Source[]>([])
  const [form, setForm] = useState(empty)
  const [editing, setEditing] = useState<SourceRef | null>(null)
  const [password, setPassword] = useState('')
  const original = sources.find((s) => s.id === editing?.id && s.revision === editing.revision)
  const unchangedConnection =
    !!original &&
    (['name', 'host', 'port', 'database', 'username'] as const).every((field) =>
      field === 'host'
        ? original[field].toLowerCase() === form[field].toLowerCase()
        : original[field] === form[field],
    )
  const savedPassword = unchangedConnection && original?.password_set
  const [message, setMessage] = useState('')
  const [busy, setBusy] = useState(false)
  const [dirty, setDirty] = useState(false)
  const [saving, setSaving] = useState(false)
  const [testState, setTestState] = useState<'idle' | 'testing' | 'success' | 'error'>('idle')
  const requestGeneration = useRef(0)
  const inFlight = useRef(false)
  function clearTest() {
    requestGeneration.current++
    setTestState('idle')
    setMessage('')
  }
  useEffect(() => {
    clearTest()
    return () => {
      requestGeneration.current++
    }
  }, [selected?.id, selected?.revision, adminKey])
  const paired = typeof adminKey !== 'string'
  useImperativeHandle(ref, () => ({
    prepare: async () => {
      if (dirty || (paired && !selected)) {
        const result = await submit('save', true)
        if (!result) throw new Error('请先填写数据库连接信息')
        return result
      }
      return selected
    },
  }))
  useEffect(() => {
    let active = true
    validationRequest<{ sources: Source[] }>(adminKey, '/sources')
      .then((data) => {
        if (!Array.isArray(data.sources)) throw new Error('配置列表响应异常，请重新解锁')
        if (active) setSources(data.sources)
      })
      .catch((e) => {
        if (active) setMessage(e.message)
      })
    return () => {
      active = false
    }
  }, [adminKey])
  async function submit(action: 'save' | 'test', rethrow = false): Promise<SourceRef | undefined> {
    if (inFlight.current) {
      if (rethrow) throw new Error('请等待当前连接操作完成')
      return
    }
    inFlight.current = true
    const generation = requestGeneration.current
    setBusy(true)
    setSaving(action === 'save')
    if (action === 'test') setTestState('testing')
    setMessage('')
    try {
      if (editing && !password && !unchangedConnection)
        throw new Error('数据库名称或连接信息已变更，请重新输入密码')
      const body = { ...form, ...(editing || {}), password: editing && !password ? null : password }
      if (action === 'save') {
        const result = await validationRequest<Source>(adminKey, '/sources/save', body)
        setSources((old) => [result, ...old.filter((s) => s.id !== result.id)])
        setEditing({ id: result.id, revision: result.revision })
        onSelect({ id: result.id, revision: result.revision })
        setPassword('')
        setDirty(false)
        setMessage('配置已保存并选用。')
        return { id: result.id, revision: result.revision }
      } else {
        await validationRequest(adminKey, '/sources/test', body)
        if (generation === requestGeneration.current) setTestState('success')
      }
    } catch (e) {
      if (generation === requestGeneration.current) {
        setMessage(e instanceof Error ? e.message : '操作失败')
        if (action === 'test') setTestState('error')
      }
      if (rethrow) throw e
    } finally {
      inFlight.current = false
      setBusy(false)
      setSaving(false)
    }
  }
  return (
    <fieldset className="validation-source" disabled={saving}>
      <legend className="sr-only">验证数据库（独立于其他模块）</legend>
      <div className="validation-source-picker">
        <label>
          使用的数据源
          <select
            aria-label="验证数据源"
            value={selected ? `${selected.id}:${selected.revision}` : ''}
            onChange={(e) => {
              clearTest()
              const source = sources.find((s) => `${s.id}:${s.revision}` === e.target.value)
              onSelect(source ? { id: source.id, revision: source.revision } : null)
              setDirty(false)
              setPassword('')
              setEditing(source ? { id: source.id, revision: source.revision } : null)
              setForm(sourceForm(source ?? empty))
            }}
          >
            <option value="">{paired ? '填写新数据库连接' : '服务默认数据库'}</option>
            {sources.map((s) => (
              <option key={s.id} value={`${s.id}:${s.revision}`}>
                {s.name}
              </option>
            ))}
          </select>
        </label>
        <button
          className="button button-quiet"
          type="button"
          disabled={!selected || busy}
          onClick={() => {
            const source = sources.find(
              (s) => s.id === selected?.id && s.revision === selected?.revision,
            )
            if (source) {
              setForm(sourceForm(source))
              setEditing({ id: source.id, revision: source.revision })
              setPassword('')
              clearTest()
            }
          }}
        >
          编辑所选配置
        </button>
      </div>
      <details className="validation-connection" open={paired || undefined}>
        <summary>新增或编辑数据库配置</summary>
        <div className="validation-source-fields">
          {(['name', 'host', 'port', 'database', 'username'] as const).map((field) => (
            <label key={field} className={'validation-field-' + field}>
              {
                {
                  name: '配置名称',
                  host: '数据库地址',
                  port: '数据库端口',
                  database: '数据库名',
                  username: '数据库用户名',
                }[field]
              }
              <input
                aria-label={
                  {
                    name: '配置名称',
                    host: '数据库地址',
                    port: '数据库端口',
                    database: '数据库名',
                    username: '数据库用户名',
                  }[field]
                }
                type={field === 'port' ? 'number' : 'text'}
                min={field === 'port' ? 1 : undefined}
                max={field === 'port' ? 65535 : undefined}
                autoComplete="off"
                value={form[field]}
                onChange={(e) => {
                  clearTest()
                  setDirty(true)
                  setForm((old) => ({
                    ...old,
                    [field]: field === 'port' ? Number(e.target.value) : e.target.value,
                  }))
                }}
              />
            </label>
          ))}
          <label className="validation-field-password">
            数据库密码
            <input
              aria-label="数据库密码"
              type="password"
              autoComplete="new-password"
              placeholder={savedPassword ? '********' : undefined}
              aria-describedby={savedPassword ? 'validation-password-help' : undefined}
              value={password}
              onChange={(e) => {
                clearTest()
                setDirty(true)
                setPassword(e.target.value)
              }}
            />
            {savedPassword && (
              <small id="validation-password-help">
                ******** 表示已保存密码；输入可替换，清空则保留原密码。
              </small>
            )}
          </label>
        </div>
        <div className="short-controls validation-source-actions">
          <button
            className={'button validation-probe probe-' + testState}
            disabled={busy}
            aria-busy={busy && !saving}
            onClick={() => void submit('test')}
          >
            测试连接{' '}
            <span aria-hidden="true">
              {busy && !saving
                ? '…'
                : testState === 'success'
                  ? '✓'
                  : testState === 'error'
                    ? '✕'
                    : ''}
            </span>
          </button>
          <span className="sr-only" role="status">
            {busy && !saving
              ? '正在测试连接'
              : testState === 'success'
                ? '连接成功'
                : testState === 'error'
                  ? '连接失败'
                  : '连接尚未测试'}
          </span>
          <button className="button" disabled={busy} onClick={() => void submit('save')}>
            保存
          </button>
        </div>
      </details>
      {message && (
        <p className="validation-feedback" role="status">
          {message}
        </p>
      )}
    </fieldset>
  )
})
