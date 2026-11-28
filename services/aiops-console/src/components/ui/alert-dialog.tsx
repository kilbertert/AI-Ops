import type { ReactNode } from 'react'
import { createContext, useContext } from 'react'
import { Button } from './button'

type AlertDialogCtxValue = {
  open: boolean
  onOpenChange?: (open: boolean) => void
}

const AlertDialogCtx = createContext<AlertDialogCtxValue | null>(null)

export function AlertDialog({
  open,
  onOpenChange,
  children,
}: {
  open: boolean
  onOpenChange?: (open: boolean) => void
  children: ReactNode
}) {
  if (!open) return null
  return (
    <AlertDialogCtx.Provider value={{ open, onOpenChange }}>
      <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 p-4 backdrop-blur-xs">
        {children}
      </div>
    </AlertDialogCtx.Provider>
  )
}

export function AlertDialogContent({ children }: { children: ReactNode }) {
  return (
    <div
      role="alertdialog"
      aria-modal="true"
      className="w-full max-w-md space-y-3 rounded-2xl border border-components-panel-border bg-components-panel-bg p-6 shadow-xl"
    >
      {children}
    </div>
  )
}

export function AlertDialogTitle({ children }: { children: ReactNode }) {
  return <h2 className="system-lg-semibold text-text-primary">{children}</h2>
}

export function AlertDialogDescription({ children }: { children: ReactNode }) {
  return <p className="system-sm-regular text-text-secondary">{children}</p>
}

export function AlertDialogActions({ children }: { children: ReactNode }) {
  return <div className="flex justify-end gap-2 pt-2">{children}</div>
}

export function AlertDialogCancelButton({ children }: { children: ReactNode }) {
  const ctx = useContext(AlertDialogCtx)
  return (
    <Button variant="secondary" onClick={() => ctx?.onOpenChange?.(false)}>
      {children}
    </Button>
  )
}

export function AlertDialogConfirmButton({
  onClick,
  children,
}: {
  onClick?: () => void
  children: ReactNode
}) {
  const ctx = useContext(AlertDialogCtx)
  return (
    <Button
      variant="primary"
      onClick={() => {
        onClick?.()
        ctx?.onOpenChange?.(false)
      }}
    >
      {children}
    </Button>
  )
}
