import { Alert } from '@/components/ui/alert'
import { Label } from '@/components/ui/label'
import { Select } from '@/components/ui/select'
import { Switch } from '@/components/ui/switch'
import {
  useSaveTapoAutoRecording,
  useTapoAutoRecording,
  useTimedRecordingOptions,
} from '@/features/cameras/useTimedRecording'
import { errorText } from '@/lib/errors'
import type { Camera, TapoAutoRecording } from '@/types/api'

const FAILED = 'The automatic recording settings could not be saved.'

interface Props {
  camera: Camera
  open: boolean
}

type Rules = Omit<TapoAutoRecording, 'on_wake_supported'>

/**
 * When this battery camera records without anyone pressing Start.
 *
 * Each change is saved at once. Both start the same bounded recording as the
 * panel above, so the battery cost is stated here rather than confirmed per
 * recording: nobody is there to confirm when the camera wakes at night.
 */
export function AutoRecordingPanel({ camera, open }: Props) {
  const rules = useTapoAutoRecording(camera.id, open)
  const options = useTimedRecordingOptions(camera.id, open)
  const save = useSaveTapoAutoRecording(camera.id)
  const value = rules.data
  const failure = errorText(save.error, FAILED) ?? errorText(rules.error, FAILED)

  function update(change: Partial<Rules>) {
    if (!value) return
    const { on_wake_supported: _, ...current } = value
    void save.mutateAsync({ ...current, ...change }).catch(() => undefined)
  }

  function durations(current: number): number[] {
    const presets = options.data?.presets_minutes ?? [current]
    return [...new Set([...presets, current])].sort((a, b) => a - b)
  }

  return (
    <section className="space-y-3 rounded-lg border border-(--color-border) p-3">
      <div>
        <h3 className="text-sm font-semibold">Automatic recording</h3>
        <p className="mt-1 text-xs text-(--color-fg-muted)">
          Each recording keeps the camera awake for the chosen time, so the battery lasts weeks
          rather than months.
        </p>
      </div>

      {failure ? <Alert variant="danger">{failure}</Alert> : null}

      {value ? (
        <div className="divide-y divide-(--color-border)">
          <Rule
            id={`auto-wake-${camera.id}`}
            title="When it wakes up by itself"
            description={
              value.on_wake_supported
                ? 'Records when its motion sensor wakes it for more than a few seconds. The first seconds of the motion are not in the clip.'
                : 'Not available behind a hub: the hub is always awake, so Sentinel cannot see the camera wake.'
            }
            checked={value.on_wake}
            minutes={value.on_wake_minutes}
            durations={durations(value.on_wake_minutes)}
            disabled={save.isPending || !value.on_wake_supported}
            onChecked={(on_wake) => update({ on_wake })}
            onMinutes={(on_wake_minutes) => update({ on_wake_minutes })}
          />
          <Rule
            id={`auto-motion-${camera.id}`}
            title="When another camera sees motion"
            description="Wakes the camera and records. Nothing new starts while a live view or a recording is open."
            checked={value.on_motion}
            minutes={value.on_motion_minutes}
            durations={durations(value.on_motion_minutes)}
            disabled={save.isPending}
            onChecked={(on_motion) => update({ on_motion })}
            onMinutes={(on_motion_minutes) => update({ on_motion_minutes })}
          />
        </div>
      ) : rules.isPending ? (
        <p className="text-xs text-(--color-fg-muted)">Loading…</p>
      ) : null}
    </section>
  )
}

function Rule({
  id,
  title,
  description,
  checked,
  minutes,
  durations,
  disabled,
  onChecked,
  onMinutes,
}: {
  id: string
  title: string
  description: string
  checked: boolean
  minutes: number
  durations: number[]
  disabled: boolean
  onChecked: (value: boolean) => void
  onMinutes: (value: number) => void
}) {
  return (
    <div className="space-y-2 py-3 first:pt-0 last:pb-0">
      <div className="flex items-start justify-between gap-3">
        <div>
          <Label htmlFor={id}>{title}</Label>
          <p className="mt-1 text-xs text-(--color-fg-muted)">{description}</p>
        </div>
        <Switch id={id} checked={checked} disabled={disabled} onCheckedChange={onChecked} />
      </div>
      <div className="flex items-center gap-2">
        <Label htmlFor={`${id}-minutes`} className="shrink-0 text-xs text-(--color-fg-muted)">
          Record for
        </Label>
        <div className="w-32">
          <Select
            id={`${id}-minutes`}
            value={minutes}
            disabled={disabled}
            onChange={(event) => onMinutes(Number(event.target.value))}
          >
            {durations.map((value) => (
              <option key={value} value={value}>
                {value} min
              </option>
            ))}
          </Select>
        </div>
      </div>
    </div>
  )
}
