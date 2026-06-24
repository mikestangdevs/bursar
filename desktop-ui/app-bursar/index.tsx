import { useLocation } from 'react-router-dom'

import { cn } from '@/lib/utils'

import { PAGE_INSET_X } from '../layout-constants'
import { BURSAR_BUDGETS_ROUTE, BURSAR_LEDGER_ROUTE, BURSAR_ROUTE, BURSAR_ROUTING_ROUTE } from '../routes'

import { BursarModeBar } from './components/mode-bar'
import { BudgetsPage } from './pages/budgets'
import { LedgerPage } from './pages/ledger'
import { RoutingPage } from './pages/routing'
import { TradingFloorPage } from './pages/trading-floor'
import { BursarLiveProvider } from './use-bursar-live'

import type { ReactNode } from 'react'

interface BursarPage {
  path: string
  render: () => ReactNode
}

// Order mirrors the sidebar dropdown. Trading Floor is the hero/landing page.
const PAGES: readonly BursarPage[] = [
  { path: BURSAR_ROUTE, render: () => <TradingFloorPage /> },
  { path: BURSAR_LEDGER_ROUTE, render: () => <LedgerPage /> },
  { path: BURSAR_BUDGETS_ROUTE, render: () => <BudgetsPage /> },
  { path: BURSAR_ROUTING_ROUTE, render: () => <RoutingPage /> }
]

// Resolve the active page from the path. Sub-pages own a `/bursar/<x>` prefix;
// anything else under `/bursar` (including `/bursar` itself) is the Trading
// Floor landing page.
function activePageFor(pathname: string): BursarPage {
  const sub = PAGES.find(page => page.path !== BURSAR_ROUTE && pathname.startsWith(page.path))

  return sub ?? PAGES[0]
}

/**
 * Bursar rendered INLINE in the desktop app's main pane (PaneMain), so the
 * Hermes chrome — titlebar, chat sidebar (with the Bursar page dropdown),
 * statusbar — stays in place. `.bursar` on the scroll root scopes the accent
 * theme. The four pages read the engine over the same backend the desktop app
 * already runs (`window.hermesDesktop.api` → `/api/plugins/bursar`).
 */
export function BursarView() {
  const { pathname } = useLocation()
  const active = activePageFor(pathname)

  return (
    <BursarLiveProvider>
      <div
        className={cn(
          'bursar flex h-full min-h-0 flex-col overflow-y-auto bg-background pb-10 pt-[calc(var(--titlebar-height)+0.75rem)]',
          PAGE_INSET_X
        )}
      >
        <BursarModeBar />
        {active.render()}
      </div>
    </BursarLiveProvider>
  )
}
