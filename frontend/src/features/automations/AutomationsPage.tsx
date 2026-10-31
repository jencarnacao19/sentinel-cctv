import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Plus, Trash2, Undo2 } from 'lucide-react'
import { useState } from 'react'

import { automationsApi, queryKeys } from '@/api/endpoints'
import { PageHeader } from '@/components/layout/PageHeader'
import { Alert } from '@/components/ui/alert'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { CheckboxField } from '@/components/ui/checkbox'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Select } from '@/components/ui/select'
import { useCameras } from '@/features/cameras/useCameras'
import { useMotionZones } from '@/features/zones/useMotionZones'
import { errorText } from '@/lib/errors'
import type { AutomationRecipe, AutomationsState, AutomationTarget } from '@/types/api'

type Kind = AutomationRecipe['kind']

const KINDS: { value: Kind; label: string }[] = [
  { value: 'motion_alert', label: 'Motion → notify phones' },
  { value: 'system_alert', label: 'Problem → notify phones' },
  { value: 'motion_actions', label: 'Motion → lights or alarm' },
  { value: 'presence_recording', label: 'Nobody home → record indoor cameras' },
]
const WHEN: { value: AutomationRecipe['when']; label: string }[] = [
  { value: 'always', label: 'Always' },
  { value: 'night', label: 'At night (sunset to sunrise)' },
  { value: 'away', label: 'When nobody is home' },
  { value: 'alarm_armed', label: 'When the alarm is armed' },
]

function blank(kind: Kind): AutomationRecipe {
  return {
    id: crypto.randomUUID().replaceAll('-', '').slice(0, 16),
    kind,
    name: '',
    camera_id: null,
    zone_id: null,
    camera_ids: [],
    notify: [],
    lights: [],
    when: kind === 'motion_actions' ? 'night' : 'always',
    alarm_entity: null,
    image: true,
    cooldown_minutes: 5,
    minutes: 5,
    trigger_alarm: false,
    storage_below_gb: 20,
    message: '',
    reclaim: 0,
  }
}

function toggled(list: string[], id: string, on: boolean): string[] {
  return on ? [...list, id] : list.filter((item) => item !== id)
}

function Picks({
  legend,
  options,
  value,
  onChange,
}: {
  legend: string
  options: AutomationTarget[]
  value: string[]
  onChange: (next: string[]) => void
}) {
  return (
    <fieldset className="space-y-1">
      <legend className="text-sm font-medium">{legend}</legend>
      {options.length === 0 ? (
        <p className="text-xs text-(--color-fg-muted)">Home Assistant reported none.</p>
      ) : (
        options.map((option) => (
          <CheckboxField
            key={option.id}
            label={option.name}
            checked={value.includes(option.id)}
            onChange={(event) => onChange(toggled(value, option.id, event.target.checked))}
          />
        ))
      )}
    </fieldset>
  )
}

function StatusBadge({ status }: { status: AutomationsState['status'][string] | undefined }) {
  if (!status) return <Badge>Waiting for Home Assistant</Badge>
  const look = {
    created: ['success', 'Created in Home Assistant'],
    edited_in_ha: ['warning', 'Edited in Home Assistant'],
    refused: ['danger', 'Not created'],
    waiting_permission: ['warning', 'Waiting for permission'],
    unsupported: ['danger', 'Not possible here'],
  } as const
  const [variant, label] = look[status.state]
  return <Badge variant={variant}>{label}</Badge>
}

function NewRecipe({
  targets,
  onAdd,
  saving,
}: {
  targets: NonNullable<AutomationsState['targets']>
  onAdd: (recipe: AutomationRecipe) => void
  saving: boolean
}) {
  const cameras = useCameras(true)
  const [recipe, setRecipe] = useState<AutomationRecipe>(() => blank('motion_alert'))
  const zones = useMotionZones(recipe.camera_id ?? '')
  const set = (patch: Partial<AutomationRecipe>) => setRecipe({ ...recipe, ...patch })
  const kind = recipe.kind
  const motion = kind === 'motion_alert' || kind === 'motion_actions'
  const rtsp = (cameras.data ?? []).filter((camera) => camera.kind === 'rtsp')
  const detection = (zones.data?.zones ?? []).filter(
    (zone) => zone.kind === 'detect' && zone.enabled && zone.saved,
  )
  const alarmNeeded = recipe.when === 'alarm_armed' || recipe.trigger_alarm

  return (
    <Card>
      <CardHeader>
        <CardTitle>New automation</CardTitle>
        <CardDescription>The lists come from your Home Assistant.</CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        <div className="space-y-2">
          <Label htmlFor="recipe-kind">What it does</Label>
          <Select
            id="recipe-kind"
            value={kind}
            onChange={(event) => setRecipe(blank(event.target.value as Kind))}
          >
            {KINDS.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </Select>
        </div>
        <div className="space-y-2">
          <Label htmlFor="recipe-name">Name</Label>
          <Input
            id="recipe-name"
            value={recipe.name}
            placeholder="Gate at night"
            onChange={(event) => set({ name: event.target.value })}
          />
        </div>

        {motion ? (
          <div className="grid gap-4 sm:grid-cols-2">
            <div className="space-y-2">
              <Label htmlFor="recipe-camera">Camera</Label>
              <Select
                id="recipe-camera"
                value={recipe.camera_id ?? ''}
                onChange={(event) => set({ camera_id: event.target.value || null, zone_id: null })}
              >
                <option value="">Choose a camera</option>
                {rtsp.map((camera) => (
                  <option key={camera.id} value={camera.id}>
                    {camera.name}
                  </option>
                ))}
              </Select>
            </div>
            <div className="space-y-2">
              <Label htmlFor="recipe-zone">Where</Label>
              <Select
                id="recipe-zone"
                value={recipe.zone_id ?? ''}
                disabled={!recipe.camera_id}
                onChange={(event) => set({ zone_id: event.target.value || null })}
              >
                <option value="">The whole camera</option>
                {detection.map((zone) => (
                  <option key={zone.id} value={zone.id}>
                    {zone.name}
                  </option>
                ))}
              </Select>
            </div>
            <div className="space-y-2 sm:col-span-2">
              <Label htmlFor="recipe-when">When</Label>
              <Select
                id="recipe-when"
                value={recipe.when}
                onChange={(event) => set({ when: event.target.value as AutomationRecipe['when'] })}
              >
                {WHEN.map((option) => (
                  <option key={option.value} value={option.value}>
                    {option.label}
                  </option>
                ))}
              </Select>
            </div>
          </div>
        ) : null}

        {kind === 'motion_alert' || kind === 'system_alert' ? (
          <Picks
            legend="Notify"
            options={targets.notify}
            value={recipe.notify}
            onChange={(notify) => set({ notify })}
          />
        ) : null}
        {kind === 'motion_alert' ? (
          <CheckboxField
            label="Include the camera picture"
            checked={recipe.image}
            onChange={(event) => set({ image: event.target.checked })}
          />
        ) : null}
        {kind === 'system_alert' ? (
          <div className="space-y-2">
            <Label htmlFor="recipe-storage">Also warn when free space drops below (GB)</Label>
            <Input
              id="recipe-storage"
              type="number"
              min={1}
              value={recipe.storage_below_gb}
              onChange={(event) => set({ storage_below_gb: Number(event.target.value) || 1 })}
            />
          </div>
        ) : null}
        {kind === 'motion_alert' || kind === 'system_alert' ? (
          <div className="space-y-2">
            <Label htmlFor="recipe-cooldown">Do not repeat for (minutes)</Label>
            <Input
              id="recipe-cooldown"
              type="number"
              min={0}
              max={240}
              value={recipe.cooldown_minutes}
              onChange={(event) => set({ cooldown_minutes: Number(event.target.value) || 0 })}
            />
          </div>
        ) : null}

        {kind === 'motion_actions' ? (
          <>
            <Picks
              legend="Turn on"
              options={targets.lights}
              value={recipe.lights}
              onChange={(lights) => set({ lights })}
            />
            <div className="space-y-2">
              <Label htmlFor="recipe-minutes">For (minutes)</Label>
              <Input
                id="recipe-minutes"
                type="number"
                min={1}
                max={240}
                value={recipe.minutes}
                onChange={(event) => set({ minutes: Number(event.target.value) || 1 })}
              />
            </div>
            <CheckboxField
              label="Also sound the alarm, only if it is already armed"
              checked={recipe.trigger_alarm}
              onChange={(event) => set({ trigger_alarm: event.target.checked })}
            />
          </>
        ) : null}

        {alarmNeeded ? (
          <div className="space-y-2">
            <Label htmlFor="recipe-alarm">Alarm</Label>
            <Select
              id="recipe-alarm"
              value={recipe.alarm_entity ?? ''}
              onChange={(event) => set({ alarm_entity: event.target.value || null })}
            >
              <option value="">Choose the alarm</option>
              {targets.alarms.map((alarm) => (
                <option key={alarm.id} value={alarm.id}>
                  {alarm.name}
                </option>
              ))}
            </Select>
          </div>
        ) : null}

        {kind === 'presence_recording' ? (
          <Picks
            legend="Record these cameras only while nobody is home"
            options={rtsp.map((camera) => ({ id: camera.id, name: camera.name }))}
            value={recipe.camera_ids}
            onChange={(camera_ids) => set({ camera_ids })}
          />
        ) : null}

        {kind === 'motion_alert' || kind === 'system_alert' ? (
          <div className="space-y-2">
            <Label htmlFor="recipe-message">Message (optional)</Label>
            <Input
              id="recipe-message"
              value={recipe.message}
              maxLength={300}
              onChange={(event) => set({ message: event.target.value })}
            />
          </div>
        ) : null}

        <Button
          loading={saving}
          disabled={!recipe.name.trim() || (motion && !recipe.camera_id)}
          onClick={() => {
            onAdd({ ...recipe, name: recipe.name.trim() })
            setRecipe(blank(kind))
          }}
        >
          <Plus aria-hidden="true" />
          Create in Home Assistant
        </Button>
      </CardContent>
    </Card>
  )
}

export function AutomationsPage() {
  const queryClient = useQueryClient()
  const state = useQuery({
    queryKey: queryKeys.automations,
    queryFn: automationsApi.get,
    // Home Assistant reports what it did a moment after a save.
    refetchInterval: 10_000,
  })
  const save = useMutation({
    mutationFn: automationsApi.save,
    onSuccess: (next) => queryClient.setQueryData(queryKeys.automations, next),
  })
  const recipes = state.data?.recipes ?? []
  const targets = state.data?.targets

  return (
    <div className="mx-auto max-w-3xl space-y-6">
      <PageHeader
        title="Automations"
        description="Home Assistant automations Sentinel sets up for you. They appear in Home Assistant under Settings → Automations, where you can change them."
      />

      {!targets ? (
        <Alert variant="info">
          Home Assistant has not connected yet. Add the integration under Configure → Home
          Assistant, then come back here.
        </Alert>
      ) : !targets.allowed ? (
        <Alert variant="warning" title="Home Assistant has not allowed this yet">
          In Home Assistant open Settings → Devices &amp; services → Sentinel CCTV → Configure and
          turn on “Let Sentinel create automations”. Until then nothing is created. Sentinel can
          only notify phones, switch lights, record cameras and sound an alarm that is already
          armed.
        </Alert>
      ) : !targets.automations_file ? (
        <Alert variant="danger">
          This Home Assistant does not keep its automations in automations.yaml, so Sentinel cannot
          add any.
        </Alert>
      ) : null}

      {errorText(save.error, 'Could not save the automations.') ? (
        <Alert variant="danger">{errorText(save.error, 'Could not save the automations.')}</Alert>
      ) : null}

      {recipes.length > 0 ? (
        <Card>
          <CardHeader>
            <CardTitle>Your automations</CardTitle>
          </CardHeader>
          <CardContent>
            <ul className="divide-y divide-(--color-border)">
              {recipes.map((recipe) => {
                const status = state.data?.status[recipe.id]
                return (
                  <li key={recipe.id} className="flex flex-wrap items-center gap-2 py-3">
                    <div className="min-w-0 flex-1">
                      <p className="truncate text-sm font-medium">{recipe.name}</p>
                      <p className="text-xs text-(--color-fg-muted)">
                        {KINDS.find((kind) => kind.value === recipe.kind)?.label}
                      </p>
                      {status?.message ? (
                        <p className="text-xs text-(--color-fg-muted)">{status.message}</p>
                      ) : null}
                    </div>
                    <StatusBadge status={status} />
                    {status?.state === 'edited_in_ha' ? (
                      <Button
                        size="sm"
                        variant="secondary"
                        onClick={() =>
                          save.mutate(
                            recipes.map((item) =>
                              item.id === recipe.id ? { ...item, reclaim: item.reclaim + 1 } : item,
                            ),
                          )
                        }
                      >
                        <Undo2 aria-hidden="true" />
                        Give back to Sentinel
                      </Button>
                    ) : null}
                    <Button
                      size="icon"
                      variant="ghost"
                      aria-label={`Delete ${recipe.name}`}
                      onClick={() => save.mutate(recipes.filter((item) => item.id !== recipe.id))}
                    >
                      <Trash2 aria-hidden="true" />
                    </Button>
                  </li>
                )
              })}
            </ul>
          </CardContent>
        </Card>
      ) : null}

      {targets ? (
        <NewRecipe
          targets={targets}
          saving={save.isPending}
          onAdd={(recipe) => save.mutate([...recipes, recipe])}
        />
      ) : null}
    </div>
  )
}
