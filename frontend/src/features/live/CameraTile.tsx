import type { ReactNode } from 'react'

import { cn } from '@/lib/utils'

interface CameraTileProps {
  name: string
  /** Resolution, or whatever else identifies this feed at a glance. */
  meta?: ReactNode
  /** The media area. Always drawn at 16:9 so the grid never reflows. */
  children: ReactNode
  className?: string
}

/**
 * One cell of the camera wall.
 *
 * Every tile is the same shape from the first paint: a 16:9 media area and a
 * two-line caption, both sized before any stream connects. Previously the
 * caption was a single row whose height depended on whether a resolution had
 * arrived yet, so a grid of cameras shuffled itself several times over
 * the first few seconds as feeds negotiated at different speeds.
 *
 * The name gets the full width and the resolution sits under it rather than
 * beside it. A camera called "Side gate — delivery and vehicle entrance" used to
 * be squeezed into whatever the resolution left over, which on a phone was
 * about nine characters; now it has the whole line, and the full text is still
 * available to a pointer and to assistive technology when it does not fit.
 */
export function CameraTile({ name, meta, children, className }: CameraTileProps) {
  return (
    <figure
      className={cn(
        'overflow-hidden rounded-(--radius-tile) border border-(--color-border)',
        'bg-(--color-surface)',
        className,
      )}
    >
      <div className="relative aspect-video bg-black">{children}</div>
      <figcaption className="px-3 py-2.5">
        <span className="block truncate text-[13px] leading-5 font-semibold" title={name}>
          {name}
        </span>
        <span className="tabular mt-px block h-4 truncate text-[11px] text-(--color-fg-muted)">
          {meta}
        </span>
      </figcaption>
    </figure>
  )
}

/** A tile-shaped placeholder, used while the camera list itself is loading. */
export function CameraTileSkeleton() {
  return (
    <div
      role="status"
      aria-busy="true"
      className="overflow-hidden rounded-(--radius-tile) border border-(--color-border) bg-(--color-surface)"
    >
      <div className="skeleton aspect-video" />
      <div className="space-y-1.5 px-3 py-2.5">
        <div className="skeleton h-3 w-2/5 rounded-full" />
        <div className="skeleton h-2.5 w-1/4 rounded-full" />
      </div>
      <span className="sr-only">Loading cameras…</span>
    </div>
  )
}
