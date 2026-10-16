import { cva, type VariantProps } from 'class-variance-authority'
import { AlertTriangle, CircleAlert, Info } from 'lucide-react'
import type * as React from 'react'

import { cn } from '@/lib/utils'

const alertVariants = cva(
  'flex gap-3 rounded-(--radius-card) border p-4 text-sm leading-5',
  {
    variants: {
      variant: {
        info: 'border-(--color-border) bg-(--color-bg-subtle) text-(--color-fg)',
        warning: 'border-(--color-warning)/35 bg-(--color-warning)/10 text-(--color-fg)',
        danger: 'border-(--color-danger)/35 bg-(--color-danger)/10 text-(--color-fg)',
        success: 'border-(--color-success)/35 bg-(--color-success)/10 text-(--color-fg)',
      },
    },
    defaultVariants: { variant: 'info' },
  },
)

const icons = {
  info: Info,
  warning: AlertTriangle,
  danger: CircleAlert,
  success: Info,
} as const

export interface AlertProps
  extends React.HTMLAttributes<HTMLDivElement>,
    VariantProps<typeof alertVariants> {
  title?: string
  /** Replaces the default symbol where a more specific one says more. */
  icon?: React.ReactNode
}

export function Alert({
  className,
  variant = 'info',
  title,
  icon,
  children,
  ...props
}: AlertProps) {
  const Icon = icons[variant ?? 'info']
  return (
    <div
      role={variant === 'info' || variant === 'success' ? 'status' : 'alert'}
      className={cn(alertVariants({ variant }), className)}
      {...props}
    >
      <span
        className={cn(
          'mt-0.5 shrink-0 [&_svg]:size-4',
          variant === 'warning' && 'text-(--color-warning)',
          variant === 'danger' && 'text-(--color-danger)',
          variant === 'success' && 'text-(--color-success)',
          variant === 'info' && 'text-(--color-fg-muted)',
        )}
        aria-hidden="true"
      >
        {icon ?? <Icon />}
      </span>
      <div className="min-w-0 flex-1 space-y-1">
        {title ? <p className="font-semibold">{title}</p> : null}
        {children ? <div className="text-(--color-fg-muted)">{children}</div> : null}
      </div>
    </div>
  )
}
