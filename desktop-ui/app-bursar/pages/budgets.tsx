import { useMemo } from 'react'

import { cn } from '@/lib/utils'

import { useBursarMode } from '../bursar-mode'
import { compact, money, pct, Panel, StatCard } from '../components/primitives'
import { type BudgetRow, useBursarBudgets, useBursarMarket } from '../use-bursar'

import { BursarPageHeader } from './page-header'

// Budgets have no source column (live teams are auto-created as `live:<sid>`),
// so we split them by the team-name convention the gate uses: live traffic is
// attributed to `live:*` envelopes, the firehose to the named preset teams.
const isLiveTeam = (team: string) => team.startsWith('live:')

// Utilization tone: healthy under 80%, watch 80–99%, blown at/over cap.
function utilTone(value: number | null): 'gain' | 'loss' | 'warn' {
  if (value == null) {
    return 'gain'
  }
  if (value >= 1) {
    return 'loss'
  }
  if (value >= 0.8) {
    return 'warn'
  }
  return 'gain'
}

const TONE_BAR: Record<'gain' | 'loss' | 'warn', string> = {
  gain: 'bg-(--bursar-gain)',
  loss: 'bg-(--bursar-loss)',
  warn: 'bg-(--bursar-warn)'
}
const TONE_TEXT: Record<'gain' | 'loss' | 'warn', string> = {
  gain: 'text-(--bursar-gain)',
  loss: 'text-(--bursar-loss)',
  warn: 'text-(--bursar-warn)'
}

function BudgetMeter({ row }: { row: BudgetRow }) {
  const used = row.pct_used ?? (row.cap > 0 ? row.spent / row.cap : 0)
  const tone = utilTone(used)
  const width = `${Math.min(100, Math.max(0, used * 100))}%`

  return (
    <div className="grid grid-cols-[minmax(0,10rem)_minmax(0,1fr)_auto] items-center gap-3 py-2.5">
      <div className="min-w-0">
        <div className="truncate text-sm font-medium text-foreground">{row.team}</div>
        <div className="text-xs text-(--ui-text-tertiary)">{row.period}</div>
      </div>
      <div className="flex items-center gap-3">
        <div className="h-2 min-w-0 flex-1 overflow-hidden rounded-full bg-(--ui-bg-quinary)">
          <div className={cn('h-full rounded-full transition-all', TONE_BAR[tone])} style={{ width }} />
        </div>
        <span className={cn('w-12 shrink-0 text-right text-xs font-medium tabular-nums', TONE_TEXT[tone])}>
          {pct(used)}
        </span>
      </div>
      <div className="text-right text-xs tabular-nums text-(--ui-text-secondary)">
        {money(row.spent)} <span className="text-(--ui-text-quaternary)">/ {money(row.cap)}</span>
      </div>
    </div>
  )
}

export function BudgetsPage() {
  const mode = useBursarMode()
  const { data: budgetData } = useBursarBudgets()
  const { data: queuedData } = useBursarMarket('queued', 500)

  const budgets = (budgetData?.budgets ?? []).filter(row => (mode === 'live' ? isLiveTeam(row.team) : !isLiveTeam(row.team)))

  const totals = useMemo(
    () =>
      budgets.reduce(
        (acc, row) => ({
          cap: acc.cap + row.cap,
          remaining: acc.remaining + row.remaining,
          spent: acc.spent + row.spent
        }),
        { cap: 0, remaining: 0, spent: 0 }
      ),
    [budgets]
  )
  const nearCap = budgets.filter(row => (row.pct_used ?? 0) >= 0.8).length

  // Queries the gate is holding back, grouped by team — the consequence of a
  // budget running dry. The market endpoint is the source of the queued lane.
  const queuedByTeam = useMemo(() => {
    const counts = new Map<string, number>()

    for (const query of queuedData?.queries ?? []) {
      counts.set(query.team, (counts.get(query.team) ?? 0) + 1)
    }

    return [...counts.entries()].map(([team, count]) => ({ count, team })).sort((a, b) => b.count - a.count)
  }, [queuedData])
  const totalQueued = queuedData?.counts.queued ?? queuedData?.queries.length ?? 0

  const sorted = [...budgets].sort((a, b) => (b.pct_used ?? 0) - (a.pct_used ?? 0))

  return (
    <>
      <BursarPageHeader
        subtitle="Hard pre-execution budgets per team — the gate every query clears before it can run. No overspend, ever."
        title="Budgets"
      />

      <div className="flex min-h-0 flex-1 flex-col gap-4 overflow-y-auto pb-6">
        <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
          <StatCard hint={`${compact(budgets.length)} teams`} label="Total caps" value={money(totals.cap)} />
          <StatCard label="Committed" value={money(totals.spent)} />
          <StatCard accent="gain" label="Headroom" value={money(totals.remaining)} />
          <StatCard
            accent={nearCap > 0 ? 'loss' : 'gain'}
            hint="at or above 80% of cap"
            label="Near limit"
            value={`${nearCap} / ${budgets.length}`}
          />
        </div>

        <Panel className="shrink-0" subtitle="committed vs cap, this period" title="Team utilization">
          <div className="px-4 py-1">
            {budgets.length === 0 ? (
              <div className="py-8 text-center text-sm text-(--ui-text-tertiary)">
                {mode === 'live' ? 'No live teams yet — arm the gate and send a chat to open an envelope.' : 'Loading budgets…'}
              </div>
            ) : (
              <div className="divide-y divide-(--ui-stroke-tertiary)">
                {sorted.map(row => (
                  <BudgetMeter key={row.team} row={row} />
                ))}
              </div>
            )}
          </div>
        </Panel>

        <Panel
          className="shrink-0"
          subtitle={`${compact(totalQueued)} queries held — budget gate working`}
          title="Queued on budget"
        >
          {queuedByTeam.length === 0 ? (
            <div className="px-4 py-6 text-center text-sm text-(--ui-text-tertiary)">
              Nothing queued — every team has headroom.
            </div>
          ) : (
            <div className="flex flex-wrap gap-2 px-4 py-4">
              {queuedByTeam.map(item => (
                <div
                  className="flex items-center gap-2 rounded-lg border border-(--ui-stroke-tertiary) bg-(--ui-bg-elevated) px-3 py-1.5"
                  key={item.team}
                >
                  <span className="text-sm font-medium text-foreground">{item.team}</span>
                  <span className="rounded-full bg-(--ui-bg-quinary) px-2 py-0.5 text-xs font-medium tabular-nums text-(--bursar-queued)">
                    {compact(item.count)} queued
                  </span>
                </div>
              ))}
            </div>
          )}
        </Panel>
      </div>
    </>
  )
}
