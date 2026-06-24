import { type ReactNode, useEffect, useState } from 'react'

import { cn } from '@/lib/utils'

// ---- Formatting -------------------------------------------------------------

export function money(value: number | null | undefined, opts: { cents?: boolean } = {}): string {
  if (value == null || Number.isNaN(value)) {
    return '—'
  }
  const digits = opts.cents === false ? 0 : value !== 0 && Math.abs(value) < 0.01 ? 4 : 2
  return `$${value.toLocaleString(undefined, { maximumFractionDigits: digits, minimumFractionDigits: digits })}`
}

export function pct(value: number | null | undefined): string {
  if (value == null || Number.isNaN(value)) {
    return '—'
  }
  return `${(value * 100).toFixed(value < 0.1 ? 1 : 0)}%`
}

export function compact(value: number | null | undefined): string {
  if (value == null || Number.isNaN(value)) {
    return '—'
  }
  return value.toLocaleString(undefined, { maximumFractionDigits: 0 })
}

// ---- Layout primitives ------------------------------------------------------

export function StatCard({
  accent,
  hint,
  label,
  value
}: {
  accent?: 'gain' | 'loss' | 'neutral'
  hint?: ReactNode
  label: string
  value: ReactNode
}) {
  return (
    <div className="flex flex-col gap-1 rounded-xl border border-(--ui-stroke-tertiary) bg-(--ui-bg-card) px-4 py-3">
      <span className="text-xs font-medium uppercase tracking-wide text-(--ui-text-tertiary)">{label}</span>
      <span
        className={cn(
          'text-2xl font-semibold tabular-nums tracking-tight',
          accent === 'gain' && 'text-(--bursar-gain)',
          accent === 'loss' && 'text-(--bursar-loss)',
          (accent === 'neutral' || !accent) && 'text-foreground'
        )}
      >
        {value}
      </span>
      {hint != null && <span className="text-xs text-(--ui-text-tertiary)">{hint}</span>}
    </div>
  )
}

export function Panel({
  actions,
  children,
  className,
  subtitle,
  title
}: {
  actions?: ReactNode
  children: ReactNode
  className?: string
  subtitle?: string
  title?: string
}) {
  return (
    <section
      className={cn(
        'flex min-h-0 flex-col overflow-hidden rounded-xl border border-(--ui-stroke-tertiary) bg-(--ui-bg-card)',
        className
      )}
    >
      {(title || actions) && (
        <header className="flex shrink-0 items-center justify-between gap-3 border-b border-(--ui-stroke-tertiary) px-4 py-2.5">
          <div className="min-w-0">
            {title && <h2 className="truncate text-sm font-semibold text-foreground">{title}</h2>}
            {subtitle && <p className="truncate text-xs text-(--ui-text-tertiary)">{subtitle}</p>}
          </div>
          {actions}
        </header>
      )}
      {children}
    </section>
  )
}

// ---- Theme palette ----------------------------------------------------------

export interface BursarPalette {
  accent: string
  deduped: string
  gain: string
  grid: string
  loss: string
  queued: string
  serviced: string
  text: string
}

const FALLBACK: BursarPalette = {
  accent: '#0e9f6e',
  deduped: '#4c7f8c',
  gain: '#16a34a',
  grid: 'rgba(120,120,120,0.18)',
  loss: '#e5484d',
  queued: '#6366f1',
  serviced: '#0e9f6e',
  text: 'rgba(120,120,120,0.9)'
}

// Recharts applies colors as SVG `fill`/`stroke` attributes, which don't
// resolve CSS `var()`. So read the computed values off the live `.bursar`
// scope once mounted (re-reads on theme flip via the class/style observer
// is overkill for the demo — a remount covers it).
export function useBursarPalette(): BursarPalette {
  const [palette, setPalette] = useState<BursarPalette>(FALLBACK)

  useEffect(() => {
    const root = document.querySelector('.bursar')
    if (!root) {
      return
    }
    const cs = getComputedStyle(root)
    const read = (name: string, fallback: string) => cs.getPropertyValue(name).trim() || fallback

    setPalette({
      accent: read('--bursar-accent', FALLBACK.accent),
      deduped: read('--ui-cyan', FALLBACK.deduped),
      gain: read('--bursar-gain', FALLBACK.gain),
      grid: read('--ui-stroke-tertiary', FALLBACK.grid),
      loss: read('--bursar-loss', FALLBACK.loss),
      queued: read('--bursar-queued', FALLBACK.queued),
      serviced: read('--bursar-accent', FALLBACK.serviced),
      text: read('--ui-text-tertiary', FALLBACK.text)
    })
  }, [])

  return palette
}
