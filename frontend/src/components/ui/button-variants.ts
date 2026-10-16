import { cva } from 'class-variance-authority'

/**
 * Kept out of button.tsx so that file exports only a component -- otherwise Vite
 * cannot hot-reload it (react-refresh/only-export-components).
 * Also lets a link or Radix trigger borrow the button styling via `asChild`.
 *
 * Every variant carries an `active:` state as well as `hover:`. A touch device
 * has no hover, so without it a tapped button gave no feedback at all until the
 * request it started came back -- and `-webkit-tap-highlight-color` is now
 * cleared globally, so the platform is no longer filling that gap either.
 */
export const buttonVariants = cva(
  'inline-flex items-center justify-center gap-2 whitespace-nowrap rounded-(--radius-control) ' +
    'text-sm font-semibold transition-[background-color,color,opacity] duration-(--duration-fast) ' +
    'disabled:pointer-events-none disabled:opacity-50 ' +
    'focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-(--color-accent) ' +
    '[&_svg]:pointer-events-none [&_svg]:size-4 [&_svg]:shrink-0',
  {
    variants: {
      variant: {
        primary: 'bg-(--color-accent) text-(--color-accent-fg) hover:opacity-90 active:opacity-80',
        secondary:
          'border border-(--color-border) bg-(--color-surface-raised) text-(--color-fg) ' +
          'hover:bg-(--color-surface-hover) active:bg-(--color-surface-hover)',
        ghost:
          'text-(--color-fg-muted) hover:bg-(--color-surface-hover) hover:text-(--color-fg) ' +
          'active:bg-(--color-surface-hover) active:text-(--color-fg)',
        danger: 'bg-(--color-danger) text-(--color-danger-fg) hover:opacity-90 active:opacity-80',
        link: 'text-(--color-accent) underline-offset-4 hover:underline',
      },
      size: {
        // Touch targets stay at least 44px tall on phones, while denser desktop
        // controls keep their original dimensions from the sm breakpoint up.
        sm: 'h-11 px-3 text-sm sm:h-8 sm:text-xs',
        md: 'h-11 px-4 sm:h-9',
        lg: 'h-12 px-6 text-base',
        icon: 'size-11 sm:size-9',
      },
    },
    defaultVariants: { variant: 'primary', size: 'md' },
  },
)
