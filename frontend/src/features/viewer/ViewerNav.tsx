import { History, LockKeyhole, LogOut, Video } from 'lucide-react'
import { Link } from 'react-router-dom'

import { Logo } from '@/components/Logo'
import { ThemeToggle } from '@/components/layout/ThemeToggle'
import { Button } from '@/components/ui/button'
import { useCurrentUser, useLogout } from '@/features/auth/useAuth'
import { cn } from '@/lib/utils'

/**
 * The viewer's navigation bar.
 *
 * One component for both viewer screens, which had drifted into two copies with
 * different translucency, a different current-page treatment and one of them
 * marking the wrong tab as selected.
 *
 * `pt-safe` matters more here than anywhere else in the application: this is the
 * page Home Assistant embeds full-bleed on an iPhone, so without the inset the
 * title sits underneath the status bar and the notch cuts the logo in half.
 */
export function ViewerNav({ current }: { current: 'live' | 'playback' }) {
  // Only a password sign-in can be signed out of; Home Assistant and
  // Cloudflare Access visitors have no account here.
  const account = useCurrentUser().data
  const logout = useLogout()

  return (
    <header
      className={cn(
        'material-chrome sticky top-0 z-30 border-b border-(--color-border)',
        'pt-safe px-safe',
      )}
    >
      <div className="flex min-h-14 items-center gap-2 px-3 sm:gap-3 sm:px-5">
        <Logo className="size-6 shrink-0" />
        <div className="min-w-0">
          <p className="truncate text-sm font-semibold">Sentinel CCTV</p>
          <p className="hidden text-xs text-(--color-fg-subtle) sm:block">
            Live, playback and permanent clips
          </p>
        </div>
        <nav className="ml-auto flex items-center gap-1" aria-label="Viewer">
          <Button
            asChild
            variant={current === 'live' ? 'secondary' : 'ghost'}
            className="min-h-11 px-2.5 sm:px-3"
          >
            {/* The visible word disappears below `sm`, so the accessible name
                comes from the label rather than from text a phone never shows. */}
            <Link
              to="/"
              aria-label="Live"
              aria-current={current === 'live' ? 'page' : undefined}
            >
              <Video aria-hidden="true" />
              <span className="hidden sm:inline">Live</span>
            </Link>
          </Button>
          <Button
            asChild
            variant={current === 'playback' ? 'secondary' : 'ghost'}
            className="min-h-11 px-2.5 sm:px-3"
          >
            <Link
              to="/playback"
              aria-label="Playback"
              aria-current={current === 'playback' ? 'page' : undefined}
            >
              <History aria-hidden="true" />
              <span className="hidden sm:inline">Playback</span>
            </Link>
          </Button>
          <div className="hidden sm:block">
            <ThemeToggle />
          </div>
          {account ? (
            <Button
              variant="ghost"
              className="min-h-11 px-2.5 sm:px-3"
              onClick={() => logout.mutate()}
              loading={logout.isPending}
              aria-label={`Sign out ${account.username}`}
              title={`Signed in as ${account.username}`}
            >
              <LogOut aria-hidden="true" />
              <span className="hidden sm:inline">Sign out</span>
            </Button>
          ) : null}
          <Button asChild variant="ghost" className="min-h-11 px-2.5 sm:px-3">
            <Link
              to="/admin/login"
              target="_blank"
              rel="noopener noreferrer"
              aria-label="Administrator login"
              title="Administrator login"
            >
              <LockKeyhole aria-hidden="true" />
              <span className="hidden sm:inline">Admin</span>
            </Link>
          </Button>
        </nav>
      </div>
    </header>
  )
}
