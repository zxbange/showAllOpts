import { createContext, useContext, useEffect, useState, type ReactNode } from 'react'
import { accountRequest, setCsrf } from '../api/http'
import '../styles/accounts.css'

export type User = {
  id: string
  username: string
  role: 'admin' | 'viewer'
  enabled: boolean | number
  created_at: number
}
export type AccountSession = {
  user: User | null
  csrf_token: string | null
  configured: boolean
  https_required: boolean
}
const Context = createContext<{ session: AccountSession | null; signedOut: () => void }>({
  session: null,
  signedOut: () => {},
})
export const useAuth = () => useContext(Context)

export function AccountAccess({ children }: { children: ReactNode }) {
  const [session, setSession] = useState<AccountSession | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [name, setName] = useState('')
  const [password, setPassword] = useState('')
  const [busy, setBusy] = useState(false)
  const [revision, setRevision] = useState(0)
  function accept(next: AccountSession) {
    setCsrf(next.csrf_token ?? '')
    setSession(next)
  }
  function signedOut() {
    setCsrf('')
    setPassword('')
    setSession((old) => (old ? { ...old, user: null, csrf_token: null } : null))
  }
  useEffect(() => {
    let active = true
    setLoading(true)
    accountRequest<AccountSession>('/session')
      .then((next) => {
        if (active) {
          accept(next)
          setError('')
        }
      })
      .catch((err) => {
        if (active) setError(err instanceof Error ? err.message : '无法检查登录状态')
      })
      .finally(() => {
        if (active) setLoading(false)
      })
    const expired = () => {
      signedOut()
      setError('登录已过期，请重新登录')
    }
    window.addEventListener('account-session-expired', expired)
    return () => {
      active = false
      window.removeEventListener('account-session-expired', expired)
    }
  }, [revision])
  if (loading)
    return (
      <main className="account-screen">
        <p role="status">正在检查登录状态…</p>
      </main>
    )
  if (session?.user)
    return <Context.Provider value={{ session, signedOut }}>{children}</Context.Provider>
  const insecure = session?.https_required && window.location.protocol !== 'https:'
  return (
    <main className="account-screen">
      <section className="account-card">
        <h1>发奋墙</h1>
        <p>登录研究工作台</p>
        {!session ? (
          <button className="button" onClick={() => setRevision((n) => n + 1)}>
            重新检查
          </button>
        ) : !session.configured ? (
          <div className="account-setup">
            <p>请先在服务器终端创建管理员。</p>
            <code>
              APP_ENV_FILE=config/app.config backend/.venv/bin/python backend/manage_users.py init
              --username admin
            </code>
          </div>
        ) : insecure ? (
          <p role="alert">请配置并通过 HTTPS 访问后登录，避免密码和会话通过 HTTP 明文传输。</p>
        ) : (
          <form
            onSubmit={async (event) => {
              event.preventDefault()
              if (busy) return
              setBusy(true)
              setError('')
              try {
                accept(await accountRequest<AccountSession>('/login', { username: name, password }))
              } catch (err) {
                setError(err instanceof Error ? err.message : '登录失败')
              } finally {
                setPassword('')
                setBusy(false)
              }
            }}
          >
            <label>
              用户名
              <input
                value={name}
                onChange={(e) => setName(e.target.value)}
                autoComplete="username"
                maxLength={32}
                required
              />
            </label>
            <label>
              密码
              <input
                type="password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                autoComplete="current-password"
                maxLength={128}
                required
              />
            </label>
            <button className="button button-primary" disabled={busy}>
              {busy ? '正在登录…' : '登录'}
            </button>
          </form>
        )}
        {error && (
          <p role="alert" className="account-error">
            {error}
          </p>
        )}
      </section>
    </main>
  )
}
