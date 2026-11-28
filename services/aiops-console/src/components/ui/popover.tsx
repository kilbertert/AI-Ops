import type { ButtonHTMLAttributes, ReactNode } from 'react'
import { createContext, useContext, useEffect, useRef, useState } from 'react'

type PopoverCtxValue = {
  open: boolean
  setOpen: (open: boolean) => void
}

const PopoverCtx = createContext<PopoverCtxValue | null>(null)

export function Popover({
  open: controlledOpen,
  onOpenChange,
  children,
}: {
  open?: boolean
  onOpenChange?: (open: boolean) => void
  children: ReactNode
}) {
  const [uncontrolledOpen, setUncontrolledOpen] = useState(false)
  const open = controlledOpen !== undefined ? controlledOpen : uncontrolledOpen
  const ref = useRef<HTMLDivElement>(null)

  const setOpen = (next: boolean) => {
    if (controlledOpen === undefined) setUncontrolledOpen(next)
    onOpenChange?.(next)
  }

  useEffect(() => {
    if (!open) return
    const handleMouseDown = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) {
        setOpen(false)
      }
    }
    const handleKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setOpen(false)
    }
    document.addEventListener('mousedown', handleMouseDown)
    document.addEventListener('keydown', handleKey)
    return () => {
      document.removeEventListener('mousedown', handleMouseDown)
      document.removeEventListener('keydown', handleKey)
    }
  }, [open])

  return (
    <PopoverCtx.Provider value={{ open, setOpen }}>
      <div ref={ref} className="relative inline-block w-full">
        {children}
      </div>
    </PopoverCtx.Provider>
  )
}

export function PopoverTrigger({
  children,
  onClick,
  ...rest
}: ButtonHTMLAttributes<HTMLButtonElement> & { children: ReactNode }) {
  const ctx = useContext(PopoverCtx)
  return (
    <button
      type="button"
      onClick={(e) => {
        ctx?.setOpen(!ctx.open)
        onClick?.(e)
      }}
      {...rest}
    >
      {children}
    </button>
  )
}

export function PopoverContent({
  placement: _placement,
  sideOffset: _sideOffset,
  className = '',
  children,
}: {
  placement?: string
  sideOffset?: number
  className?: string
  children: ReactNode
}) {
  const ctx = useContext(PopoverCtx)
  if (!ctx?.open) return null
  return (
    <div
      className={[
        'absolute left-0 top-full z-40 mt-1.5 min-w-full rounded-xl border border-components-panel-border bg-components-panel-bg shadow-lg',
        className,
      ]
        .filter(Boolean)
        .join(' ')}
    >
      {children}
    </div>
  )
}
