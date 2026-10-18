import type { ReactNode } from 'react'
import { Link } from 'react-router-dom'

import { Logo } from '@/components/Logo'

const SECURITY_FACTS = [
  ['Footage', 'Stored on the disks configured for this recorder'],
  ['Credentials', 'Encrypted at rest on this Sentinel server'],
  ['Session', 'Kept in a protected, HttpOnly browser cookie'],
] as const

interface AuthLayoutProps {
  eyebrow: string
  title: string
  description: string
  children: ReactNode
  footer: ReactNode
}

/**
 * A product-specific entry screen, shared by sign-in and first-run setup.
 *
 * The letter-spaced monospace capitals are gone. Six of them on one screen --
 * an eyebrow, a strapline, three definition terms and a link -- made the page
 * look like a template rather than an application, and none of them were
 * technical values that a monospace face helps to read.
 *
 * On a phone the marketing column is not rendered at all, so the sign-in form
 * is the first thing on screen rather than the second, and the whole layout
 * respects the safe-area insets: this is the one page someone may open from a
 * home-screen shortcut, full-bleed under the notch.
 */
export function AuthLayout({
  eyebrow,
  title,
  description,
  children,
  footer,
}: AuthLayoutProps) {
  return (
    <main className="min-h-dvh bg-(--color-bg)">
      <div className="grid min-h-dvh lg:grid-cols-[minmax(22rem,0.9fr)_minmax(30rem,1.1fr)]">
        <section className="relative hidden overflow-hidden border-r border-(--color-border) bg-(--color-bg-subtle) lg:flex lg:flex-col lg:justify-between lg:p-10 xl:p-14">
          <div className="flex items-center gap-3">
            <Logo className="size-8" />
            <div>
              <p className="text-sm font-semibold tracking-tight">Sentinel CCTV</p>
              <p className="mt-0.5 text-xs text-(--color-fg-subtle)">Local recorder console</p>
            </div>
          </div>

          <div className="my-16 max-w-xl">
            <p className="text-sm font-semibold text-(--color-accent)">Private by architecture</p>
            <h2 className="mt-3 max-w-lg text-4xl leading-[1.08] font-semibold tracking-[-0.035em] xl:text-5xl">
              Local surveillance control.
            </h2>
            <p className="mt-5 max-w-lg text-base leading-7 text-(--color-fg-muted)">
              Sentinel records, indexes and serves footage from this machine. Administration is
              kept separate from the restricted camera viewer.
            </p>
          </div>

          <dl className="border-t border-(--color-border)">
            {SECURITY_FACTS.map(([term, detail]) => (
              <div
                key={term}
                className="grid grid-cols-[7rem_1fr] gap-4 border-b border-(--color-border) py-4 last:border-b-0"
              >
                <dt className="text-sm font-medium">{term}</dt>
                <dd className="text-sm leading-5 text-(--color-fg-muted)">{detail}</dd>
              </div>
            ))}
          </dl>
        </section>

        <section className="pt-safe pb-safe flex min-h-dvh flex-col px-5 py-6 sm:px-10 lg:px-16 lg:py-10 xl:px-24">
          <div className="flex items-center justify-between lg:justify-end">
            <Link to="/" className="flex items-center gap-2.5 lg:hidden">
              <Logo className="size-7" />
              <span className="text-sm font-semibold">Sentinel CCTV</span>
            </Link>
            <Link
              to="/"
              className="flex min-h-11 items-center text-sm text-(--color-fg-muted) hover:text-(--color-fg)"
            >
              Camera viewer
            </Link>
          </div>

          <div className="my-auto w-full max-w-md py-10 lg:mx-auto">
            <header className="mb-7">
              <p className="text-sm font-semibold text-(--color-accent)">{eyebrow}</p>
              <h1 className="mt-2 text-3xl font-semibold tracking-[-0.03em]">{title}</h1>
              <p className="mt-2 max-w-sm text-sm leading-6 text-(--color-fg-muted)">
                {description}
              </p>
            </header>

            <div className="border-t border-(--color-border) pt-6">{children}</div>
            <div className="mt-6 border-t border-(--color-border) pt-5 text-xs leading-5 text-(--color-fg-subtle)">
              {footer}
            </div>
          </div>
        </section>
      </div>
    </main>
  )
}
