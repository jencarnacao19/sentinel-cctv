/**
 * The text list of zones: the accessible alternative to the drawing.
 *
 * Everything the canvas can do to a zone as a whole -- rename it, change its
 * kind and sensitivity, enable or disable it, select it, delete it -- also
 * exists here as plain form controls, because a screen-reader user cannot see
 * a coloured polygon well enough to pick it out from a scene, and should not
 * have to. Editing the *shape* of a zone (moving or adding vertices) still
 * happens on the canvas; this list is where zones are managed, not drawn.
 */

import { Eye, EyeOff, Trash2 } from 'lucide-react'

import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Select } from '@/components/ui/select'
import type { EditorZone, ZoneKind } from '@/features/zones/types'
import { MAX_NAME_LENGTH } from '@/features/zones/zoneMath'
import { cn } from '@/lib/utils'

/** Sensitivity choices for a detection zone. "Camera" follows the camera's
 * own setting; the rest override it for this zone only. */
const SENSITIVITY_OPTIONS: { value: string; label: string }[] = [
  { value: 'camera', label: 'Camera setting' },
  { value: '10', label: 'Very low' },
  { value: '30', label: 'Low' },
  { value: '50', label: 'Medium' },
  { value: '70', label: 'High' },
  { value: '90', label: 'Very high' },
]

function sensitivityValue(sensitivity: number | null): string {
  if (sensitivity === null) return 'camera'
  const exact = SENSITIVITY_OPTIONS.find((option) => option.value === String(sensitivity))
  return exact ? exact.value : String(sensitivity)
}

interface ZoneListProps {
  zones: EditorZone[]
  activeZoneId: string | null
  drawingZoneId: string | null
  onSelectZone: (zoneId: string) => void
  onRenameZone: (zoneId: string, name: string) => void
  onToggleZone: (zoneId: string) => void
  onChangeKind: (zoneId: string, kind: ZoneKind) => void
  onChangeSensitivity: (zoneId: string, sensitivity: number | null) => void
  onDeleteZone: (zoneId: string) => void
}

export function ZoneList(props: ZoneListProps) {
  if (props.zones.length === 0) {
    return (
      <p className="text-sm text-(--color-fg-muted)">
        No zones yet. Use the button above the picture to draw the first one.
      </p>
    )
  }

  return (
    <ul className="space-y-2">
      {props.zones.map((zone) => {
        const isActive = zone.id === props.activeZoneId
        const isDrawing = zone.id === props.drawingZoneId
        const sensitivity = sensitivityValue(zone.sensitivity)
        const customSensitivity = !SENSITIVITY_OPTIONS.some(
          (option) => option.value === sensitivity,
        )
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
                  background: !zone.enabled
                    ? 'var(--color-fg-subtle)'
                    : zone.kind === 'detect'
                      ? 'var(--color-accent)'
                      : 'var(--color-motion)',
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
                maxLength={MAX_NAME_LENGTH}
                onChange={(event) => props.onRenameZone(zone.id, event.target.value)}
                className="h-8"
              />
            </div>

            <Label htmlFor={`zone-kind-${zone.id}`} className="sr-only">
              What {zone.name} does
            </Label>
            <Select
              id={`zone-kind-${zone.id}`}
              className="h-8 w-auto"
              value={zone.kind}
              onChange={(event) => props.onChangeKind(zone.id, event.target.value as ZoneKind)}
            >
              <option value="detect">Detect motion</option>
              <option value="ignore">Ignore</option>
            </Select>

            {zone.kind === 'detect' ? (
              <>
                <Label htmlFor={`zone-sensitivity-${zone.id}`} className="sr-only">
                  Sensitivity of {zone.name}
                </Label>
                <Select
                  id={`zone-sensitivity-${zone.id}`}
                  className="h-8 w-auto"
                  value={sensitivity}
                  title="Sensitivity: how small a movement this zone notices"
                  onChange={(event) =>
                    props.onChangeSensitivity(
                      zone.id,
                      event.target.value === 'camera' ? null : Number(event.target.value),
                    )
                  }
                >
                  {customSensitivity ? (
                    <option value={sensitivity}>Custom ({sensitivity})</option>
                  ) : null}
                  {SENSITIVITY_OPTIONS.map((option) => (
                    <option key={option.value} value={option.value}>
                      {option.value === 'camera' ? option.label : `Sensitivity: ${option.label}`}
                    </option>
                  ))}
                </Select>
              </>
            ) : null}

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
                  ? zone.kind === 'detect'
                    ? 'Watching this area for motion'
                    : 'Ignoring motion in this area'
                  : 'Kept, but not in use right now'
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
