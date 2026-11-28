export type StatusDotStatus =
  | 'success'
  | 'warning'
  | 'error'
  | 'default'
  | 'normal'
  | 'disabled'
  | 'green'
  | 'yellow'
  | 'red'
  | 'gray'

export type StatusDotProps = {
  status?: StatusDotStatus
  size?: 'small' | 'medium'
  className?: string
}

const DOT_COLOR: Record<string, string> = {
  success: 'bg-state-success-solid',
  green: 'bg-state-success-solid',
  warning: 'bg-state-warning-solid',
  yellow: 'bg-state-warning-solid',
  error: 'bg-state-destructive-solid',
  red: 'bg-state-destructive-solid',
  normal: 'bg-state-accent-solid',
  disabled: 'bg-text-quaternary',
  default: 'bg-text-quaternary',
  gray: 'bg-text-quaternary',
}

export function StatusDot({ status = 'default', size = 'medium', className = '' }: StatusDotProps) {
  const dim = size === 'small' ? 'size-1.5' : 'size-2'
  const color = DOT_COLOR[status] ?? DOT_COLOR.default
  return <span aria-hidden className={['inline-block shrink-0 rounded-full', dim, color, className].join(' ')} />
}
