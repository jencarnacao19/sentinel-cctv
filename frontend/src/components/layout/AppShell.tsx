import { EyeOff, LogOut, MoreHorizontal } from 'lucide-react'
import { useEffect, useState } from 'react'
import { NavLink, Outlet, useLocation, useNavigate } from 'react-router-dom'

import { Logo } from '@/components/Logo'
import { NAV_ITEMS, NAV_SECTIONS, titleForPath } from '@/components/layout/navigation'
import { ThemeToggle } from '@/components/layout/ThemeToggle'
import { useLargeTitleVisible } from '@/components/layout/useLargeTitle'
import { Button } from '@/components/ui/button'
import {
  Dialog,
  DialogBody,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { useCurrentUser, useLogout } from '@/features/auth/useAuth'
import {
  PrivacyModeBanner,
  PrivacyModeControl,
} from '@/features/privacy/PrivacyModeControl'
import { usePrivacyMode } from '@/features/privacy/usePrivacyMode'
import { cn } from '@/lib/utils'

/** The four destinations that earn a permanent place under the thumb. */
const TAB_ITEMS = NAV_ITEMS.filter((item) => item.primary)

function SidebarNav() {
  return (
    <nav aria-label="Main" className="space-y-5">
      {NAV_SECTIONS.map((section) => (
        <div key={section}>
          <p className="mb-1.5 px-3 text-[11px] font-medium uppercase tracking-wider text-(--color-fg-subtle)">
            {section}
          </p>
          <div className="space-y-0.5">
            {NAV_ITEMS.filter((item) => item.section === section).map(
              ({ to, label, icon: Icon }) => (
                <NavLink
                  key={to}
                  to={to}
                  end={to === '/admin'}
                  className={({ isActive }) =>
                    cn(
                      'group flex min-h-8.5 items-center gap-2.5 rounded-(--radius-md) px-3 text-[13px]',
                      'transition-colors duration-(--duration-fast)',
                      isActive
                        ? 'bg-(--color-accent)/10 font-semibold text-(--color-accent) dark:bg-(--color-accent)/15 dark:text-white'
                        : 'font-medium text-(--color-fg-muted) hover:bg-(--color-surface-hover) hover:text-(--color-fg)',
                    )
                  }
                >
                  {({ isActive }) => (
                    <>
                      <Icon
                        className={cn(
                          'size-4 shrink-0 transition-transform duration-(--duration-fast)',
                          isActive
                            ? 'stroke-[2.25] text-(--color-accent)'
                            : 'stroke-[1.75] text-(--color-fg-subtle) group-hover:text-(--color-fg)',
                        )}
                        aria-hidden="true"
                      />
                      <span className="truncate">{label}</span>
                    </>
                  )}
                </NavLink>
              ),
            )}
          </div>
        </div>
      ))}
    </nav>
  )
}

/**
 * The phone tab bar: authentic iOS UITabBar experience.
 */
function TabBar({ onMore, moreOpen }: { onMore: () => void; moreOpen: boolean }) {
  return (
    <nav
      aria-label="Primary"
      className={cn(
        'material-chrome fixed inset-x-0 bottom-0 z-30 border-t border-(--color-border)',
        'pb-safe px-safe lg:hidden',
      )}
    >
      <div className="mx-auto flex max-w-lg items-stretch">
        {TAB_ITEMS.map(({ to, label, shortLabel, icon: Icon }) => (
          <NavLink
            key={to}
            to={to}
            end={to === '/admin'}
            className={({ isActive }) =>
              cn(
                'flex min-h-[3.25rem] flex-1 flex-col items-center justify-center gap-1 px-1 pt-1.5 pb-1',
                'text-[10px] tracking-tight transition-colors duration-(--duration-fast)',
                'active:opacity-70',
                isActive
                  ? 'font-semibold text-(--color-accent)'
                  : 'font-medium text-(--color-fg-subtle)',
              )
            }
          >
            {({ isActive }) => (
              <>
                <Icon
                  className={cn(
                    'size-5 shrink-0 transition-transform duration-(--duration-fast)',
                    isActive ? 'stroke-[2.25]' : 'stroke-[1.75]',
                  )}
                  aria-hidden="true"
                />
                <span className="max-w-full truncate">{shortLabel ?? label}</span>
              </>
            )}
          </NavLink>
        ))}
        <button
          type="button"
          onClick={onMore}
          aria-expanded={moreOpen}
          aria-haspopup="dialog"
          className={cn(
            'flex min-h-[3.25rem] flex-1 flex-col items-center justify-center gap-1 px-1 pt-1.5 pb-1',
            'text-[10px] tracking-tight transition-colors duration-(--duration-fast)',
            'active:opacity-70',
            moreOpen
              ? 'font-semibold text-(--color-accent)'
              : 'font-medium text-(--color-fg-subtle)',
          )}
        >
          <MoreHorizontal
            className={cn('size-5 shrink-0', moreOpen ? 'stroke-[2.25]' : 'stroke-[1.75]')}
            aria-hidden="true"
          />
          <span>More</span>
        </button>
      </div>
    </nav>
  )
}

function MoreSheet({
  open,
  onOpenChange,
  username,
  onSignOut,
  signingOut,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  username: string | null
  onSignOut: () => void
  signingOut: boolean
}) {
  const secondary = NAV_ITEMS.filter((item) => !item.primary)

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-sm">
        <DialogHeader>
          <DialogTitle>More</DialogTitle>
          <DialogDescription>
            Every other Sentinel screen, plus the controls for this session.
          </DialogDescription>
        </DialogHeader>
        <DialogBody className="space-y-5 pb-safe-gutter">
          <div className="space-y-0.5">
            {secondary.map(({ to, label, icon: Icon }) => (
              <NavLink
                key={to}
                to={to}
                onClick={() => onOpenChange(false)}
                className={({ isActive }) =>
                  cn(
                    'flex min-h-11 items-center gap-3 rounded-(--radius-md) px-3 text-sm',
                    'transition-colors duration-(--duration-fast)',
                    isActive
                      ? 'bg-(--color-accent-subtle) font-semibold text-(--color-fg)'
                      : 'font-medium text-(--color-fg) hover:bg-(--color-surface-hover)',
                  )
                }
              >
                {({ isActive }) => (
                  <>
                    <Icon
                      className={cn(
                        'size-4.5 shrink-0',
                        isActive ? 'text-(--color-accent)' : 'text-(--color-fg-subtle)',
                      )}
                      aria-hidden="true"
                    />
                    {label}
                  </>
                )}
              </NavLink>
            ))}
          </div>

          <div className="space-y-3 border-t border-(--color-border) pt-4">
            <PrivacyModeControl />
            <div className="flex items-center justify-between gap-3">
              <span className="text-sm text-(--color-fg-muted)">Appearance</span>
              <ThemeToggle />
            </div>
          </div>

          {username ? (
            <div className="flex items-center justify-between gap-3 border-t border-(--color-border) pt-4">
              <span className="min-w-0">
                <span className="block text-[11px] font-semibold tracking-wide text-(--color-fg-subtle)">
                  Signed in
                </span>
                <span className="mt-0.5 block truncate text-sm">{username}</span>
              </span>
              <Button variant="secondary" onClick={onSignOut} loading={signingOut}>
                <LogOut aria-hidden="true" />
                Sign out
              </Button>
            </div>
          ) : null}
        </DialogBody>
      </DialogContent>
    </Dialog>
  )
}

export function AppShell() {
  const [moreOpen, setMoreOpen] = useState(false)
  const location = useLocation()
  const navigate = useNavigate()
  const { data: user } = useCurrentUser()
  const logout = useLogout()
  const privacy = usePrivacyMode()
  const title = titleForPath(location.pathname)
  const largeTitleVisible = useLargeTitleVisible(location.pathname)

  // Close the sheet on navigation, otherwise it stays over the new page.
  useEffect(() => {
    setMoreOpen(false)
  }, [location.pathname])

  // Sign-out always lands on the login screen, even if the request failed --
  // the local session is gone either way.
  function handleLogout(): void {
    void (async () => {
      await logout.mutateAsync().catch(() => undefined)
      await navigate('/', { replace: true })
    })()
  }

  return (
    <div className="flex min-h-dvh bg-(--color-bg)">
      <a
        href="#main-content"
        className="sr-only focus:not-sr-only focus:absolute focus:top-2 focus:left-2 focus:z-50 focus:rounded-(--radius-md) focus:bg-(--color-surface) focus:px-3 focus:py-2 focus:text-sm"
      >
        Skip to content
      </a>

      {/* Desktop sidebar */}
      <aside className="hidden w-60 shrink-0 flex-col border-r border-(--color-border) bg-(--color-surface) lg:flex">
        <div className="flex h-16 items-center gap-3 border-b border-(--color-border) px-5">
          <Logo className="size-7" />
          <div className="min-w-0">
            <span className="block text-sm font-semibold tracking-tight">Sentinel CCTV</span>
            <span className="mt-0.5 block text-[11px] text-(--color-fg-subtle)">
              Recorder control
            </span>
          </div>
        </div>
        <div className="flex-1 overflow-y-auto px-3 py-5">
          <SidebarNav />
        </div>
        <div className="space-y-3 border-t border-(--color-border) p-3">
          <PrivacyModeControl />
          <div className="flex items-center justify-between gap-2 px-1">
            <span className="text-xs text-(--color-fg-muted)">Appearance</span>
            <ThemeToggle />
          </div>
          {user ? (
            <div className="flex items-center justify-between gap-2 border-t border-(--color-border) px-1 pt-3">
              <div className="min-w-0">
                <span className="block text-[11px] font-semibold tracking-wide text-(--color-fg-subtle)">
                  Admin session
                </span>
                <span className="mt-0.5 block truncate text-xs text-(--color-fg-muted)">
                  {user.username}
                </span>
              </div>
              <Button
                variant="ghost"
                size="icon"
                onClick={handleLogout}
                loading={logout.isPending}
                aria-label="Sign out"
                title="Sign out"
              >
                <LogOut aria-hidden="true" />
              </Button>
            </div>
          ) : null}
        </div>
      </aside>

      <div className="flex min-w-0 flex-1 flex-col">
        {/*
          Navigation bar and privacy banner stick together: a banner that says
          every camera is paused must not be the first thing to scroll away.
        */}
        <div className="sticky top-0 z-30">
          <header
            className={cn(
              'material-chrome flex items-center gap-3 border-b border-(--color-border)',
              'h-[calc(3.25rem+env(safe-area-inset-top,0px))] px-4 pt-safe lg:hidden',
            )}
          >
            <Logo className="size-6 shrink-0" />
            {/* A duplicate of the page's own heading, revealed only once that
                heading has scrolled behind this bar. The bar's height never
                changes, so nothing shifts as it appears or as the first query
                resolves. Hidden from assistive technology: the page's `<h1>`
                is the real title. */}
            <span
              aria-hidden="true"
              className={cn(
                'min-w-0 flex-1 truncate text-[17px] font-semibold tracking-[-0.01em]',
                'transition-opacity duration-(--duration-base)',
                largeTitleVisible ? 'opacity-0' : 'opacity-100',
              )}
            >
              {title}
            </span>
            {privacy.data?.enabled ? (
              <span
                className="flex size-8 shrink-0 items-center justify-center rounded-full bg-(--color-warning)/15 text-(--color-warning)"
                title="Privacy mode is on"
              >
                <EyeOff className="size-4" aria-hidden="true" />
                <span className="sr-only">Privacy mode is on</span>
              </span>
            ) : null}
          </header>

          <PrivacyModeBanner />
        </div>

        <main
          id="main-content"
          className={cn(
            'min-w-0 flex-1 px-4 py-5 sm:px-6 sm:py-6 lg:px-8',
            // Clears the fixed tab bar and the home indicator, so the last row
            // of a list is never trapped underneath it.
            'pb-[calc(4.5rem+env(safe-area-inset-bottom,0px))] lg:pb-8',
          )}
        >
          <Outlet />
        </main>

        <TabBar onMore={() => setMoreOpen(true)} moreOpen={moreOpen} />
      </div>

      <MoreSheet
        open={moreOpen}
        onOpenChange={setMoreOpen}
        username={user?.username ?? null}
        onSignOut={handleLogout}
        signingOut={logout.isPending}
      />
    </div>
  )
}
