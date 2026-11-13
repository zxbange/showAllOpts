import { PageHeader } from '../components/PageHeader'
import { useEffect, useState } from 'react'
import { useAuth, type User } from '../components/AccountAccess'
import { accountRequest } from '../api/http'

export function UsersPage() {
  const { session, signedOut } = useAuth()
  const admin = session?.user?.role === 'admin'
  const [users, setUsers] = useState<User[]>([])
  const [name, setName] = useState('')
  const [password, setPassword] = useState('')
  const [role, setRole] = useState('viewer')
  const [old, setOld] = useState('')
  const [next, setNext] = useState('')
  const [target, setTarget] = useState<User | null>(null)
  const [reset, setReset] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [message, setMessage] = useState('')
  async function load() {
    setUsers((await accountRequest<{ users: User[] }>('/users')).users)
  }
  useEffect(() => {
    if (admin) void load().catch((err) => setError(err.message))
  }, [admin])
  async function act(action: () => Promise<void>) {
    if (busy) return
    setBusy(true)
    setError('')
    setMessage('')
    try {
      await action()
    } catch (err) {
      setError(err instanceof Error ? err.message : '操作失败')
    } finally {
      setBusy(false)
    }
  }
  return (
    <div className="accounts-page">
      <PageHeader>
        <div>
          <h1>{admin ? '用户管理' : '我的账号'}</h1>
        </div>
      </PageHeader>
      {error && (
        <p role="alert" className="account-error">
          {error}
        </p>
      )}
      {message && <p role="status">{message}</p>}
      <section className="account-panel">
        <h2>修改我的密码</h2>
        <form
          onSubmit={(e) => {
            e.preventDefault()
            void act(async () => {
              await accountRequest('/password', { old_password: old, new_password: next })
              setOld('')
              setNext('')
              signedOut()
            })
          }}
        >
          <label>
            原密码
            <input
              type="password"
              autoComplete="current-password"
              value={old}
              onChange={(e) => setOld(e.target.value)}
              required
              maxLength={128}
            />
          </label>
          <label>
            新密码
            <input
              type="password"
              autoComplete="new-password"
              value={next}
              onChange={(e) => setNext(e.target.value)}
              required
              minLength={12}
              maxLength={128}
            />
          </label>
          <button className="button button-primary" disabled={busy}>
            修改密码并重新登录
          </button>
        </form>
      </section>
      {admin && (
        <>
          <section className="account-panel">
            <h2>创建用户</h2>
            <form
              onSubmit={(e) => {
                e.preventDefault()
                void act(async () => {
                  await accountRequest('/users', { username: name, password, role })
                  setPassword('')
                  setName('')
                  await load()
                  setMessage('用户已创建')
                })
              }}
            >
              <label>
                用户名
                <input
                  value={name}
                  onChange={(e) => setName(e.target.value)}
                  required
                  minLength={3}
                  maxLength={32}
                  autoComplete="off"
                />
              </label>
              <label>
                初始密码
                <input
                  type="password"
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  required
                  minLength={12}
                  maxLength={128}
                  autoComplete="new-password"
                />
              </label>
              <label>
                角色
                <select value={role} onChange={(e) => setRole(e.target.value)}>
                  <option value="viewer">普通用户（只读）</option>
                  <option value="admin">管理员</option>
                </select>
              </label>
              <button className="button button-primary" disabled={busy}>
                创建用户
              </button>
            </form>
          </section>
          <section className="account-panel">
            <h2>用户列表</h2>
            <div className="account-table">
              <table>
                <thead>
                  <tr>
                    <th>用户名</th>
                    <th>角色</th>
                    <th>状态</th>
                    <th>操作</th>
                  </tr>
                </thead>
                <tbody>
                  {users.map((user) => (
                    <tr key={user.id}>
                      <td>{user.username}</td>
                      <td>{user.role === 'admin' ? '管理员' : '普通用户'}</td>
                      <td>{user.enabled ? '启用' : '停用'}</td>
                      <td>
                        <button
                          className="button button-quiet"
                          disabled={busy}
                          onClick={() => {
                            setTarget(user)
                            setReset('')
                          }}
                        >
                          重置密码
                        </button>
                        <button
                          className="button button-quiet"
                          disabled={busy || user.id === session?.user?.id}
                          onClick={() => {
                            if (
                              window.confirm(
                                `${user.enabled ? '停用' : '启用'}用户 ${user.username}？已有登录将失效。`,
                              )
                            )
                              void act(async () => {
                                await accountRequest('/users/' + user.id, {
                                  enabled: !user.enabled,
                                })
                                await load()
                              })
                          }}
                        >
                          {user.enabled ? '停用' : '启用'}
                        </button>
                        <button
                          className="button button-quiet"
                          disabled={busy || user.id === session?.user?.id}
                          onClick={() => {
                            if (
                              window.confirm(
                                `将 ${user.username} 改为${user.role === 'admin' ? '普通用户' : '管理员'}？`,
                              )
                            )
                              void act(async () => {
                                await accountRequest('/users/' + user.id, {
                                  role: user.role === 'admin' ? 'viewer' : 'admin',
                                })
                                await load()
                              })
                          }}
                        >
                          更改角色
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </section>
          {target && (
            <section className="account-panel">
              <h2>重置 {target.username} 的密码</h2>
              <form
                onSubmit={(e) => {
                  e.preventDefault()
                  void act(async () => {
                    await accountRequest('/users/' + target.id, { password: reset })
                    setReset('')
                    setTarget(null)
                    if (target.id === session?.user?.id) signedOut()
                    else setMessage('密码已重置，该用户的旧登录已失效')
                  })
                }}
              >
                <label>
                  新密码
                  <input
                    type="password"
                    value={reset}
                    onChange={(e) => setReset(e.target.value)}
                    minLength={12}
                    maxLength={128}
                    autoComplete="new-password"
                    required
                  />
                </label>
                <button className="button button-primary" disabled={busy}>
                  确认重置
                </button>
                <button
                  type="button"
                  className="button"
                  onClick={() => {
                    setTarget(null)
                    setReset('')
                  }}
                >
                  取消
                </button>
              </form>
            </section>
          )}
        </>
      )}
    </div>
  )
}
