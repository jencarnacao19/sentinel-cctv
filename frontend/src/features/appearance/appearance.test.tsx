import { act, fireEvent, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { renderWithProviders } from '@/test/utils'
import { activatePreferences, DEFAULT_PREFERENCES, resetPreferences, useThemeStore, validPreferences } from '@/stores/theme'
import { AppearanceDock } from './AppearanceControls'
import { createHoverClick, hoverTarget, installHoverSound } from './hoverSound'
import { startHaBridge } from './haBridge'
import { AppearanceRuntime } from './AppearanceRuntime'
import { api } from '@/api/client'
import { viewerApi } from '@/features/viewer/access'
import { Link } from 'react-router-dom'

beforeEach(() => { resetPreferences(); localStorage.clear() })

describe('personal appearance', () => {
  it('keeps the current theme when polling after browser storage was cleared', async () => {
    const get = vi.spyOn(viewerApi, 'get').mockResolvedValue({ profile_id: null, preferences: null, hover_sounds_allowed: true })
    renderWithProviders(<AppearanceRuntime />)
    await waitFor(() => expect(useThemeStore.getState().ready).toBe(true))
    act(() => {
      useThemeStore.getState().setPalette('forest')
      useThemeStore.getState().setMode('dark')
    })
    // Reproduce the old Clear-Site-Data response erasing localStorage between
    // a user's click and the next policy request in a cookie-blocked HA embed.
    localStorage.clear()
    await act(async () => { window.dispatchEvent(new Event('focus')); await Promise.resolve() })
    expect(get).toHaveBeenCalledTimes(2)
    expect(useThemeStore.getState()).toMatchObject({ palette: 'forest', mode: 'dark' })
    expect(document.documentElement.dataset.palette).toBe('forest')
    // An explicit valid write from another tab still synchronizes.
    act(() => { window.dispatchEvent(new StorageEvent('storage', {
      key: 'sentinel.viewer-appearance.v1', newValue: JSON.stringify({ ...DEFAULT_PREFERENCES, palette: 'sand' }),
    })) })
    expect(useThemeStore.getState().palette).toBe('sand')
  })
  it.each(['Android', 'iPhone', 'iPad'])('has no interface sound controls or audio listeners on %s', (platform) => {
    vi.spyOn(navigator, 'userAgent', 'get').mockReturnValue(platform)
    const save = vi.fn()
    activatePreferences('Your Sentinel account', DEFAULT_PREFERENCES, save)
    useThemeStore.setState({ soundAllowed: true, policyKnown: true })
    const listen = vi.spyOn(document, 'addEventListener')
    const stop = installHoverSound()
    expect(listen).not.toHaveBeenCalled()
    stop()
    renderWithProviders(<AppearanceDock admin={false} />)
    expect(screen.queryByRole('button', { name: /interface sounds/i })).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Choose theme and preferences' }))
    expect(screen.queryByLabelText('Interface sounds')).not.toBeInTheDocument()
    expect(screen.getByRole('radio', { name: 'Forest' })).toBeInTheDocument()
    expect(useThemeStore.getState().hover_sound).toBe(true)
    expect(save).not.toHaveBeenCalled() // Desktop choice must not be overwritten.
  })
  it('starts rapid hovers immediately, reuses the buffer, and never queues excess voices', () => {
    const sources: { start: ReturnType<typeof vi.fn>; stop: ReturnType<typeof vi.fn>; connect: ReturnType<typeof vi.fn>; disconnect: ReturnType<typeof vi.fn> }[] = []
    const gain = { gain: { value: 0 }, connect: vi.fn(), disconnect: vi.fn() }
    const filter = { frequency: { value: 0 }, connect: vi.fn(() => gain), disconnect: vi.fn() }
    const context = {
      sampleRate: 48000, destination: {},
      createBuffer: vi.fn(() => ({ getChannelData: () => new Float32Array(576) })),
      createBiquadFilter: () => filter, createGain: () => gain,
      createBufferSource: () => {
        const source = { start: vi.fn(), stop: vi.fn(), connect: vi.fn(), disconnect: vi.fn() }
        sources.push(source)
        return source
      },
    }
    const click = createHoverClick(context as unknown as AudioContext)
    // No clock advance or previous onended: all ten must start now.
    for (let i = 0; i < 10; i++) click.play()
    expect(sources).toHaveLength(10)
    expect(sources.every((source) => source.start.mock.calls.length === 1)).toBe(true)
    expect(context.createBuffer).toHaveBeenCalledTimes(1)
    expect(sources[0].stop).toHaveBeenCalledOnce()
    expect(sources[1].stop).toHaveBeenCalledOnce()
    click.dispose()
    expect(sources.every((source) => source.stop.mock.calls.length === 1)).toBe(true)
    expect(filter.disconnect).toHaveBeenCalledOnce()
    expect(gain.disconnect).toHaveBeenCalledOnce()
  })
  it('changes full palette and persists mute without touching audio elements', async () => {
    const save = vi.fn().mockResolvedValue(undefined)
    activatePreferences('Your Sentinel account', DEFAULT_PREFERENCES, save)
    useThemeStore.setState({ soundAllowed: true, policyKnown: true })
    renderWithProviders(<AppearanceDock admin />)
    fireEvent.click(screen.getByRole('button', { name: 'Choose theme and preferences' }))
    expect(screen.getByRole('radio', { name: 'Light' })).toHaveTextContent('Light')
    expect(screen.getByRole('radio', { name: 'Dark' })).toHaveTextContent('Dark')
    expect(screen.getByRole('radio', { name: 'System' })).toHaveTextContent('System')
    fireEvent.click(screen.getByRole('radio', { name: 'Forest' }))
    await waitFor(() => expect(save).toHaveBeenCalledWith({ ...DEFAULT_PREFERENCES, palette: 'forest' }))
    expect(document.documentElement.dataset.palette).toBe('forest')
    fireEvent.click(screen.getByRole('button', { name: 'Mute interface sounds', hidden: true }))
    await waitFor(() => expect(save).toHaveBeenLastCalledWith({ ...DEFAULT_PREFERENCES, palette: 'forest', hover_sound: false }))
  })
  it('persists browser choices before immediate navigation and restores after remount', async () => {
    vi.spyOn(viewerApi, 'get').mockResolvedValue({ profile_id: null, preferences: null, hover_sounds_allowed: true })
    const first = renderWithProviders(<AppearanceRuntime />)
    await waitFor(() => expect(useThemeStore.getState().ready).toBe(true))
    act(() => {
      useThemeStore.getState().setPalette('sand')
      useThemeStore.getState().setMode('dark')
    })
    first.unmount()
    expect(JSON.parse(localStorage.getItem('sentinel.viewer-appearance.v1')!)).toMatchObject({ palette: 'sand', mode: 'dark' })
    renderWithProviders(<AppearanceRuntime />)
    await waitFor(() => expect(useThemeStore.getState().palette).toBe('sand'))
    expect(useThemeStore.getState().mode).toBe('dark')
  })
  it('does not undo a saved account theme with a GET started during its save', async () => {
    const original = { profile_id: 'person-x', preferences: DEFAULT_PREFERENCES, hover_sounds_allowed: true }
    let finishSave: (value?: unknown) => void = () => {}
    let finishRead: (value: unknown) => void = () => {}
    vi.spyOn(viewerApi, 'get').mockResolvedValueOnce(original).mockImplementationOnce(() => new Promise((resolve) => { finishRead = resolve }))
    const put = vi.spyOn(api, 'put').mockImplementationOnce(() => new Promise((resolve) => { finishSave = resolve }))
    renderWithProviders(<AppearanceRuntime />, { route: '/admin' })
    await waitFor(() => expect(useThemeStore.getState().ready).toBe(true))
    act(() => useThemeStore.getState().setPalette('forest'))
    act(() => { window.dispatchEvent(new Event('focus')) })
    await act(() => { finishSave(); return Promise.resolve() })
    await waitFor(() => expect(useThemeStore.getState().saveState).toBe('saved'))
    await act(() => { finishRead(original); return Promise.resolve() })
    expect(useThemeStore.getState().palette).toBe('forest')
    expect(put).toHaveBeenCalledWith('/appearance/me', { profile_id: 'person-x', preferences: { ...DEFAULT_PREFERENCES, palette: 'forest' } }, { keepalive: true })
  })
  it('keeps queued account changes when moving from viewer to admin', async () => {
    vi.spyOn(viewerApi, 'get').mockResolvedValue({ profile_id: 'person-x', preferences: DEFAULT_PREFERENCES, hover_sounds_allowed: true })
    let finish: (value?: unknown) => void = () => {}
    const put = vi.spyOn(api, 'put').mockImplementationOnce(() => new Promise((resolve) => { finish = resolve })).mockResolvedValue(undefined)
    renderWithProviders(<><AppearanceRuntime /><Link to="/admin">Administration</Link></>)
    await waitFor(() => expect(useThemeStore.getState().ready).toBe(true))
    act(() => {
      useThemeStore.getState().setPalette('forest')
      useThemeStore.getState().setPalette('sand')
    })
    fireEvent.click(screen.getByRole('link', { name: 'Administration' }))
    await act(() => { finish(); return Promise.resolve() })
    await waitFor(() => expect(put).toHaveBeenCalledTimes(2))
    expect(put.mock.calls[1][1]).toMatchObject({ preferences: { palette: 'sand' } })
    expect(useThemeStore.getState().palette).toBe('sand')
  })
  it('migrates the old saved mode for a shared viewer, never another account', async () => {
    localStorage.setItem('sentinel.theme', 'dark')
    vi.spyOn(viewerApi, 'get').mockResolvedValueOnce({ profile_id: null, preferences: null, hover_sounds_allowed: true })
      .mockResolvedValueOnce({ profile_id: 'person-y', preferences: DEFAULT_PREFERENCES, hover_sounds_allowed: true })
    const viewer = renderWithProviders(<AppearanceRuntime />)
    await waitFor(() => expect(useThemeStore.getState().mode).toBe('dark'))
    viewer.unmount()
    renderWithProviders(<AppearanceRuntime />, { route: '/admin' })
    await waitFor(() => expect(useThemeStore.getState().ready).toBe(true))
    expect(useThemeStore.getState().mode).toBe('system')
  })
  it('resets state between people and discards a late save completion', async () => {
    let finish: () => void = () => {}
    const pending = new Promise<void>((resolve) => { finish = resolve })
    activatePreferences('Person X', DEFAULT_PREFERENCES, () => pending)
    useThemeStore.getState().setPalette('sand')
    await Promise.resolve()
    resetPreferences()
    activatePreferences('Person Y', { ...DEFAULT_PREFERENCES, palette: 'ocean' }, vi.fn())
    finish()
    await pending
    expect(useThemeStore.getState().palette).toBe('ocean')
    expect(useThemeStore.getState().scope).toBe('Person Y')
  })
  it('global silence does not change the stored personal choice', () => {
    activatePreferences('Account', { ...DEFAULT_PREFERENCES, hover_sound: false }, vi.fn())
    useThemeStore.setState({ soundAllowed: false, policyKnown: true })
    renderWithProviders(<AppearanceDock admin={false} />)
    expect(screen.getByRole('button', { name: 'Interface sounds disabled by administrator' })).toBeDisabled()
    act(() => useThemeStore.setState({ soundAllowed: true }))
    expect(screen.getByRole('button', { name: 'Enable interface sounds' })).toBeEnabled()
    expect(useThemeStore.getState().hover_sound).toBe(false)
  })
  it('shows an honest save failure and can retry', async () => {
    const save = vi.fn().mockRejectedValueOnce(new Error()).mockResolvedValue(undefined)
    activatePreferences('Account', DEFAULT_PREFERENCES, save)
    useThemeStore.getState().setPalette('sand')
    await waitFor(() => expect(useThemeStore.getState().saveState).toBe('error'))
    useThemeStore.getState().retry()
    await waitFor(() => expect(useThemeStore.getState().saveState).toBe('saved'))
  })
  it('filters touch, disabled controls and transitions between children', () => {
    const button = document.createElement('button')
    const icon = document.createElement('span')
    button.append(icon)
    const pointer = (props: object) => ({ pointerType: 'mouse', target: icon, relatedTarget: null, ...props }) as unknown as PointerEvent
    expect(hoverTarget(pointer({}))).toBe(button)
    expect(hoverTarget(pointer({ pointerType: 'touch' }))).toBeNull()
    expect(hoverTarget(pointer({ relatedTarget: button }))).toBeNull()
    button.disabled = true
    expect(hoverTarget(pointer({}))).toBeNull()
    expect(validPreferences({ ...DEFAULT_PREFERENCES, is_admin: true })).toBe(false)
  })
  it('accepts only parent/origin/channel matched cosmetic messages', () => {
    const post = vi.spyOn(window.parent, 'postMessage').mockImplementation(() => {})
    const stop = startHaBridge('https://ha.example')
    const channel = (post.mock.calls[0][0] as { channel: string }).channel
    const message = (origin: string, nonce = channel) => window.dispatchEvent(new MessageEvent('message', {
      source: window.parent, origin, data: { type: 'sentinel:preferences:init', channel: nonce, preferences: { ...DEFAULT_PREFERENCES, palette: 'forest' } },
    }))
    message('https://evil.example')
    message('https://ha.example', 'wrong-channel')
    expect(useThemeStore.getState().ready).toBe(false)
    message('https://ha.example')
    expect(useThemeStore.getState().palette).toBe('forest')
    expect(useThemeStore.getState().soundAllowed).toBe(false)
    stop()
  })
})
