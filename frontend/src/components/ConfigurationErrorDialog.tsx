import { useEffect, useRef } from 'react'

export function ConfigurationErrorDialog({
  message,
  onDismiss,
}: {
  message: string
  onDismiss?: () => void
}) {
  const dialog = useRef<HTMLDialogElement>(null)
  useEffect(() => {
    if (message && !dialog.current?.open) dialog.current?.showModal()
    if (!message && dialog.current?.open) dialog.current.close()
  }, [message])
  return (
    <dialog
      ref={dialog}
      className="configuration-error-dialog"
      aria-label="配置操作失败"
      onClose={onDismiss}
    >
      <h2>配置操作失败</h2>
      <p>{message}</p>
      <form method="dialog">
        <button className="button button-primary">知道了</button>
      </form>
    </dialog>
  )
}
