/**
 * The text list of zones: the accessible alternative to the drawing.
 *
 * Everything the canvas can do to a zone as a whole -- rename it, enable or
 * disable it, select it, delete it -- also exists here as plain form
 * controls, because a screen-reader user cannot see a coloured polygon well
 * enough to pick it out from a scene, and should not have to. Editing the
 * *shape* of a zone (moving or adding vertices) still happens on the canvas;
 * this list is where zones are managed, not drawn.
 */

import { Eye, EyeOff, Trash2 } from 'lucide-react'

import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import type { EditorZone } from '@/features/zones/types'
import { cn } from '@/lib/utils'

interface ZoneListProps {
  zones: EditorZone[]
  activeZoneId: string | null
  drawingZoneId: string | null
  onSelectZone: (zoneId: string) => void
  onRenameZone: (zoneId: string, name: string) => void
  onToggleZone: (zoneId: string) => void
  onDeleteZone: (zoneId: string) => void
}

export function ZoneList(props: ZoneListProps) {
  if (props.zones.length === 0) {
    return (
      <p className="text-sm text-(--color-fg-muted)">
        No zones yet. Use "New zone" above to draw the first one.
      </p>
    )
  }

  return (
    <ul className="space-y-2">
      {props.zones.map((zone) => {
        const isActive = zone.id === props.activeZoneId
        const isDrawing = zone.id === props.drawingZoneId
        return (
          <li
            key={zone.id}
            className={cn(
              'flex flex-wrap items-center gap-2 rounded-(--radius-card) border p-3',
              isActive ? 'border-(--color-accent)' : 'border-(--color-border)',
            )}
          >
            <button
              type="button"
              onClick={() => props.onSelectZone(zone.id)}
              aria-pressed={isActive}
              aria-label={`Select ${zone.name} to highlight it on the canvas`}
              className="flex size-8 shrink-0 items-center justify-center rounded-full border border-(--color-border) focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-(--color-accent)"
            >
              <span
                className="size-3 rounded-full"
                style={{
                  background: zone.enabled ? 'var(--color-motion)' : 'var(--color-fg-subtle)',
                }}
                aria-hidden="true"
              />
            </button>

            <div className="min-w-32 flex-1">
              <Label htmlFor={`zone-name-${zone.id}`} className="sr-only">
                Name for this zone
              </Label>
              <Input
                id={`zone-name-${zone.id}`}
                value={zone.name}
                onChange={(event) => props.onRenameZone(zone.id, event.target.value)}
                className="h-8"
              />
            </div>

            <span className="tabular text-xs whitespace-nowrap text-(--color-fg-muted)">
              {zone.points.length} point{zone.points.length === 1 ? '' : 's'}
              {isDrawing ? ' · drawing' : ''}
            </span>

            <Button
              type="button"
              variant="ghost"
              size="sm"
              aria-pressed={zone.enabled}
              onClick={() => props.onToggleZone(zone.id)}
              title={
                zone.enabled
                  ? 'Suppressing motion in this area'
                  : 'Kept, but not suppressing motion right now'
              }
            >
              {zone.enabled ? <Eye aria-hidden="true" /> : <EyeOff aria-hidden="true" />}
              {zone.enabled ? 'Enabled' : 'Disabled'}
            </Button>

            <Button
              type="button"
              variant="ghost"
              size="icon"
              aria-label={`Delete ${zone.name}`}
              onClick={() => props.onDeleteZone(zone.id)}
            >
              <Trash2 aria-hidden="true" />
            </Button>
          </li>
        )
      })}
    </ul>
  )
}
