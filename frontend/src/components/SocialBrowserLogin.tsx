import { apiFetch } from '../api/http'
import { useEffect, useRef, useState } from 'react'

type Platform = 'weibo' | 'xueqiu'
type LoginState = {
  id?: string
  state: string
  message: string
  image?: boolean
  session?: { state: string; checked_at?: string | number | null }
}
const platforms = ['weibo', 'xueqiu'] as const
const label = (platform: Platform) => (platform === 'weibo' ? '微博' : '雪球')
const active = (state?: LoginState) =>
  !!state && ['starting', 'waiting', 'verification', 'cancelling'].includes(state.state)
const normal = (state?: LoginState) => state?.session?.state === 'normal'
const sessionMessage = (state?: LoginState) => {
  if (!state) return '正在读取登录状态…'
  if (active(state)) return state.message
  if (normal(state)) return '登录正常'
  if (state.session?.state === 'expired') return '登录已失效，请重新扫码'
  if (state.session?.state === 'restricted') return '平台访问受限，请检查账号或扫码处理'
  if (['error', 'blocked', 'expired'].includes(state.state)) return state.message
  if (state.session?.state === 'unknown') return '已有登录凭据，等待下一次监控验证'
  return '未登录'
}

export function SocialBrowserLogin({
  available,
  onSaved,
}: {
  available: boolean
  onSaved: () => void
}) {
  const [error, setError] = useState('')
  const [pending, setPending] = useState(false)
  const [states, setStates] = useState<Partial<Record<Platform, LoginState>>>({})
  const [images, setImages] = useState<Partial<Record<Platform, string>>>({})
  const imageUrls = useRef<Partial<Record<Platform, string>>>({})
  const pendingRef = useRef(false)
  const epoch = useRef(0)
  const saved = useRef(new Set<string>())
  const onSavedRef = useRef(onSaved)
  onSavedRef.current = onSaved

  function clearImages() {
    Object.values(imageUrls.current).forEach((url) => URL.revokeObjectURL(url))
    imageUrls.current = {}
    setImages({})
  }
  async function request(platform: Platform, path: string, body?: unknown, signal?: AbortSignal) {
    const response = await apiFetch(`/api/v1/social-monitor/browser/${platform}${path}`, {
      method: body === undefined ? 'GET' : 'POST',
      headers: { 'Content-Type': 'application/json' },
      cache: 'no-store',
      signal,
      ...(body === undefined ? {} : { body: JSON.stringify(body) }),
    })
    if (!response.ok) {
      const result = await response.json().catch(() => null)
      throw new Error(typeof result?.detail === 'string' ? result.detail : '平台登录请求失败')
    }
    return response
  }

  useEffect(() => {
    if (!available) return
    const abort = new AbortController()
    let timer: ReturnType<typeof setTimeout>
    const poll = async () => {
      const version = epoch.current
      const stale = () => abort.signal.aborted || version !== epoch.current || pendingRef.current
      let scanning = false
      try {
        if (!document.hidden && !pendingRef.current) {
          for (const platform of platforms) {
            const state: LoginState = await (
              await request(platform, '/status', undefined, abort.signal)
            ).json()
            if (stale()) return
            scanning ||= active(state)
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
              if (stale()) return
              next = URL.createObjectURL(blob)
            }
            const previous = imageUrls.current[platform]
            imageUrls.current[platform] = next
            setImages((current) => ({ ...current, [platform]: next }))
            if (previous) URL.revokeObjectURL(previous)
          }
          setError('')
        }
      } catch (err) {
        if (!stale()) setError(err instanceof Error ? err.message : '无法读取登录状态')
      } finally {
        if (!abort.signal.aborted)
          timer = setTimeout(() => void poll(), scanning || pendingRef.current ? 1500 : 4000)
      }
    }
    void poll()
    return () => {
      abort.abort()
      clearTimeout(timer)
      Object.values(imageUrls.current).forEach((url) => URL.revokeObjectURL(url))
      imageUrls.current = {}
    }
  }, [available])

  async function run(
    platform: Platform,
    action: 'start' | 'cancel' | 'logout' | 'click',
    x?: number,
    y?: number,
  ) {
    if (pendingRef.current) return
    if (action === 'logout' && !window.confirm(`退出${label(platform)}登录并暂停该平台监控？`))
      return
    pendingRef.current = true
    epoch.current++
    setPending(true)
    setError('')
    clearImages()
    try {
      const direct = action === 'start' || action === 'logout'
      const state: LoginState = await (
        await request(
          platform,
          direct ? `/${action}` : '/action',
          direct ? {} : { id: states[platform]?.id, action, x, y },
        )
      ).json()
      setStates((current) => ({ ...current, [platform]: state }))
      if (action === 'logout') onSavedRef.current()
    } catch (err) {
      setError(err instanceof Error ? err.message : '登录操作失败')
    } finally {
      pendingRef.current = false
      setPending(false)
    }
  }

  const scanning = platforms.some((platform) => active(states[platform]))
  return (
    <div className="social-browser-login">
      {!available ? (
        <p className="social-muted">平台登录暂不可用，请检查服务状态。</p>
      ) : (
        <>
          {error && (
            <p role="alert" className="social-error">
              {error}
            </p>
          )}
          <div className="social-browser-cards">
            {platforms.map((platform) => {
              const state = states[platform]
              return (
                <article className="social-browser-card" key={platform}>
                  <h4>{label(platform)}</h4>
                  <p role="status">{sessionMessage(state)}</p>
                  <div className="social-actions">
                    {active(state) ? (
                      <button
                        className="button"
                        type="button"
                        disabled={pending || state?.state === 'cancelling'}
                        onClick={() => void run(platform, 'cancel')}
                      >
                        {state?.state === 'cancelling' ? '正在关闭…' : '取消扫码'}
                      </button>
                    ) : normal(state) ? (
                      <button
                        className="button"
                        type="button"
                        disabled={pending}
                        onClick={() => void run(platform, 'logout')}
                      >
                        退出登录
                      </button>
                    ) : (
                      <button
                        className="button"
                        type="button"
                        disabled={pending || scanning || !state}
                        onClick={() => void run(platform, 'start')}
                      >
                        扫码登录
                      </button>
                    )}
                  </div>
                  {images[platform] && active(state) && (
                    <>
                      <p className="social-muted">
                        使用手机扫码并确认，完成后会自动收起。可点击画面中的官方扫码入口。
                      </p>
                      <button
                        type="button"
                        className="social-browser-screen"
                        disabled={pending}
                        aria-label={`操作${label(platform)}官方登录画面`}
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
                          alt={`${label(platform)}官方登录画面`}
                          draggable={false}
                        />
                      </button>
                    </>
                  )}
                </article>
              )
            })}
          </div>
        </>
      )}
    </div>
  )
}
