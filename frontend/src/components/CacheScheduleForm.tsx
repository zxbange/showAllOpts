import { useEffect, useState } from 'react'
import { saveCacheSchedule } from '../api/client'
import type { CacheSchedule } from '../types/cache'

export function CacheScheduleForm({
  schedule,
  autoRefresh,
  onSaved,
}: {
  schedule: CacheSchedule
  autoRefresh: boolean
  onSaved: (schedule: CacheSchedule) => void
}) {
  const [time, setTime] = useState(schedule.daily_time)
  const [dirty, setDirty] = useState(false)
  const [saving, setSaving] = useState(false)
  const [message, setMessage] = useState('')
  const [failed, setFailed] = useState(false)
  useEffect(() => {
    if (!dirty) setTime(schedule.daily_time)
  }, [schedule.daily_time, dirty])
  async function save() {
    if (saving) return
    setMessage('')
    setFailed(false)
    if (!/^(?:[01][0-9]|2[0-3]):[0-5][0-9]$/.test(time)) {
      setFailed(true)
      setMessage('请输入有效的 HH:mm 时间（00:00–23:59）。')
      return
    }
    setSaving(true)
    try {
      const result = await saveCacheSchedule(time)
      onSaved(result)
      setDirty(false)
      setMessage('同步时间已保存，不会立即触发同步。')
    } catch (reason) {
      setFailed(true)
      setMessage(reason instanceof Error ? reason.message : '无法保存同步时间，请稍后重试。')
    } finally {
      setSaving(false)
    }
  }
  return (
    <form
      className="cache-form cache-schedule"
      onSubmit={(event) => {
        event.preventDefault()
        void save()
      }}
    >
      <strong>自动同步时间</strong>
      <p>
        当前设置：每天 {schedule.daily_time}，北京时间（{schedule.timezone}）。
      </p>
      {autoRefresh ? (
        schedule.next_refresh_at && (
          <p>
            下次计划：
            {new Date(schedule.next_refresh_at).toLocaleString('zh-CN', {
              timeZone: 'Asia/Shanghai',
              hour12: false,
            })}
          </p>
        )
      ) : (
        <p>自动同步已关闭；保存时间不会开启自动同步。</p>
      )}
      <div className="cache-fields">
        <label>
          每日同步时间（北京时间）
          <input
            type="time"
            step="60"
            required
            value={time}
            disabled={saving}
            onChange={(event) => {
              setTime(event.target.value)
              setDirty(true)
              setMessage('')
            }}
          />
        </label>
      </div>
      <button type="submit" className="cache-submit" disabled={saving}>
        {saving ? '正在保存…' : '保存同步时间'}
      </button>
      <p>
        保存后按新的时间执行，正在进行的同步继续。本日已启动过自动同步时，下次从明天开始；服务重启不补跑错过的时段。
      </p>
      {message && (
        <p className="cache-message" role={failed ? 'alert' : 'status'}>
          {message}
        </p>
      )}
    </form>
  )
}
