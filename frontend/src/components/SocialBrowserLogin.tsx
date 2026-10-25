import { useEffect, useRef, useState } from 'react'

type Platform = 'weibo' | 'xueqiu'
type LoginState = {
  id?: string
  state: string
  message: string
  image?: boolean
  revision?: number
  expires_at?: number
}
const active = (state?: LoginState) =>
  !!state && ['starting', 'waiting', 'verification'].includes(state.state)

export function SocialBrowserLogin({
  available,
  onSaved,
}: {
  available: boolean
  onSaved: () => void
}) {
  const [key, setKey] = useState('')
  const [unlocked, setUnlocked] = useState(false)
  const [error, setError] = useState('')
  const [pending, setPending] = useState(false)
  const [states, setStates] = useState<Partial<Record<Platform, LoginState>>>({})
  const [images, setImages] = useState<Partial<Record<Platform, string>>>({})
  const imageUrls = useRef<Partial<Record<Platform, string>>>({})
  const saved = useRef(new Set<string>())
  const onSavedRef = useRef(onSaved)
  onSavedRef.current = onSaved

  useEffect(
    () => () => {
      Object.values(imageUrls.current).forEach((url) => URL.revokeObjectURL(url))
    },
    [],
  )

  async function request(platform: Platform, path: string, body?: unknown, signal?: AbortSignal) {
    const response = await fetch(`/api/v1/social-monitor/browser/${platform}${path}`, {
      method: body === undefined ? 'GET' : 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Browser-Login-Key': key },
      cache: 'no-store',
      signal,
      ...(body === undefined ? {} : { body: JSON.stringify(body) }),
    })
    if (!response.ok) {
      if (response.status === 403) setUnlocked(false)
      const result = await response.json().catch(() => null)
      throw new Error(typeof result?.detail === 'string' ? result.detail : '登录窗口请求失败')
    }
    return response
  }

  useEffect(() => {
    if (!unlocked) return
    const abort = new AbortController()
    let timer: ReturnType<typeof setTimeout>
    const poll = async () => {
      try {
        if (!document.hidden) {
          for (const platform of ['weibo', 'xueqiu'] as const) {
            const state: LoginState = await (
              await request(platform, '/status', undefined, abort.signal)
            ).json()
            if (abort.signal.aborted) return
            setStates((current) => ({ ...current, [platform]: state }))
            if (state.state === 'success' && state.id && !saved.current.has(state.id)) {
              saved.current.add(state.id)
              onSavedRef.current()
            }
            let next: string | undefined
            if (state.image && active(state) && state.id) {
              const response = await request(
                platform,
                `/image/${state.id}`,
                undefined,
                abort.signal,
              )
              const blob = await response.blob()
              if (abort.signal.aborted) return
              next = URL.createObjectURL(blob)
            }
            const previous = imageUrls.current[platform]
            imageUrls.current[platform] = next
            setImages((current) => ({ ...current, [platform]: next }))
            if (previous) URL.revokeObjectURL(previous)
          }
        }
      } catch (err) {
        if (abort.signal.aborted) return
        setError(err instanceof Error ? err.message : '无法读取登录状态')
      }
      if (!abort.signal.aborted) timer = setTimeout(() => void poll(), 2500)
    }
    void poll()
    return () => {
      abort.abort()
      clearTimeout(timer)
    }
    // The management key remains in memory; changing it requires unlocking again.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [unlocked, key])

  async function run(
    platform: Platform,
    action: 'start' | 'cancel' | 'check' | 'reload' | 'click',
    x?: number,
    y?: number,
  ) {
    if (pending) return
    setPending(true)
    setError('')
    try {
      const response = await request(
        platform,
        action === 'start' ? '/start' : '/action',
        action === 'start' ? {} : { id: states[platform]?.id, action, x, y },
      )
      const state: LoginState = await response.json()
      setStates((current) => ({ ...current, [platform]: state }))
    } catch (err) {
      setError(err instanceof Error ? err.message : '登录操作失败')
    } finally {
      setPending(false)
    }
  }

  return (
    <div className="social-browser-login">
      {!available ? (
        <p className="social-muted">
          请先在服务器私有配置中设置至少 24 位的 SOCIAL_BROWSER_LOGIN_KEY，并安装 Chromium
          后重启服务。
        </p>
      ) : (
        <>
          <form
            className="social-browser-key"
            onSubmit={(event) => {
              event.preventDefault()
              setError('')
              setUnlocked(true)
            }}
          >
            <label>
              浏览器登录管理密钥
              <input
                type="password"
                autoComplete="off"
                value={key}
                minLength={24}
                required
                disabled={unlocked}
                onChange={(event) => setKey(event.target.value)}
              />
            </label>
            {unlocked ? (
              <button
                className="button"
                type="button"
                onClick={() => {
                  setUnlocked(false)
                  setKey('')
                  setStates({})
                  setImages({})
                  setError('')
                  Object.values(imageUrls.current).forEach((url) => URL.revokeObjectURL(url))
                  imageUrls.current = {}
                }}
              >
                隐藏登录窗口
              </button>
            ) : (
              <button className="button" type="submit">
                查看登录状态
              </button>
            )}
          </form>
          {error && (
            <p role="alert" className="social-error">
              {error}
            </p>
          )}
          {unlocked && (
            <div className="social-browser-cards">
              {(['weibo', 'xueqiu'] as const).map((platform) => (
                <article className="social-browser-card" key={platform}>
                  <h4>{platform === 'weibo' ? '微博' : '雪球'}</h4>
                  <p role="status">{states[platform]?.message || '正在读取登录状态…'}</p>
                  <div className="social-actions">
                    <button
                      className="button"
                      type="button"
                      disabled={pending || active(states[platform])}
                      onClick={() => void run(platform, 'start')}
                    >
                      打开扫码登录
                    </button>
                    {active(states[platform]) && (
                      <>
                        <button
                          className="button"
                          type="button"
                          disabled={pending}
                          onClick={() => void run(platform, 'check')}
                        >
                          检查登录结果
                        </button>
                        <button
                          className="button"
                          type="button"
                          disabled={pending}
                          onClick={() => void run(platform, 'reload')}
                        >
                          刷新登录页
                        </button>
                        <button
                          className="button"
                          type="button"
                          disabled={pending}
                          onClick={() => void run(platform, 'cancel')}
                        >
                          取消登录
                        </button>
                      </>
                    )}
                  </div>
                  {images[platform] && active(states[platform]) && (
                    <>
                      <p className="social-muted">
                        可点击画面中的扫码入口。请用手机应用扫描二维码并确认；不支持在画面中输入密码或拖动滑块。
                      </p>
                      <button
                        type="button"
                        className="social-browser-screen"
                        disabled={pending}
                        aria-label={`操作${platform === 'weibo' ? '微博' : '雪球'}官方登录画面`}
                        onClick={(event) => {
                          const rect = event.currentTarget.getBoundingClientRect()
                          void run(
                            platform,
                            'click',
                            Math.min(
                              1099,
                              Math.max(
                                0,
                                Math.floor(((event.clientX - rect.left) * 1100) / rect.width),
                              ),
                            ),
                            Math.min(
                              799,
                              Math.max(
                                0,
                                Math.floor(((event.clientY - rect.top) * 800) / rect.height),
                              ),
                            ),
                          )
                        }}
                      >
                        <img
                          src={images[platform]}
                          alt={`${platform === 'weibo' ? '微博' : '雪球'}官方登录画面`}
                          draggable={false}
                        />
                      </button>
                    </>
                  )}
                </article>
              ))}
            </div>
          )}
        </>
      )}
    </div>
  )
}
