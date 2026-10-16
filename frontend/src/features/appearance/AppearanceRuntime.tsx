import { useEffect, useState } from 'react'
import { useLocation } from 'react-router-dom'

import { api } from '@/api/client'
import { viewerApi } from '@/features/viewer/access'
import { activatePreferences, DEFAULT_PREFERENCES, refreshPreferences, resetPreferences, useThemeStore, validPreferences, type Preferences } from '@/stores/theme'
import { AppearanceDock } from './AppearanceControls'
import { homeAssistantOrigin, startHaBridge } from './haBridge'
import { installHoverSound } from './hoverSound'

export interface AppearanceResponse { profile_id: string | null; preferences: Preferences | null; hover_sounds_allowed: boolean }
const BROWSER_KEY = 'sentinel.viewer-appearance.v1'

function browserPreferences(): Preferences {
  try {
    const value: unknown = JSON.parse(localStorage.getItem(BROWSER_KEY) ?? 'null')
    if (validPreferences(value)) return value
    const legacy = localStorage.getItem('sentinel.theme')
    if (legacy === 'light' || legacy === 'dark' || legacy === 'system') {
      const migrated: Preferences = { ...DEFAULT_PREFERENCES, mode: legacy }
      localStorage.setItem(BROWSER_KEY, JSON.stringify(migrated))
      return migrated
    }
  } catch { /* Private mode still supports an in-memory preference. */ }
  return { ...DEFAULT_PREFERENCES }
}

export function AppearanceRuntime() {
  const { pathname } = useLocation()
  const active = !['/setup', '/login', '/admin/login'].includes(pathname)
  const admin = pathname.startsWith('/admin')
  // Keep the bridge origin across SPA navigation to playback/live.
  const [haOrigin] = useState(homeAssistantOrigin)
  const bridgeOrigin = admin ? null : haOrigin
  useEffect(() => {
    resetPreferences()
    if (!active) return
    let disposed = false
    let inFlight = false
    let scope: string | null = null
    let policyExpiry: number | undefined
    const stopBridge = bridgeOrigin ? startHaBridge(bridgeOrigin) : undefined
    const stopAudio = installHoverSound()
    const refresh = async () => {
      if (inFlight || disposed) return
      inFlight = true
      const { revision, saveState } = useThemeStore.getState()
      try {
        const data = await viewerApi.get<AppearanceResponse>('/viewer/appearance')
        if (disposed || typeof data?.hover_sounds_allowed !== 'boolean') return
        window.clearTimeout(policyExpiry)
        useThemeStore.setState({ soundAllowed: data.hover_sounds_allowed, policyKnown: true })
        // Fail silent if an open tab loses contact with the admin policy.
        policyExpiry = window.setTimeout(() => useThemeStore.setState({ soundAllowed: false, policyKnown: false }), 8000)
        if (stopBridge) return
        const nextScope = data.profile_id ?? 'browser'
        const preferences = validPreferences(data.preferences) ? data.preferences : data.profile_id ? DEFAULT_PREFERENCES : browserPreferences()
        if (scope !== nextScope) {
          scope = nextScope
          const id = data.profile_id
          activatePreferences(id ? 'Your Sentinel account' : 'This browser only · shared viewer', preferences,
            id ? (p) => api.put('/appearance/me', { profile_id: id, preferences: p }, { keepalive: true }) : (p) => {
              localStorage.setItem(BROWSER_KEY, JSON.stringify(p))
            })
        // A GET started while a PUT is pending may contain the old value even
        // if it arrives after the PUT succeeds. It must not undo that save.
        } else if (nextScope !== 'browser' && saveState === 'saved' && revision === useThemeStore.getState().revision) refreshPreferences(preferences)
      } catch {
        if (!disposed) {
          useThemeStore.setState({ soundAllowed: false, policyKnown: false })
          if (!stopBridge && !useThemeStore.getState().ready) useThemeStore.setState({
            scope: 'Preferences unavailable. Reconnecting…', saveState: 'error',
          })
        }
      } finally { inFlight = false }
    }
    void refresh()
    const timer = window.setInterval(() => { if (!document.hidden) void refresh() }, 5000)
    const onVisible = () => { if (!document.hidden) void refresh() }
    // Browser preferences are local state, not part of the server sound-policy
    // poll. Only an explicit valid change from another tab should replace them.
    const onStorage = (event: StorageEvent) => {
      if (scope !== 'browser' || event.key !== BROWSER_KEY || !event.newValue) return
      try {
        const preferences: unknown = JSON.parse(event.newValue)
        if (validPreferences(preferences)) refreshPreferences(preferences)
      } catch { /* A removed/invalid value must not erase this tab's choice. */ }
    }
    window.addEventListener('focus', onVisible)
    window.addEventListener('storage', onStorage)
    window.addEventListener('sentinel:policy-changed', onVisible)
    document.addEventListener('visibilitychange', onVisible)
    return () => {
      disposed = true
      window.clearInterval(timer)
      window.clearTimeout(policyExpiry)
      window.removeEventListener('focus', onVisible)
      window.removeEventListener('storage', onStorage)
      window.removeEventListener('sentinel:policy-changed', onVisible)
      document.removeEventListener('visibilitychange', onVisible)
      stopBridge?.()
      stopAudio()
      resetPreferences()
    }
  }, [active, bridgeOrigin])
  return active ? <AppearanceDock admin={admin} /> : null
}
