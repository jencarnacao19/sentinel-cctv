import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ArrowDown, ArrowUp } from 'lucide-react'
import { useState } from 'react'

import { queryKeys, wallApi } from '@/api/endpoints'
import { PageHeader } from '@/components/layout/PageHeader'
import { Alert } from '@/components/ui/alert'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { CheckboxField } from '@/components/ui/checkbox'
import { Label } from '@/components/ui/label'
import { Select } from '@/components/ui/select'
import { useCameras } from '@/features/cameras/useCameras'
import { arrangeCameras } from '@/features/live/useWall'
import { viewerApi } from '@/features/viewer/access'
import { errorText } from '@/lib/errors'
import type { WallLayout } from '@/types/api'

const GRIDS: { value: WallLayout['columns']; label: string }[] = [
  { value: 'auto', label: 'Automatic: the fewest columns that fit every camera' },
  { value: '1', label: '1 per row' },
  { value: '2x2', label: '2 per row' },
  { value: '3x3', label: '3 per row' },
  { value: '4x4', label: '4 per row' },
]

/**
 * Which cameras the wall shows, in what order, and its default grid.
 *
 * Arrows rather than dragging: they work the same with a thumb, a mouse and a
 * keyboard. Nothing is saved until Save, so a reorder can be abandoned.
 */
export function WallPage() {
  const queryClient = useQueryClient()
  const cameras = useCameras(true)
  const wall = useQuery({
    queryKey: queryKeys.wall,
    queryFn: () => viewerApi.get<WallLayout>('/viewer/wall'),
  })
  const [draft, setDraft] = useState<WallLayout | null>(null)
  const current = draft ?? wall.data
  const ordered = current
    ? arrangeCameras(cameras.data ?? [], { ...current, hidden: [] })
    : (cameras.data ?? [])
  const hidden = new Set(current?.hidden ?? [])

  const save = useMutation({
    mutationFn: wallApi.save,
    onSuccess: (saved) => {
      queryClient.setQueryData(queryKeys.wall, saved)
      setDraft(null)
    },
  })

  if (!current) {
    return (
      <div className="mx-auto max-w-3xl space-y-6">
        <PageHeader title="Camera wall" />
        {wall.isError ? <Alert variant="danger">Could not load the camera wall.</Alert> : null}
      </div>
    )
  }

  const move = (index: number, delta: number) => {
    const ids = ordered.map((camera) => camera.id)
    const target = index + delta
    ;[ids[index], ids[target]] = [ids[target], ids[index]]
    setDraft({ ...current, order: ids })
  }
  const toggle = (id: string, show: boolean) =>
    setDraft({
      ...current,
      order: ordered.map((camera) => camera.id),
      hidden: show ? current.hidden.filter((other) => other !== id) : [...current.hidden, id],
    })

  return (
    <div className="mx-auto max-w-3xl space-y-6">
      <PageHeader
        title="Camera wall"
        description="What every wall shows: the Home Assistant panel, the viewer page and Live."
      />

      <Card>
        <CardHeader>
          <CardTitle>Default grid</CardTitle>
          <CardDescription>
            Used wherever the wall is set to Default. Anyone watching can pick another grid on
            their own device.
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-2">
          <Label htmlFor="wall-grid">Cameras per row</Label>
          <Select
            id="wall-grid"
            value={current.columns}
            onChange={(event) =>
              setDraft({ ...current, columns: event.target.value as WallLayout['columns'] })
            }
          >
            {GRIDS.map((grid) => (
              <option key={grid.value} value={grid.value}>
                {grid.label}
              </option>
            ))}
          </Select>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Cameras</CardTitle>
          <CardDescription>
            A hidden camera keeps recording and stays in playback; it only leaves the wall.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <ol className="divide-y divide-(--color-border)">
            {ordered.map((camera, index) => (
              <li key={camera.id} className="flex items-center gap-2 py-1">
                <CheckboxField
                  className="min-w-0 flex-1"
                  label={camera.name}
                  checked={!hidden.has(camera.id)}
                  onChange={(event) => toggle(camera.id, event.target.checked)}
                />
                <Button
                  variant="ghost"
                  size="icon"
                  aria-label={`Move ${camera.name} up`}
                  disabled={index === 0}
                  onClick={() => move(index, -1)}
                >
                  <ArrowUp aria-hidden="true" />
                </Button>
                <Button
                  variant="ghost"
                  size="icon"
                  aria-label={`Move ${camera.name} down`}
                  disabled={index === ordered.length - 1}
                  onClick={() => move(index, 1)}
                >
                  <ArrowDown aria-hidden="true" />
                </Button>
              </li>
            ))}
          </ol>
        </CardContent>
      </Card>

      {errorText(save.error, 'Could not save the camera wall.') ? (
        <Alert variant="danger">{errorText(save.error, 'Could not save the camera wall.')}</Alert>
      ) : null}
      <div className="flex justify-end gap-2">
        <Button variant="ghost" disabled={!draft} onClick={() => setDraft(null)}>
          Discard changes
        </Button>
        <Button disabled={!draft} loading={save.isPending} onClick={() => draft && save.mutate(draft)}>
          Save
        </Button>
      </div>
    </div>
  )
}
