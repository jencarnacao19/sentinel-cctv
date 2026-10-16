import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { api } from '@/api/client'
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from '@/components/ui/card'
import { Alert } from '@/components/ui/alert'
import { Switch } from '@/components/ui/switch'
import { useThemeStore } from '@/stores/theme'
import type { AppearanceResponse } from './AppearanceRuntime'

export function SoundPolicyCard() {
  const client = useQueryClient()
  const policy = useQuery({ queryKey: ['sound-policy'], queryFn: () => api.get<AppearanceResponse>('/viewer/appearance') })
  const update = useMutation({
    mutationFn: (hover_sounds_allowed: boolean) => api.put<{ hover_sounds_allowed: boolean }>('/appearance/sound-policy', { hover_sounds_allowed }),
    onSuccess: async (data) => {
      useThemeStore.setState({ soundAllowed: data.hover_sounds_allowed, policyKnown: true })
      window.dispatchEvent(new Event('sentinel:policy-changed'))
      await client.invalidateQueries({ queryKey: ['sound-policy'] })
    },
  })
  return <Card>
    <CardHeader><CardTitle>Interface sounds</CardTitle><CardDescription>Administrator control for the web interface and Home Assistant panels.</CardDescription></CardHeader>
    <CardContent className="space-y-3">
      <div className="flex items-center justify-between gap-4">
        <label htmlFor="global-hover-sounds" className="text-sm font-medium">Allow hover sounds for everyone</label>
        <Switch id="global-hover-sounds" tone="accent" checked={policy.data?.hover_sounds_allowed ?? false} disabled={!policy.data || update.isPending} onCheckedChange={(value) => update.mutate(value)} />
      </div>
      <p className="text-xs leading-5 text-(--color-fg-muted)">Disabling silences interface sounds in open panels within a few seconds. Enabling permits sounds but respects each person's mute choice. Camera audio and alarms are unchanged.</p>
      {(update.isError || policy.isError) && <Alert variant="danger">Could not load or save the sound policy. Reload and try again.</Alert>}
    </CardContent>
  </Card>
}
