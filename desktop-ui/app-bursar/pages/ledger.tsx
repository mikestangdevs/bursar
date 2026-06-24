import { useMemo, useState } from 'react'

import { cn } from '@/lib/utils'

import { compact, money, Panel, StatCard } from '../components/primitives'
import { type LedgerRow, useBursarLedger } from '../use-bursar'

import { BursarPageHeader } from './page-header'

function SettlementCell({ row }: { row: LedgerRow }) {
  const stripe = Boolean(row.meter_event_id)

  return stripe ? (
    <span className="inline-flex items-center gap-1.5 font-medium text-(--bursar-gain)" title={row.meter_event_id ?? ''}>
      <span className="size-1.5 rounded-full bg-current" />
      Stripe
    </span>
  ) : (
    <span className="inline-flex items-center gap-1.5 text-(--ui-text-tertiary)">
      <span className="size-1.5 rounded-full bg-current opacity-50" />
      Local
    </span>
  )
}

export function LedgerPage() {
  const { data } = useBursarLedger()
  const [selected, setSelected] = useState<string | null>(null)

  const rows = data?.ledger ?? []
  const summary = data?.summary

  // Per-team rollup drives the filter pills (and orders them by spend).
  const teams = useMemo(() => {
    const byTeam = new Map<string, { rows: number; team: string; total: number }>()

    for (const row of rows) {
      const entry = byTeam.get(row.team) ?? { rows: 0, team: row.team, total: 0 }
      entry.total += row.total ?? 0
      entry.rows += 1
      byTeam.set(row.team, entry)
    }

    return [...byTeam.values()].sort((a, b) => b.total - a.total)
  }, [rows])

  // The statement scopes to the selected team; the summary cards stay global so
  // they always reconcile with the engine's bill.
  const visible = selected ? rows.filter(row => row.team === selected) : rows
  const subtotal = useMemo(
    () =>
      visible.reduce(
        (acc, row) => ({
          fee: acc.fee + (row.fee ?? 0),
          tokenCost: acc.tokenCost + (row.token_cost ?? 0),
          tokens: acc.tokens + (row.tokens ?? 0),
          total: acc.total + (row.total ?? 0)
        }),
        { fee: 0, tokenCost: 0, tokens: 0, total: 0 }
      ),
    [visible]
  )

  return (
    <>
      <BursarPageHeader
        subtitle="Finance-grade metered settlement — every serviced query, its compute cost, and the trading fee metered through Stripe."
        title="Ledger"
      />

      <div className="flex min-h-0 flex-1 flex-col gap-4 overflow-y-auto pb-6">
        <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
          <StatCard hint={`${compact(summary?.rows)} line items`} label="Total billed" value={money(summary?.total)} />
          <StatCard hint="model inference" label="Compute cost" value={money(summary?.token_cost)} />
          <StatCard accent="gain" hint="Bursar's take" label="Trading fees" value={money(summary?.fee)} />
          <StatCard
            hint={summary ? `${compact(summary.local_only)} local-only` : undefined}
            label="Settled via Stripe"
            value={money(summary?.settled)}
          />
        </div>

        {teams.length > 0 && (
          <div className="flex flex-wrap gap-1.5">
            <TeamPill active={selected === null} label="All teams" meta={money(summary?.total)} onClick={() => setSelected(null)} />
            {teams.map(team => (
              <TeamPill
                active={selected === team.team}
                key={team.team}
                label={team.team}
                meta={money(team.total)}
                onClick={() => setSelected(selected === team.team ? null : team.team)}
              />
            ))}
          </div>
        )}

        <Panel
          className="min-h-72 flex-1"
          subtitle={selected ? `${compact(visible.length)} line items` : 'all teams · newest first'}
          title={selected ? `${selected} statement` : 'Settlement statement'}
        >
          {!data ? (
            <div className="flex flex-1 items-center justify-center p-6 text-sm text-(--ui-text-tertiary)">
              Loading ledger…
            </div>
          ) : visible.length === 0 ? (
            <div className="flex flex-1 items-center justify-center p-6 text-sm text-(--ui-text-tertiary)">
              No settled queries yet — service the market to bill against it.
            </div>
          ) : (
            <div className="min-h-0 flex-1 overflow-y-auto">
              <table className="w-full border-collapse text-xs">
                <thead className="sticky top-0 z-10 bg-(--ui-bg-elevated) text-(--ui-text-tertiary)">
                  <tr className="[&>th]:px-3 [&>th]:py-2 [&>th]:text-left [&>th]:font-medium">
                    <th className="w-16">Query</th>
                    {!selected && <th className="w-24">Team</th>}
                    <th>Model</th>
                    <th className="w-20 text-right">Tokens</th>
                    <th className="w-24 text-right">Compute</th>
                    <th className="w-20 text-right">Fee</th>
                    <th className="w-24 text-right">Total</th>
                    <th className="w-24">Settlement</th>
                  </tr>
                </thead>
                <tbody className="tabular-nums">
                  {visible.map(row => (
                    <tr className="border-t border-(--ui-stroke-tertiary) [&>td]:px-3 [&>td]:py-1.5" key={row.id}>
                      <td className="text-(--ui-text-tertiary)">#{row.query_id ?? row.id}</td>
                      {!selected && <td className="truncate text-(--ui-text-secondary)">{row.team}</td>}
                      <td className="truncate text-(--ui-text-secondary)" title={row.model ?? undefined}>
                        {row.model ?? '—'}
                      </td>
                      <td className="text-right text-(--ui-text-tertiary)">{compact(row.tokens)}</td>
                      <td className="text-right text-(--ui-text-tertiary)">{money(row.token_cost)}</td>
                      <td className="text-right text-(--ui-text-tertiary)">{money(row.fee)}</td>
                      <td className="text-right font-medium text-foreground">{money(row.total)}</td>
                      <td>
                        <SettlementCell row={row} />
                      </td>
                    </tr>
                  ))}
                </tbody>
                <tfoot className="sticky bottom-0 bg-(--ui-bg-elevated) font-medium text-foreground">
                  <tr className="border-t border-(--ui-stroke-tertiary) [&>td]:px-3 [&>td]:py-2">
                    <td colSpan={selected ? 2 : 3} className="text-(--ui-text-tertiary)">
                      {selected ? `${selected} subtotal` : 'Total'}
                    </td>
                    <td className="text-right tabular-nums text-(--ui-text-secondary)">{compact(subtotal.tokens)}</td>
                    <td className="text-right tabular-nums text-(--ui-text-secondary)">{money(subtotal.tokenCost)}</td>
                    <td className="text-right tabular-nums text-(--bursar-gain)">{money(subtotal.fee)}</td>
                    <td className="text-right tabular-nums">{money(subtotal.total)}</td>
                    <td />
                  </tr>
                </tfoot>
              </table>
            </div>
          )}
        </Panel>
      </div>
    </>
  )
}

function TeamPill({ active, label, meta, onClick }: { active: boolean; label: string; meta: string; onClick: () => void }) {
  return (
    <button
      className={cn(
        'flex items-center gap-2 rounded-full border px-3 py-1 text-xs transition-colors',
        active
          ? 'border-(--bursar-accent) bg-(--ui-control-active-background) text-foreground'
          : 'border-(--ui-stroke-tertiary) text-(--ui-text-secondary) hover:bg-(--ui-control-hover-background) hover:text-foreground'
      )}
      onClick={onClick}
      type="button"
    >
      <span className="font-medium">{label}</span>
      <span className="tabular-nums text-(--ui-text-tertiary)">{meta}</span>
    </button>
  )
}
