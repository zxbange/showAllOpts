import { useEffect, useRef, useState } from 'react'
import { validationRequest, type ValidationAuth } from '../api/validation'

export function ValidationDeleteDialog({
  id,
  description,
  adminKey,
  onClose,
  onDeleted,
  onBusy,
}: {
  id: string
  description: string
  adminKey: ValidationAuth
  onClose: () => void
  onDeleted: () => void
  onBusy: (busy: boolean) => void
}) {
  const dialog = useRef<HTMLDialogElement>(null)
  const cancel = useRef<HTMLButtonElement>(null)
  const submitting = useRef(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  useEffect(() => {
    const element = dialog.current!
    element.showModal()
    cancel.current?.focus()
    return () => element.close()
  }, [])
  function dismiss() {
    dialog.current?.close()
    onClose()
  }
  async function remove() {
    if (submitting.current) return
    submitting.current = true
    setBusy(true)
    onBusy(true)
    setError('')
    try {
      await validationRequest(adminKey, '/jobs/' + id + '/delete', {})
      // Remove modal inertness before the parent restores keyboard focus.
      dialog.current?.close()
      onDeleted()
    } catch (e) {
      setError(e instanceof Error ? e.message : '删除未完成，请重试')
    } finally {
      submitting.current = false
      setBusy(false)
      onBusy(false)
    }
  }
  return (
    <dialog
      ref={dialog}
      className="validation-delete-dialog"
      aria-labelledby="validation-delete-title"
      aria-describedby="validation-delete-scope"
      onCancel={(event) => {
        event.preventDefault()
        if (!submitting.current) dismiss()
      }}
    >
      <h2 id="validation-delete-title">删除这条验证记录？</h2>
      <p className="validation-hint">{description}</p>
      <p className="validation-delete-id">任务：{id}</p>
      <p id="validation-delete-scope">
        此操作不可恢复。将清空该任务的记录、冻结输入、断点、逐股计算结果和聚合统计。
        源数据库行情、共享连接配置及其他任务不受影响。
      </p>
      {error && (
        <p className="validation-feedback" role="alert">
          {error}
        </p>
      )}
      <div className="validation-job-actions">
        <button ref={cancel} className="button button-quiet" disabled={busy} onClick={dismiss}>
          保留记录
        </button>
        <button className="button button-primary" disabled={busy} onClick={() => void remove()}>
          {busy ? '正在删除…' : '永久删除记录和数据'}
        </button>
      </div>
    </dialog>
  )
}
