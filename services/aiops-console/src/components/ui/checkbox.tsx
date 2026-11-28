export type CheckboxProps = {
  checked?: boolean
  onCheckedChange?: (checked: boolean) => void
  disabled?: boolean
  'aria-label'?: string
  id?: string
  className?: string
}

export function Checkbox({
  checked = false,
  onCheckedChange,
  disabled = false,
  className = '',
  ...rest
}: CheckboxProps) {
  return (
    <input
      type="checkbox"
      checked={checked}
      disabled={disabled}
      onChange={(e) => onCheckedChange?.(e.target.checked)}
      className={[
        'size-4 shrink-0 cursor-pointer rounded border border-divider-deep accent-state-accent-solid disabled:cursor-not-allowed disabled:opacity-50',
        className,
      ]
        .filter(Boolean)
        .join(' ')}
      {...rest}
    />
  )
}
