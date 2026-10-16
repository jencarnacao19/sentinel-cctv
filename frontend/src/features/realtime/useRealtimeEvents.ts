import { useEffect, useRef, useState } from 'react'

import { RealtimeClient, type RealtimeEvent, type RealtimeStatus } from './client'

export interface UseRealtimeEventsOptions {
  /** Called for every event frame the server delivers, live or replayed
   * after a resume. */
  onEvent?: (event: RealtimeEvent) => void
  /** The server could not fill a resume gap -- refetch whatever REST state
   * this consumer depends on rather than trust it is still current. */
  onResyncRequired?: () => void
  /** Set to false to tear the connection down, e.g. after sign-out. */
  enabled?: boolean
}

export interface UseRealtimeEventsResult {
  status: RealtimeStatus
  /** The most recent event frame, for a consumer that only cares about the
   * latest one rather than reacting to every one individually. */
  lastEvent: RealtimeEvent | null
}

/**
 * Subscribes to the realtime event stream for the lifetime of the calling
 * component.
 *
 * One WebSocket per call: a page that wants every camera card, the storage
 * gauge and a toast notifier all reacting to the same stream should call
 * this once near its root and pass the result (or the callbacks) down,
 * rather than have every consumer open its own connection.
 */
export function useRealtimeEvents(
  options: UseRealtimeEventsOptions = {},
): UseRealtimeEventsResult {
  const { onEvent, onResyncRequired, enabled = true } = options
  const [status, setStatus] = useState<RealtimeStatus>('connecting')
  const [lastEvent, setLastEvent] = useState<RealtimeEvent | null>(null)

  // Refs so a new callback identity on every render does not tear down and
  // reopen the socket -- only `enabled` toggling should ever do that.
  const onEventRef = useRef(onEvent)
  onEventRef.current = onEvent
  const onResyncRef = useRef(onResyncRequired)
  onResyncRef.current = onResyncRequired

  useEffect(() => {
    if (!enabled) {
      setStatus('closed')
      return
    }

    const client = new RealtimeClient({
      onStatusChange: setStatus,
      onEvent: (event) => {
        setLastEvent(event)
        onEventRef.current?.(event)
      },
      onResyncRequired: () => onResyncRef.current?.(),
    })
    client.start()
    return () => client.stop()
  }, [enabled])

  return { status, lastEvent }
}
