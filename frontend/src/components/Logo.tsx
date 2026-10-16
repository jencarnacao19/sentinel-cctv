import { cn } from '@/lib/utils'

/**
 * Sentinel mark: a shield outline whose negative space forms an aperture iris --
 * protection plus lens, drawn from primitives so it stays crisp at 16px.
 */
export function Logo({ className }: { className?: string }) {
  return (
    <svg
      viewBox="0 0 32 32"
      fill="none"
      role="img"
      aria-label="Sentinel CCTV"
      className={cn('size-7', className)}
    >
      <path
        d="M16 2.5 5.5 6.6v8.2c0 6.9 4.4 12.9 10.5 14.7 6.1-1.8 10.5-7.8 10.5-14.7V6.6L16 2.5Z"
        className="fill-(--color-accent)/12 stroke-(--color-accent)"
        strokeWidth="1.8"
        strokeLinejoin="round"
      />
      <circle
        cx="16"
        cy="14.5"
        r="5.2"
        className="stroke-(--color-accent)"
        strokeWidth="1.8"
      />
      <circle cx="16" cy="14.5" r="1.9" className="fill-(--color-accent)" />
    </svg>
  )
}
