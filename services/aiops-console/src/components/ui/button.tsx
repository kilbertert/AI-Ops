import type { ButtonHTMLAttributes, ReactNode } from 'react'

export type ButtonProps = ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: 'primary' | 'secondary' | 'secondary-accent' | 'ghost'
  size?: 'small' | 'medium' | 'large'
  tone?: 'default' | 'destructive'
  children?: ReactNode
}

const SIZE_CLASSES: Record<NonNullable<ButtonProps['size']>, string> = {
  small: 'h-7 px-2.5 text-[12px] leading-4 rounded-lg',
  medium: 'h-8 px-3.5 text-[13px] leading-4 rounded-lg',
  large: 'h-9 px-4 text-[14px] leading-5 rounded-[10px]',
}

export function Button({
  variant = 'secondary',
  size = 'medium',
  tone = 'default',
  type = 'button',
  className = '',
  children,
  ...rest
}: ButtonProps) {
  let styleClass =
    'bg-components-button-secondary-bg text-components-button-secondary-text border border-divider-deep hover:bg-components-button-secondary-bg-hover'

  if (variant === 'primary' && tone === 'destructive') {
    styleClass =
      'bg-state-destructive-solid text-white border border-transparent hover:opacity-90'
  } else if (variant === 'primary') {
    styleClass =
      'bg-components-button-primary-bg text-white border border-transparent hover:bg-components-button-primary-bg-hover'
  } else if (variant === 'secondary-accent') {
    styleClass =
      'bg-state-accent-hover text-text-accent border border-divider-accent hover:opacity-90'
  } else if (variant === 'ghost' && tone === 'destructive') {
    styleClass =
      'bg-transparent text-text-destructive border border-transparent hover:bg-state-destructive-hover'
  } else if (variant === 'ghost') {
    styleClass =
      'bg-transparent text-text-secondary border border-transparent hover:bg-components-button-ghost-bg-hover'
  } else if (tone === 'destructive') {
    styleClass =
      'bg-state-destructive-hover text-text-destructive border border-state-destructive-border hover:opacity-90'
  }

  return (
    <button
      type={type}
      className={[
        'inline-flex shrink-0 cursor-pointer items-center justify-center gap-1.5 font-medium transition-colors disabled:cursor-not-allowed disabled:opacity-50',
        SIZE_CLASSES[size],
        styleClass,
        className,
      ]
        .filter(Boolean)
        .join(' ')}
      {...rest}
    >
      {children}
    </button>
  )
}
