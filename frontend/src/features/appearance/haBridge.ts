import { activatePreferences, refreshPreferences, useThemeStore, validPreferences, type Preferences } from '@/stores/theme'

/** Cosmetic values only. This bridge never authenticates or grants privileges. */
export function homeAssistantOrigin(): string | null {
  if (window.parent === window) return null
  const params = new URLSearchParams(window.location.search)
  if (params.get('ha_preferences') !== '1') return null
  try {
    const url = new URL(params.get('ha_origin') ?? '')
    return ['http:', 'https:'].includes(url.protocol) && url.origin !== 'null' ? url.origin : null
  } catch { return null }
}

// getRandomValues also works on HTTP LAN dashboards, unlike randomUUID.
function randomId(): string {
  return Array.from(crypto.getRandomValues(new Uint8Array(16)), (byte) => byte.toString(16).padStart(2, '0')).join('')
}

export function startHaBridge(origin: string): () => void {
  const channel = randomId()
  const pending = new Map<string, { resolve: () => void; reject: () => void; timer: number }>()
  let activated = false
  const send = (message: Record<string, unknown>) => window.parent.postMessage({ ...message, channel }, origin)
  const ready = () => send({ type: 'sentinel:preferences:ready' })
  const save = (preferences: Preferences) => new Promise<void>((resolve, reject) => {
    const id = randomId()
    const timer = window.setTimeout(() => { pending.delete(id); reject(new Error('HA save timed out')) }, 10_000)
    pending.set(id, { resolve, reject: () => reject(new Error('HA could not save preferences')), timer })
    send({ type: 'sentinel:preferences:save', id, preferences })
  })
  const receive = (event: MessageEvent) => {
    if (event.source !== window.parent || event.origin !== origin) return
    const data = event.data as Record<string, unknown> | null
    if (!data || data.channel !== channel) return
    if (data.type === 'sentinel:preferences:init' && validPreferences(data.preferences)) {
      if (!activated) {
        activatePreferences('Your Home Assistant account', data.preferences, save)
        activated = true
      } else refreshPreferences(data.preferences)
    }
    if (data.type === 'sentinel:preferences:result' && typeof data.id === 'string') {
      const item = pending.get(data.id)
      if (!item) return
      window.clearTimeout(item.timer)
      pending.delete(data.id)
      if (data.ok === true) item.resolve()
      else item.reject()
    }
  }
  window.addEventListener('message', receive)
  const retry = window.setInterval(() => { if (!activated) ready() }, 2000)
  const unavailable = window.setTimeout(() => {
    if (!activated) useThemeStore.setState({ scope: 'Home Assistant preferences unavailable. Reload the panel.', saveState: 'error' })
  }, 12_000)
  ready()
  return () => {
    window.removeEventListener('message', receive)
    window.clearInterval(retry)
    window.clearTimeout(unavailable)
    for (const item of pending.values()) { window.clearTimeout(item.timer); item.reject() }
    pending.clear()
  }
}
