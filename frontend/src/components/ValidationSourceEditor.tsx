import { useEffect, useState, useImperativeHandle, forwardRef } from 'react'
import { validationRequest, type ValidationAuth } from '../api/validation'
export type SourceRef = { id: string; revision: number }
type Source = SourceRef & {
  name: string
  host: string
  port: number
  database: string
  username: string
  tls: string
  ca_pem: string
  password_set: boolean
}
const empty = {
  name: '研究数据库',
  host: '',
  port: 3306,
  database: '',
  username: '',
  tls: 'verify',
  ca_pem: '',
}
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
  const [message, setMessage] = useState('')
  const [busy, setBusy] = useState(false)
  const [dirty, setDirty] = useState(false)
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
    setBusy(true)
    setMessage('')
    try {
      const body = { ...form, ...(editing || {}), password: editing && !password ? null : password }
      if (action === 'save') {
        const result = await validationRequest<Source>(adminKey, '/sources/save', body)
        setSources((old) => [result, ...old.filter((s) => s.id !== result.id)])
        setEditing({ id: result.id, revision: result.revision })
        onSelect({ id: result.id, revision: result.revision })
        setPassword('')
        setDirty(false)
        setMessage('配置已加密保存并选用新版本；已有任务仍绑定原版本。')
        return { id: result.id, revision: result.revision }
      } else {
        const result = await validationRequest<{ message: string }>(adminKey, '/sources/test', body)
        setMessage(result.message)
      }
    } catch (e) {
      setMessage(e instanceof Error ? e.message : '操作失败')
      if (rethrow) throw e
    } finally {
      setBusy(false)
    }
  }
  return (
    <fieldset className="validation-source" disabled={busy}>
      <legend className="sr-only">验证数据库（独立于其他模块）</legend>
      <div className="validation-source-picker">
        <label>
          使用的数据源
          <select
            aria-label="验证数据源"
            value={selected ? `${selected.id}:${selected.revision}` : ''}
            onChange={(e) => {
              const source = sources.find((s) => `${s.id}:${s.revision}` === e.target.value)
              onSelect(source ? { id: source.id, revision: source.revision } : null)
              setDirty(false)
              setPassword('')
              setEditing(source ? { id: source.id, revision: source.revision } : null)
              setForm(source ?? empty)
            }}
          >
            <option value="">
              {paired ? '填写新数据库连接' : '服务默认数据库（创建任务时绑定配置版本）'}
            </option>
            {sources.map((s) => (
              <option key={s.id} value={`${s.id}:${s.revision}`}>
                {s.name} · v{s.revision}
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
              setForm(source)
              setEditing({ id: source.id, revision: source.revision })
              setPassword('')
              setMessage('密码不回显；编辑时留空保留原密码。')
            }
          }}
        >
          编辑所选配置
        </button>
      </div>
      <details className="validation-connection" open={paired || undefined}>
        <summary>新增或编辑数据库配置</summary>
        <p className="validation-hint">
          只读连接研究数据源，独立于其他模块。密码加密保存，不在浏览器持久存储。
        </p>
        <p>
          {editing
            ? `编辑 v${editing.revision}，保存将创建新版本；密码留空保留。`
            : '新增配置；请填写连接信息。'}
        </p>
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
              value={password}
              onChange={(e) => {
                setDirty(true)
                setPassword(e.target.value)
              }}
            />
          </label>
        </div>
        <details className="validation-disclosure validation-tls">
          <summary>连接安全选项 · TLS 与 CA 证书</summary>
          <label>
            TLS
            <select
              aria-label="数据库TLS"
              value={form.tls}
              onChange={(e) => {
                setDirty(true)
                setForm((old) => ({ ...old, tls: e.target.value }))
              }}
            >
              <option value="verify">加密并校验证书和身份（推荐）</option>
              <option value="required">加密但不校验证书</option>
              <option value="disabled">不使用TLS（仅可信网络）</option>
            </select>
          </label>
          <label>
            可选CA证书
            <textarea
              aria-label="数据库CA证书"
              value={form.ca_pem}
              onChange={(e) => {
                setDirty(true)
                setForm((old) => ({ ...old, ca_pem: e.target.value }))
              }}
            />
          </label>
        </details>
        <div className="short-controls validation-source-actions">
          <button className="button" onClick={() => void submit('test')}>
            测试连接（只读）
          </button>
          <button className="button" onClick={() => void submit('save')}>
            加密保存并使用
          </button>
          <button
            className="button button-quiet"
            onClick={() => {
              setEditing(null)
              setDirty(false)
              onSelect(null)
              setForm(empty)
              setPassword('')
            }}
          >
            清空并新增
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
