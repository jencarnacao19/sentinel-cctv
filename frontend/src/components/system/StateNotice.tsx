import { PlugZap, Radio, ShieldAlert, WifiOff } from 'lucide-react'
import type { ReactNode } from 'react'

import { describeFailure, type FailureOptions } from '@/components/system/failure'
import { Alert } from '@/components/ui/alert'

/**
 * Named system states, each with its own wording and its own symbol.
 *
 * Recording being suspended because a disk vanished, a disk that is merely
 * getting full, and a camera that will not answer are three different
 * situations with three different next steps. They shared one red alert, which
 * made an urgent one indistinguishable from an advisory one.
 */

/** Anything that came back as an error from an API call. */
export function FailureNotice({
  error,
  className,
  ...options
}: FailureOptions & { error: unknown; className?: string }) {
  const failure = describeFailure(error, options)
  if (!failure) return null
  return (
    <Alert
      variant={failure.tone}
      title={failure.title}
      className={className}
      icon={failure.transport ? <WifiOff /> : <ShieldAlert />}
    >
      {failure.detail}
    </Alert>
  )
}

/**
 * The recorder has stopped on purpose.
 *
 * Distinct from a disk that is filling: this one means nothing is being written
 * right now, which is the single most consequential state a recorder can be in
 * and the one an operator most needs to see first.
 */
export function RecorderStoppedNotice({ reason }: { reason: string }) {
  return (
    <Alert variant="danger" title="Recording is suspended" icon={<PlugZap />}>
      <p>{reason}</p>
      <p className="mt-1.5">
        Sentinel stops writing rather than filling the system disk. It starts again on its own as
        soon as the recording storage is back — nothing needs to be restarted by hand.
      </p>
    </Alert>
  )
}

/** One or more cameras are configured to record but no recorder is running. */
export function RecordersMissingNotice({ count }: { count: number }) {
  if (count <= 0) return null
  return (
    <Alert
      variant="danger"
      title={`${count} camera${count === 1 ? '' : 's'} not recording`}
      icon={<Radio />}
    >
      {count === 1 ? 'A camera is' : 'These cameras are'} set to record but no recorder is
      running. Open the camera, check its stream address and credentials, then use Test — the row
      turns green as soon as the recorder starts.
    </Alert>
  )
}

/** A wrapper for a state that has no dedicated component yet. */
export function SystemNotice({
  tone = 'warning',
  title,
  icon,
  children,
}: {
  tone?: 'warning' | 'danger' | 'info' | 'success'
  title: string
  icon?: ReactNode
  children: ReactNode
}) {
  return (
    <Alert variant={tone} title={title} icon={icon}>
      {children}
    </Alert>
  )
}
