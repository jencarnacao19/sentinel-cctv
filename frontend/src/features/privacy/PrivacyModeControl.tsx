import { Check, EyeOff, ShieldCheck } from 'lucide-react'
import { useState } from 'react'

import { Alert } from '@/components/ui/alert'
import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogBody,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { usePrivacyMode, useSetPrivacyMode } from '@/features/privacy/usePrivacyMode'
import { cn } from '@/lib/utils'
import type { PrivacyReason } from '@/types/api'

const REASON_DETAILS: Record<PrivacyReason, string> = {
  privacy: 'Pause surveillance immediately without a more specific public explanation.',
  guests: 'Let viewers know that guests are present.',
  private_event: 'Use during a gathering, meeting or other private event.',
  maintenance: 'Use while cameras or the local network are being serviced.',
  temporary: 'A neutral reason for a short administrator-requested pause.',
}

const DEFAULT_REASONS: Record<PrivacyReason, string> = {
  privacy: 'Privacy requested',
  guests: 'Guests are present',
  private_event: 'Private event in progress',
  maintenance: 'Camera maintenance in progress',
  temporary: 'Temporarily disabled by an administrator',
}

/**
 * Turning every camera off, and back on again.
 *
 * The reason is not decoration: it is the sentence every viewer sees in place
 * of the feed, so it is chosen before the pause rather than typed afterwards,
 * and the list is fixed so nobody can accidentally publish a note that was
 * meant for one person.
 *
 * The choice is a real radio group with a check mark on the selected row. A
 * tinted background alone told a screen reader nothing, and told a
 * colour-blind viewer very little.
 */
export function PrivacyModeControl({ className }: { className?: string }) {
  const privacy = usePrivacyMode()
  const update = useSetPrivacyMode()
  const [open, setOpen] = useState(false)
  const [reason, setReason] = useState<PrivacyReason>('privacy')
  const state = privacy.data

  const reasons = state?.reasons ?? DEFAULT_REASONS

  function submit(): void {
    void update
      .mutateAsync({ enabled: !state?.enabled, reason: state?.enabled ? undefined : reason })
      .then(() => setOpen(false))
      .catch(() => undefined)
  }

  return (
    <>
      <Button
        type="button"
        variant={state?.enabled ? 'danger' : 'secondary'}
        className={cn('w-full justify-start text-left', className)}
        onClick={() => {
          update.reset()
          setOpen(true)
        }}
        disabled={!state}
      >
        {state?.enabled ? <EyeOff aria-hidden="true" /> : <ShieldCheck aria-hidden="true" />}
        <span className="min-w-0 truncate">
          {state?.enabled ? 'Cameras paused' : 'Privacy mode'}
        </span>
      </Button>

      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent className="sm:max-w-md">
          <DialogHeader>
            <DialogTitle>{state?.enabled ? 'Resume all cameras?' : 'Pause all cameras'}</DialogTitle>
            <DialogDescription>
              {state?.enabled
                ? 'Local live view, recording, motion detection and camera controls will start again.'
                : 'Choose the explanation viewers will see instead of every live feed.'}
            </DialogDescription>
          </DialogHeader>

          <DialogBody className="space-y-4">
            {state?.enabled ? (
              <Alert variant="warning" title={state.reason_label ?? 'Privacy mode active'}>
                Historical playback and saved clips stayed available during the pause, and nothing
                recorded before it was affected.
              </Alert>
            ) : (
              <div
                role="radiogroup"
                aria-label="Privacy reason"
                className="divide-y divide-(--color-border) overflow-hidden rounded-(--radius-card) border border-(--color-border)"
              >
                {(Object.entries(reasons) as [PrivacyReason, string][]).map(([value, label]) => {
                  const active = reason === value
                  return (
                    <button
                      key={value}
                      type="button"
                      role="radio"
                      aria-checked={active}
                      onClick={() => setReason(value)}
                      className={cn(
                        'flex min-h-11 w-full items-start gap-3 p-3 text-left transition-colors',
                        'duration-(--duration-fast)',
                        active
                          ? 'bg-(--color-accent-subtle)'
                          : 'bg-(--color-surface) hover:bg-(--color-surface-hover)',
                      )}
                    >
                      <span className="min-w-0 flex-1">
                        <span className="block text-sm font-semibold">{label}</span>
                        <span className="mt-0.5 block text-xs leading-4 text-(--color-fg-muted)">
                          {REASON_DETAILS[value]}
                        </span>
                      </span>
                      <span
                        className={cn(
                          'mt-0.5 shrink-0 text-(--color-accent)',
                          active ? 'opacity-100' : 'opacity-0',
                        )}
                        aria-hidden="true"
                      >
                        <Check className="size-4" />
                      </span>
                    </button>
                  )
                })}
              </div>
            )}

            {!state?.enabled ? (
              <Alert variant="warning" title="This pauses camera use inside Sentinel">
                New live video, snapshots, recordings, motion events and PTZ connections stop.
                Existing recordings are not deleted, camera power is unchanged and no cloud service
                is involved.
              </Alert>
            ) : null}

            {update.isError ? (
              <Alert variant="danger" title="The privacy change was not cleanly completed">
                {update.error instanceof Error ? update.error.message : 'Check system diagnostics.'}
              </Alert>
            ) : null}
          </DialogBody>

          <DialogFooter>
            <Button variant="ghost" onClick={() => setOpen(false)} disabled={update.isPending}>
              Cancel
            </Button>
            <Button
              variant={state?.enabled ? 'primary' : 'danger'}
              onClick={submit}
              loading={update.isPending}
            >
              {state?.enabled ? 'Resume cameras' : 'Pause all cameras'}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  )
}

/** Ends a phrase with a full stop unless it already ends with punctuation. */
function sentence(value: string): string {
  return /[.!?]$/.test(value.trim()) ? value.trim() : `${value.trim()}.`
}

/**
 * The banner that has to be impossible to miss.
 *
 * It sits with the navigation bar rather than in the page, so it does not
 * scroll away, and it states the reason viewers are being shown. Warning
 * colouring, a symbol and a bold label -- no glow, no animation. A state this
 * consequential does not need decoration to be noticed; it needs to be
 * permanently in view.
 */
export function PrivacyModeBanner() {
  const privacy = usePrivacyMode()
  const state = privacy.data
  if (!state?.enabled) return null

  return (
    <div
      role="status"
      className="border-b border-(--color-warning)/40 bg-(--color-warning)/12 px-4 py-2.5 sm:px-6 lg:px-8"
    >
      <div className="mx-auto flex max-w-[110rem] items-center gap-2.5">
        <span className="flex size-7 shrink-0 items-center justify-center rounded-full bg-(--color-warning)/20 text-(--color-warning)">
          <EyeOff className="size-4" aria-hidden="true" />
        </span>
        <span className="min-w-0 text-sm">
          <span className="font-semibold">All cameras paused.</span>{' '}
          <span className="text-(--color-fg-muted)">
            {/* The stored reason is an operator-chosen phrase and may or may not
                already be a full sentence, so the full stop is added only when
                it needs one. */}
            {sentence(state.reason_label ?? 'Privacy mode is on')} Recorded footage is unaffected.
          </span>
        </span>
      </div>
    </div>
  )
}
