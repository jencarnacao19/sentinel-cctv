import type { ReactNode } from 'react'
import { Navigate, Route, Routes, useLocation } from 'react-router-dom'

import { Logo } from '@/components/Logo'
import { AppShell } from '@/components/layout/AppShell'
import { LoginPage } from '@/features/auth/LoginPage'
import { SetupPage } from '@/features/auth/SetupPage'
import { useAuthStatus, useCurrentUser } from '@/features/auth/useAuth'
import { CamerasPage } from '@/features/cameras/CamerasPage'
import { EventsPage } from '@/features/events/EventsPage'
import { LivePage } from '@/features/live/LivePage'
import { CameraHealthPage } from '@/features/reliability/CameraHealthPage'
import { SettingsPage } from '@/features/settings/SettingsPage'
import { StoragePage } from '@/features/storage/StoragePage'
import { TimelinePage, ViewerTimelinePage } from '@/features/timeline/TimelinePage'
import { ClipsPage } from '@/features/timeline/ClipsPage'
import { ZonesPage } from '@/features/zones/ZonesPage'
import { SetupWizard } from '@/features/setup/SetupWizard'
import { ViewerPage } from '@/features/viewer/ViewerPage'
import { DashboardPage } from '@/pages/DashboardPage'
import { SystemPage } from '@/pages/SystemPage'

function FullPageSpinner() {
  return (
    <div className="flex min-h-dvh items-center justify-center bg-(--color-bg)">
      <div className="flex flex-col items-center gap-4">
        <Logo className="size-10" />
        <p className="text-sm text-(--color-fg-muted)">Connecting to Sentinel…</p>
      </div>
    </div>
  )
}

/**
 * Decides between the first-run wizard, the sign-in screen and the app.
 *
 * The order matters: a server with no administrator must always land on setup,
 * even if a stale cookie is present.
 */
function RequireAuth({ children }: { children: ReactNode }) {
  const location = useLocation()
  const status = useAuthStatus()
  const user = useCurrentUser()

  if (status.isPending || user.isPending) return <FullPageSpinner />
  if (status.data && !status.data.initialised) return <Navigate to="/setup" replace />
  if (!user.data) {
    return <Navigate to="/admin/login" replace state={{ from: location.pathname }} />
  }
  // A viewer account watches; administration is not for it.
  if (!user.data.is_admin) return <Navigate to="/" replace />
  // Until the setup steps are finished they are where the administrator works.
  if (status.data?.setup_completed === false) return <Navigate to="/setup" replace />
  return <>{children}</>
}

/** The camera wall is public only inside its separate viewer security boundary. */
function RequireInitialisedViewer({ children }: { children: ReactNode }) {
  const status = useAuthStatus()
  if (status.isPending) return <FullPageSpinner />
  if (status.data && !status.data.initialised) return <Navigate to="/setup" replace />
  return <>{children}</>
}

/** Keeps a signed-in user, and a server with no administrator yet, off the
 *  sign-in screen. */
function PublicOnly({ children }: { children: ReactNode }) {
  const status = useAuthStatus()
  const user = useCurrentUser()

  if (status.isPending || user.isPending) return <FullPageSpinner />
  if (user.data) return <Navigate to={user.data.is_admin ? '/admin' : '/'} replace />
  if (status.data && !status.data.initialised) return <Navigate to="/setup" replace />
  return <>{children}</>
}

/**
 * First run, from start to finish: the administrator account, then the setup
 * steps, then the camera wall this address serves from then on.
 */
function SetupRoute() {
  const status = useAuthStatus()
  const user = useCurrentUser()

  if (status.isPending || user.isPending) return <FullPageSpinner />
  if (!status.data?.initialised) return <SetupPage />
  if (!user.data) return <Navigate to="/admin/login" replace state={{ from: '/setup' }} />
  if (!user.data.is_admin) return <Navigate to="/" replace />
  if (status.data.setup_completed) return <Navigate to="/admin" replace />
  return <SetupWizard />
}

export function App() {
  return (
    <Routes>
      <Route path="/setup" element={<SetupRoute />} />
      <Route
        path="/admin/login"
        element={
          <PublicOnly>
            <LoginPage />
          </PublicOnly>
        }
      />
      <Route path="/login" element={<Navigate to="/admin/login" replace />} />
      <Route
        path="/embed"
        element={
          <RequireInitialisedViewer>
            <ViewerPage />
          </RequireInitialisedViewer>
        }
      />
      <Route
        path="/"
        element={
          <RequireInitialisedViewer>
            <ViewerPage />
          </RequireInitialisedViewer>
        }
      />
      <Route
        path="/playback"
        element={
          <RequireInitialisedViewer>
            <ViewerTimelinePage />
          </RequireInitialisedViewer>
        }
      />
      <Route
        path="/admin"
        element={
          <RequireAuth>
            <AppShell />
          </RequireAuth>
        }
      >
        <Route index element={<DashboardPage />} />
        <Route path="live" element={<LivePage />} />
        <Route path="timeline" element={<TimelinePage />} />
        <Route path="clips" element={<ClipsPage />} />
        <Route path="events" element={<EventsPage />} />
        <Route path="health" element={<CameraHealthPage />} />
        <Route path="cameras" element={<CamerasPage />} />
        <Route path="zones" element={<ZonesPage />} />
        <Route path="storage" element={<StoragePage />} />
        <Route path="system" element={<SystemPage />} />
        <Route path="settings" element={<SettingsPage />} />
      </Route>
      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  )
}
