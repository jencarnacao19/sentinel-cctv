import { useMutation, useQueryClient } from '@tanstack/react-query'
import { ArrowLeft, Check } from 'lucide-react'
import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'

import { queryKeys, systemApi } from '@/api/endpoints'
import { Logo } from '@/components/Logo'
import { Alert } from '@/components/ui/alert'
import { Button } from '@/components/ui/button'
import { AccountsManager } from '@/features/accounts/AccountsManager'
import { useCameras } from '@/features/cameras/useCameras'
import { CamerasStep } from '@/features/setup/CamerasStep'
import { HomeAssistantStep } from '@/features/setup/HomeAssistantStep'
import {
  applyRetention,
  RETENTION_CHOICES,
  type RetentionChoice,
  useStorageOverview,
} from '@/features/setup/storage'
import { StorageStep } from '@/features/setup/StorageStep'
import { ImportConfigForm } from '@/features/settings/ImportConfigForm'
import { cn } from '@/lib/utils'
import type { CreatedApiToken } from '@/types/api'
import { errorText } from '@/lib/errors'

type StepId = 'import' | 'storage' | 'cameras' | 'accounts' | 'home-assistant' | 'finish'

interface Step {
  id: StepId
  name: string
  title: string
  description: string
  optional?: boolean
}

const STEPS: Step[] = [
  {
    id: 'import',
    name: 'Start from a file',
    title: 'Coming from another Sentinel?',
    description:
      'Import the file you downloaded on the old machine under Settings, Move to another ' +
      'machine. It brings the cameras, their motion zones and the recording settings. ' +
      'Setting up for the first time? Skip this step.',
    optional: true,
  },
  {
    id: 'storage',
    name: 'Recordings',
    title: 'Where recordings go',
    description:
      'Sentinel records every camera all the time. Check that the footage goes to a disk ' +
      'with room for it, and choose how long to keep it.',
  },
  {
    id: 'cameras',
    name: 'Cameras',
    title: 'Add your cameras',
    description:
      'Sentinel can look for cameras on your network. Ones it cannot find are added with ' +
      'their RTSP address, which the camera maker documents.',
  },
  {
    id: 'accounts',
    name: 'Family',
    title: 'Accounts for the family',
    description:
      'People with these accounts watch the cameras and recordings at this address and ' +
      'cannot change anything. You can add or remove them later under Settings.',
  },
  {
    id: 'home-assistant',
    name: 'Home Assistant',
    title: 'Connect Home Assistant',
    description:
      'Each camera appears in Home Assistant with its snapshot, motion sensors and ' +
      'recording switch.',
    optional: true,
  },
  {
    id: 'finish',
    name: 'Done',
    title: 'Sentinel is ready',
    description:
      'From now on this address opens the camera wall. Everything you set up here is ' +
      'under Admin, at the top of it.',
  },
]

const STEP_STORAGE_KEY = 'sentinel.setup-step'

function savedStep(): number {
  try {
    const saved = window.localStorage.getItem(STEP_STORAGE_KEY)
    const index = STEPS.findIndex((step) => step.id === saved)
    return index >= 0 ? index : 0
  } catch {
    return 0
  }
}

function rememberStep(id: StepId | null): void {
  try {
    if (id) window.localStorage.setItem(STEP_STORAGE_KEY, id)
    else window.localStorage.removeItem(STEP_STORAGE_KEY)
  } catch {
    // Without storage a reload starts again from the first step, which only
    // costs a few clicks: every step shows what was already done.
  }
}

/**
 * First-run setup, after the administrator account exists.
 *
 * Every step is something the rest of the interface can also do later; the
 * wizard only puts them in the order a new installation needs them, and ends
 * on the camera wall that this address serves from then on.
 */
export function SetupWizard() {
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const cameras = useCameras()
  const { settings } = useStorageOverview()
  const [index, setIndex] = useState(savedStep)
  const [retention, setRetention] = useState<RetentionChoice>('14')
  const [homeAssistantToken, setHomeAssistantToken] = useState<CreatedApiToken | null>(null)

  const step = STEPS[index]
  const cameraCount = cameras.data?.length ?? 0

  useEffect(() => {
    const days = settings.data?.retention_days?.toString()
    if (days && (RETENTION_CHOICES as readonly string[]).includes(days)) {
      setRetention(days as RetentionChoice)
    }
  }, [settings.data])

  useEffect(() => {
    rememberStep(step.id)
  }, [step.id])

  const saveRetention = useMutation({
    mutationFn: () =>
      applyRetention(Number(retention), settings.data?.retention_days ?? null),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ['storage'] })
    },
  })

  const finish = useMutation({
    mutationFn: systemApi.completeSetup,
    onSuccess: async () => {
      rememberStep(null)
      await navigate('/', { replace: true })
      await queryClient.invalidateQueries({ queryKey: queryKeys.authStatus })
    },
  })

  function next(): void {
    setIndex((current) => Math.min(current + 1, STEPS.length - 1))
  }

  function onContinue(): void {
    if (step.id === 'storage') {
      saveRetention.mutate(undefined, { onSuccess: next })
      return
    }
    if (step.id === 'finish') {
      finish.mutate()
      return
    }
    next()
  }

  const blocked = step.id === 'cameras' && cameraCount === 0
  const error =
    step.id === 'storage'
      ? errorText(saveRetention.error, 'Could not save how long to keep recordings.')
      : step.id === 'finish'
        ? errorText(finish.error, 'Could not finish the setup.')
        : null

  return (
    <div className="min-h-dvh bg-(--color-bg)">
      <header className="pt-safe px-safe border-b border-(--color-border)">
        <div className="mx-auto flex min-h-14 max-w-3xl items-center gap-3 px-4 sm:px-6">
          <Logo className="size-6 shrink-0" />
          <p className="text-sm font-semibold">Set up Sentinel</p>
          <p className="ml-auto text-xs text-(--color-fg-muted) sm:hidden">
            Step {index + 1} of {STEPS.length}
          </p>
        </div>
      </header>

      <main className="pb-safe mx-auto grid max-w-3xl gap-8 px-4 py-6 sm:px-6 sm:py-10 md:grid-cols-[11rem_minmax(0,1fr)]">
        <nav aria-label="Setup steps" className="hidden md:block">
          <ol className="space-y-1">
            {STEPS.map((item, position) => (
              <li
                key={item.id}
                aria-current={position === index ? 'step' : undefined}
                className={cn(
                  'flex items-center gap-2 rounded-md px-2 py-1.5 text-sm',
                  position === index
                    ? 'bg-(--color-accent-subtle) font-medium text-(--color-fg)'
                    : 'text-(--color-fg-muted)',
                )}
              >
                <span
                  className={cn(
                    'flex size-5 shrink-0 items-center justify-center rounded-full border text-[11px]',
                    position < index
                      ? 'border-(--color-accent) bg-(--color-accent) text-(--color-accent-fg)'
                      : 'border-(--color-border)',
                  )}
                  aria-hidden="true"
                >
                  {position < index ? <Check className="size-3" /> : position + 1}
                </span>
                {item.name}
                {item.optional ? (
                  <span className="text-xs text-(--color-fg-subtle)">optional</span>
                ) : null}
              </li>
            ))}
          </ol>
        </nav>

        <section aria-labelledby="setup-step-title" className="min-w-0">
          <header className="mb-6">
            <h1 id="setup-step-title" className="text-2xl font-semibold tracking-[-0.02em]">
              {step.title}
            </h1>
            <p className="mt-2 text-sm leading-6 text-(--color-fg-muted)">{step.description}</p>
          </header>

          {step.id === 'import' ? <ImportConfigForm /> : null}
          {step.id === 'storage' ? (
            <StorageStep retention={retention} onRetentionChange={setRetention} />
          ) : null}
          {step.id === 'cameras' ? <CamerasStep /> : null}
          {step.id === 'accounts' ? <AccountsManager /> : null}
          {step.id === 'home-assistant' ? (
            <HomeAssistantStep token={homeAssistantToken} onCreated={setHomeAssistantToken} />
          ) : null}
          {step.id === 'finish' ? (
            <p className="rounded-lg border border-(--color-border) p-4 text-sm">
              Bookmark <span className="font-mono">{window.location.origin}</span> on every device
              that should show the cameras.
            </p>
          ) : null}

          {error ? (
            <Alert variant="danger" className="mt-5">
              {error}
            </Alert>
          ) : null}

          <footer className="mt-8 flex flex-wrap items-center gap-2 border-t border-(--color-border) pt-5">
            {index > 0 ? (
              <Button variant="ghost" onClick={() => setIndex(index - 1)}>
                <ArrowLeft aria-hidden="true" />
                Back
              </Button>
            ) : null}
            <div className="ml-auto flex flex-wrap items-center gap-2">
              {blocked ? (
                <p className="text-xs text-(--color-fg-subtle)">Add a camera to continue.</p>
              ) : null}
              {step.optional ? (
                <Button variant="ghost" onClick={next}>
                  Skip
                </Button>
              ) : null}
              <Button
                onClick={onContinue}
                disabled={blocked}
                loading={saveRetention.isPending || finish.isPending}
              >
                {step.id === 'finish' ? 'Open the cameras' : 'Continue'}
              </Button>
            </div>
          </footer>
        </section>
      </main>
    </div>
  )
}
