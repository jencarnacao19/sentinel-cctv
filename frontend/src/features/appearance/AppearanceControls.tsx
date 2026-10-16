import { Check, Monitor, Moon, Sun, Volume2, VolumeX } from 'lucide-react'
import { useState } from 'react'

import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription, DialogBody, DialogFooter } from '@/components/ui/dialog'
import { Segmented } from '@/components/ui/segmented'
import { Switch } from '@/components/ui/switch'
import { cn } from '@/lib/utils'
import { useMobileInterface } from '@/lib/device'
import { PALETTES, resolveTheme, useThemeStore, type Palette, type ThemeMode } from '@/stores/theme'

const PREVIEW: Record<Palette, { light: string; dark: string; lightSurface: string; darkSurface: string }> = {
  slate: { light: '#f1f2f4', dark: '#17191d', lightSurface: '#ffffff', darkSurface: '#292c32' },
  ocean: { light: '#eaf4f8', dark: '#10232d', lightSurface: '#f7fcfe', darkSurface: '#1b3644' },
  forest: { light: '#edf4ee', dark: '#162820', lightSurface: '#f9fcf8', darkSurface: '#294136' },
  sand: { light: '#f5f0e8', dark: '#2a231c', lightSurface: '#fffcf6', darkSurface: '#43382b' },
}

export function AppearanceDock({ admin }: { admin: boolean }) {
  const mobile = useMobileInterface()
  const [open, setOpen] = useState(false)
  const state = useThemeStore()
  const current = PALETTES.find((palette) => palette.value === state.palette)!
  const dark = resolveTheme(state.mode) === 'dark'
  const enabled = state.hover_sound && state.soundAllowed && state.policyKnown
  const soundLabel = !state.policyKnown ? 'Interface sounds unavailable' : !state.soundAllowed ? 'Interface sounds disabled by administrator' : state.hover_sound ? 'Mute interface sounds' : 'Enable interface sounds'
  return <>
    <div data-ui-sound="off" aria-label="Personal preferences" className={cn(
      'fixed right-3 z-40 flex items-center gap-0.5 rounded-full border border-(--color-border) bg-(--color-surface) p-1 shadow-sm',
      admin ? 'bottom-[calc(env(safe-area-inset-bottom,0px)+4rem)] lg:bottom-3' : 'bottom-[calc(env(safe-area-inset-bottom,0px)+0.75rem)]',
    )}>
      <Button variant="ghost" size="sm" className="min-h-10 gap-2 rounded-full px-3" aria-label="Choose theme and preferences" title="Theme" onClick={() => setOpen(true)}>
        <span aria-hidden="true" className="size-3 rounded-full ring-1 ring-black/10" style={{ backgroundColor: current.colour }} />
        Theme
      </Button>
      {!mobile && <Button variant="ghost" size="icon" className="rounded-full" aria-label={soundLabel} title={soundLabel} aria-pressed={enabled}
        disabled={!state.ready || !state.soundAllowed || !state.policyKnown} onClick={() => state.setHoverSound(!state.hover_sound)}>
        {enabled ? <Volume2 className="size-4" /> : <VolumeX className="size-4" />}
      </Button>}
    </div>
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogContent className="sm:max-w-[440px]">
        <DialogHeader><DialogTitle>Appearance</DialogTitle><DialogDescription>Choose a theme. Changes apply immediately.</DialogDescription></DialogHeader>
        <DialogBody className="space-y-5">
          <fieldset disabled={!state.ready} className="space-y-3">
            <legend className="mb-3 text-sm font-semibold">Colour mode</legend>
            <Segmented<ThemeMode> className="flex w-full [&>button]:min-h-11 [&>button]:flex-1" ariaLabel="Colour mode" value={state.mode} onChange={state.setMode} options={[
              { value: 'light', label: 'Light', children: 'Light', icon: <Sun aria-hidden="true" /> },
              { value: 'dark', label: 'Dark', children: 'Dark', icon: <Moon aria-hidden="true" /> },
              { value: 'system', label: 'System', children: 'System', icon: <Monitor aria-hidden="true" /> },
            ]} />
          </fieldset>
          <fieldset disabled={!state.ready}>
            <legend className="mb-3 text-sm font-semibold">Theme</legend>
            <div className="grid grid-cols-2 gap-3" role="radiogroup" aria-label="Theme palette">
              {PALETTES.map((palette) => {
                const selected = state.palette === palette.value
                const colours = PREVIEW[palette.value]
                return <label key={palette.value} className="relative cursor-pointer">
                  <input type="radio" name="appearance-palette" value={palette.value} checked={selected}
                    onChange={() => state.setPalette(palette.value)} className="peer sr-only" aria-label={palette.label} />
                  <span className={cn('block overflow-hidden rounded-xl border p-1 transition-colors peer-focus-visible:outline-2 peer-focus-visible:outline-offset-2 peer-focus-visible:outline-(--color-accent) peer-disabled:opacity-50',
                    selected ? 'border-(--color-accent) ring-1 ring-(--color-accent)' : 'border-(--color-border) hover:border-(--color-border-strong)')}>
                    <span aria-hidden="true" className="flex h-20 gap-2 rounded-lg p-2.5" style={{ background: dark ? colours.dark : colours.light }}>
                      <span className="flex w-5 shrink-0 flex-col gap-1.5 rounded px-1 py-2" style={{ background: dark ? colours.darkSurface : colours.lightSurface }}>
                        <span className="h-1 rounded-full" style={{ background: palette.colour }} />
                        <span className="h-1 rounded-full bg-current opacity-15" />
                        <span className="h-1 rounded-full bg-current opacity-15" />
                      </span>
                      <span className="flex flex-1 flex-col gap-2 py-1">
                        <span className="h-1.5 w-2/3 rounded-full" style={{ background: palette.colour }} />
                        <span className="grid flex-1 grid-cols-2 gap-1.5">
                          <span className="rounded" style={{ background: dark ? colours.darkSurface : colours.lightSurface }} />
                          <span className="rounded" style={{ background: dark ? colours.darkSurface : colours.lightSurface }} />
                        </span>
                      </span>
                    </span>
                    <span className="flex min-h-10 items-center justify-between gap-2 px-2 text-sm font-medium">
                      {palette.label}
                      <span className={cn('flex size-5 items-center justify-center rounded-full', selected ? 'bg-(--color-accent) text-(--color-accent-fg)' : 'border border-(--color-border)')}>
                        {selected && <Check aria-hidden="true" className="size-3.5" />}
                      </span>
                    </span>
                  </span>
                </label>
              })}
            </div>
          </fieldset>
          {!mobile && <div className="flex items-start justify-between gap-4 border-t border-(--color-border) pt-4">
            <div><label htmlFor="personal-hover-sound" className="text-sm font-semibold">Interface sounds</label>
              <p className="mt-1 text-xs leading-5 text-(--color-fg-muted)">Quiet ticks when using a mouse. Camera audio stays unchanged.</p>
              {!state.soundAllowed && <p className="mt-1 text-xs text-(--color-fg-muted)">{state.policyKnown ? 'Disabled for everyone by the administrator.' : 'Waiting for the server sound policy.'}</p>}
            </div>
            <Switch id="personal-hover-sound" tone="accent" checked={state.hover_sound} disabled={!state.ready || !state.soundAllowed} onCheckedChange={state.setHoverSound} />
          </div>}
          <p className="text-xs leading-5 text-(--color-fg-muted)">{state.scope}
            {state.scope.startsWith('This browser') && '. Home Assistant account sync needs the Sentinel custom card.'}
          </p>
        </DialogBody>
        <DialogFooter className="!flex-row items-center !justify-between">
          <div role="status" aria-live="polite" className="text-xs text-(--color-fg-muted)">
            {state.saveState === 'saving' ? 'Saving…' : state.saveState === 'error' ? 'Not saved. Try again.' : state.ready ? 'Saved' : 'Loading…'}
            {state.saveState === 'error' && state.ready && <Button variant="ghost" size="sm" onClick={state.retry}>Retry</Button>}
          </div>
          <Button size="sm" onClick={() => setOpen(false)}>Done</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  </>
}
