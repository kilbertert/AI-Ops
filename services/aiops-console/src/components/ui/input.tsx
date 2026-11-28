import type { InputHTMLAttributes } from 'react'

export type InputProps = InputHTMLAttributes<HTMLInputElement>

export function Input({ className = '', ...rest }: InputProps) {
  return (
    <input
      className={[
        'h-8 w-full rounded-lg border border-divider-regular bg-components-panel-bg px-3 system-sm-regular text-text-primary outline-none transition-colors placeholder:text-text-quaternary hover:border-components-input-border-hover focus:border-state-accent-solid disabled:cursor-not-allowed disabled:bg-components-input-bg-normal disabled:opacity-60',
        className,
      ]
        .filter(Boolean)
        .join(' ')}
      {...rest}
    />
  )
}
