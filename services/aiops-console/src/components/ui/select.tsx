import type { ReactNode } from 'react'
import { createContext, useCallback, useContext, useEffect, useId, useMemo, useRef, useState } from 'react'

type SelectCtxValue = {
  value: string | null
  open: boolean
  setOpen: (open: boolean) => void
  onValueChange?: (value: string) => void
  labels: Map<string, string>
  registerLabel: (val: string, label: string) => void
  listboxId: string
}

const SelectCtx = createContext<SelectCtxValue | null>(null)

function extractText(node: ReactNode): string {
  if (typeof node === 'string' || typeof node === 'number') return String(node)
  if (Array.isArray(node)) return node.map(extractText).join('')
  if (node && typeof node === 'object' && 'props' in node) {
    const props = (node as { props?: { children?: ReactNode } }).props
    return extractText(props?.children)
  }
  return ''
}

export function Select({
  value,
  onValueChange,
  children,
}: {
  value: string | null
  onValueChange?: (value: string) => void
  children: ReactNode
}) {
  const [open, setOpen] = useState(false)
  const [labels, setLabels] = useState<Map<string, string>>(() => new Map())
  const containerRef = useRef<HTMLDivElement>(null)
  const listboxId = useId()

  useEffect(() => {
    if (!open) return
    const onPointerDown = (e: MouseEvent) => {
      if (containerRef.current && !containerRef.current.contains(e.target as Node)) {
        setOpen(false)
      }
    }
    const onKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setOpen(false)
    }
    document.addEventListener('mousedown', onPointerDown)
    document.addEventListener('keydown', onKeyDown)
    return () => {
      document.removeEventListener('mousedown', onPointerDown)
      document.removeEventListener('keydown', onKeyDown)
    }
  }, [open])

  const registerLabel = useCallback((val: string, label: string) => {
    if (!label) return
    setLabels((prev) => {
      if (prev.get(val) === label) return prev
      const next = new Map(prev)
      next.set(val, label)
      return next
    })
  }, [])

  const ctxValue = useMemo<SelectCtxValue>(
    () => ({ value, open, setOpen, onValueChange, labels, registerLabel, listboxId }),
    [value, open, onValueChange, labels, registerLabel, listboxId],
  )

  return (
    <SelectCtx.Provider value={ctxValue}>
      <div ref={containerRef} className="relative">
        {children}
      </div>
    </SelectCtx.Provider>
  )
}

export function SelectTrigger({
  size = 'medium',
  className = '',
  children,
  ...rest
}: {
  size?: 'small' | 'medium' | 'large'
  className?: string
  'aria-label'?: string
  children: ReactNode
}) {
  const ctx = useContext(SelectCtx)
  const hClass = size === 'large' ? 'h-9 px-3' : size === 'small' ? 'h-7 px-2.5' : 'h-8 px-3'
  return (
    <button
      type="button"
      role="combobox"
      aria-expanded={ctx?.open ?? false}
      aria-controls={ctx?.listboxId}
      onClick={() => ctx?.setOpen(!ctx.open)}
      className={[
        'flex w-full cursor-pointer items-center justify-between gap-2 rounded-lg border border-divider-regular bg-components-panel-bg system-sm-regular text-text-primary transition-colors hover:border-components-input-border-hover',
        hClass,
        className,
      ]
        .filter(Boolean)
        .join(' ')}
      {...rest}
    >
      <span className="min-w-0 flex-1 truncate text-left">{children}</span>
      <span aria-hidden className="shrink-0 text-text-tertiary">▾</span>
    </button>
  )
}

export function SelectValue({
  placeholder = '',
  children,
}: {
  placeholder?: string
  children?: ReactNode
}) {
  const ctx = useContext(SelectCtx)
  if (!ctx || !ctx.value) {
    return <span className="text-text-quaternary">{placeholder}</span>
  }
  return <span>{children ?? ctx.labels.get(ctx.value) ?? ctx.value}</span>
}

export function SelectContent({
  className = '',
  children,
}: {
  className?: string
  children: ReactNode
}) {
  const ctx = useContext(SelectCtx)
  return (
    <div
      id={ctx?.listboxId}
      role="listbox"
      hidden={!ctx?.open}
      className={[
        ctx?.open ? '' : 'hidden',
        'absolute left-0 top-full z-40 mt-1 max-h-60 min-w-full overflow-y-auto rounded-xl border border-components-panel-border bg-components-panel-bg p-1 shadow-lg',
        className,
      ]
        .filter(Boolean)
        .join(' ')}
    >
      {children}
    </div>
  )
}

export function SelectItem({ value, children }: { value: string; children: ReactNode }) {
  const ctx = useContext(SelectCtx)
  const selected = ctx?.value === value
  const textLabel = extractText(children).trim()
  useEffect(() => {
    if (textLabel) ctx?.registerLabel(value, textLabel)
  }, [ctx, value, textLabel])

  return (
    <button
      type="button"
      role="option"
      aria-selected={selected}
      data-value={value}
      onClick={() => {
        ctx?.onValueChange?.(value)
        ctx?.setOpen(false)
      }}
      className={[
        'flex w-full cursor-pointer items-center justify-between gap-2 rounded-lg px-2.5 py-1.5 text-left system-sm-regular transition-colors',
        selected
          ? 'bg-state-accent-hover text-text-accent system-sm-medium'
          : 'text-text-primary hover:bg-components-panel-on-panel-item-bg-hover',
      ].join(' ')}
    >
      <span className="min-w-0 flex-1 truncate">{children}</span>
      {selected ? <span aria-hidden className="shrink-0 text-text-accent">✓</span> : null}
    </button>
  )
}

export function SelectItemText({ children }: { children: ReactNode }) {
  return <span>{children}</span>
}

export function SelectItemIndicator() {
  return null
}
