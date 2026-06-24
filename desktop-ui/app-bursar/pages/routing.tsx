import { useMemo } from 'react'

import { cn } from '@/lib/utils'

import { compact, money, Panel, StatCard } from '../components/primitives'
import { useBursarCatalog, useBursarRouting, useBursarStats } from '../use-bursar'

import { BursarPageHeader } from './page-header'

function price(value: number | null | undefined): string {
  if (value == null || Number.isNaN(value)) {
    return '—'
  }
  return `$${value.toLocaleString(undefined, { maximumFractionDigits: 2, minimumFractionDigits: 2 })}`
}

export function RoutingPage() {
  const { data: routingData } = useBursarRouting()
  const { data: catalogData } = useBursarCatalog()
  const { data: stats } = useBursarStats()

  const routing = routingData?.routing ?? []
  const catalog = catalogData?.catalog ?? []

  // Queries served by each model (for the catalog "volume" column).
  const queriesByModel = useMemo(() => new Map(routing.map(row => [row.model, row.queries])), [routing])

  const totals = useMemo(
    () =>
      routing.reduce(
        (acc, row) => ({ cost: acc.cost + row.token_cost, queries: acc.queries + row.queries, tokens: acc.tokens + row.tokens }),
        { cost: 0, queries: 0, tokens: 0 }
      ),
    [routing]
  )
  const blendedPerMtok = totals.tokens > 0 ? (totals.cost / totals.tokens) * 1_000_000 : null

  const sortedRouting = [...routing].sort((a, b) => b.queries - a.queries)
  const enabledCount = catalog.filter(row => row.in_use).length
  // Distinct pricing_version strings, for the sourced footnote.
  const sources = useMemo(
    () => [...new Set(catalog.map(row => row.source).filter((value): value is string => Boolean(value)))],
    [catalog]
  )

  return (
    <>
      <BursarPageHeader
        subtitle="The model price catalog and every routing decision — work sent to the cheapest model that clears its value bar."
        title="Routing & Catalog"
      />

      <div className="flex min-h-0 flex-1 flex-col gap-4 overflow-y-auto pb-6">
        <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
          <StatCard accent="gain" hint="vs. all-premium routing" label="Saved by routing" value={money(stats?.saved_routing)} />
          <StatCard hint={`of ${compact(catalogData?.count)} in catalog`} label="Models engaged" value={compact(enabledCount)} />
          <StatCard hint={`${compact(totals.queries)} queries`} label="Blended price / Mtok" value={price(blendedPerMtok)} />
          <StatCard
            hint="cheapest → premium"
            label="Catalog spread"
            value={catalogData?.spread ? `${catalogData.spread.toLocaleString(undefined, { maximumFractionDigits: 0 })}×` : '—'}
          />
        </div>

        <Panel className="shrink-0" subtitle="where serviced work actually went" title="Routing decisions">
          {sortedRouting.length === 0 ? (
            <div className="px-4 py-8 text-center text-sm text-(--ui-text-tertiary)">
              No routing yet — service the market to see decisions.
            </div>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full border-collapse text-xs">
                <thead className="bg-(--ui-bg-elevated) text-(--ui-text-tertiary)">
                  <tr className="[&>th]:px-3 [&>th]:py-2 [&>th]:text-left [&>th]:font-medium">
                    <th>Model</th>
                    <th className="w-24 text-right">$/Mtok</th>
                    <th className="w-20 text-right">Queries</th>
                    <th className="w-24 text-right">Compute</th>
                    <th className="w-32">Share</th>
                  </tr>
                </thead>
                <tbody className="tabular-nums">
                  {sortedRouting.map(row => {
                    const share = totals.queries > 0 ? row.queries / totals.queries : 0
                    return (
                      <tr className="border-t border-(--ui-stroke-tertiary) [&>td]:px-3 [&>td]:py-1.5" key={row.model}>
                        <td className="font-medium text-foreground">{row.model}</td>
                        <td className="text-right text-(--ui-text-secondary)">{price(row.price_per_mtok)}</td>
                        <td className="text-right text-foreground">{compact(row.queries)}</td>
                        <td className="text-right text-(--ui-text-tertiary)">{money(row.token_cost)}</td>
                        <td>
                          <div className="flex items-center gap-2">
                            <div className="h-1.5 min-w-0 flex-1 overflow-hidden rounded-full bg-(--ui-bg-quinary)">
                              <div className="h-full rounded-full bg-(--bursar-accent)" style={{ width: `${share * 100}%` }} />
                            </div>
                            <span className="w-9 shrink-0 text-right text-(--ui-text-tertiary)">{Math.round(share * 100)}%</span>
                          </div>
                        </td>
                      </tr>
                    )
                  })}
                </tbody>
              </table>
            </div>
          )}
        </Panel>

        <Panel
          className="shrink-0"
          subtitle={sources.length ? `sourced · ${sources.join(' · ')}` : 'the menu Bursar prices against'}
          title="Model catalog"
        >
          {catalog.length === 0 ? (
            <div className="px-4 py-8 text-center text-sm text-(--ui-text-tertiary)">Loading catalog…</div>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full border-collapse text-xs">
                <thead className="bg-(--ui-bg-elevated) text-(--ui-text-tertiary)">
                  <tr className="[&>th]:px-3 [&>th]:py-2 [&>th]:text-left [&>th]:font-medium">
                    <th>Model</th>
                    <th className="w-24">Provider</th>
                    <th className="w-24 text-right">Input</th>
                    <th className="w-24 text-right">Output</th>
                    <th className="w-24 text-right">Blended</th>
                    <th className="w-24">Status</th>
                  </tr>
                </thead>
                <tbody className="tabular-nums">
                  {catalog.map(row => {
                    const queries = queriesByModel.get(row.model)
                    return (
                      <tr className="border-t border-(--ui-stroke-tertiary) [&>td]:px-3 [&>td]:py-1.5" key={`${row.provider}/${row.model}`}>
                        <td>
                          <span className="flex items-center gap-2">
                            <span className={cn('size-1.5 rounded-full', row.in_use ? 'bg-(--bursar-accent)' : 'bg-(--ui-text-quaternary)')} />
                            <span className={cn('truncate', row.in_use ? 'font-medium text-foreground' : 'text-(--ui-text-secondary)')}>
                              {row.model}
                            </span>
                          </span>
                        </td>
                        <td className="truncate text-(--ui-text-tertiary)">{row.provider}</td>
                        <td className="text-right text-(--ui-text-tertiary)">{price(row.input)}</td>
                        <td className="text-right text-(--ui-text-tertiary)">{price(row.output)}</td>
                        <td className="text-right text-(--ui-text-secondary)">{price(row.blended)}</td>
                        <td className="text-xs">
                          {row.in_use ? (
                            <span className="font-medium text-(--bursar-gain)">
                              In use{queries ? ` · ${compact(queries)}` : ''}
                            </span>
                          ) : (
                            <span className="text-(--ui-text-quaternary)">Idle</span>
                          )}
                        </td>
                      </tr>
                    )
                  })}
                </tbody>
              </table>
            </div>
          )}
        </Panel>
      </div>
    </>
  )
}
