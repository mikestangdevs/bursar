import { useBursarMode } from '../bursar-mode'
import { ControlBar } from '../components/control-bar'
import { LaneBar } from '../components/lane-bar'
import { OrderFeed } from '../components/order-feed'
import { money, pct, Panel, StatCard, useBursarPalette } from '../components/primitives'
import { useBursarMarket, useBursarStats } from '../use-bursar'

import { BursarPageHeader } from './page-header'

export function TradingFloorPage() {
  const palette = useBursarPalette()
  const mode = useBursarMode()
  const { data: stats } = useBursarStats()
  const { data: market } = useBursarMarket(undefined, 200)

  // Headline = the routing reduction vs the industry default: every call run on
  // the single frontier model (Bursar's core thesis — "everyone defaults to the
  // best model, and most of that spend is waste"). `cost` is real model spend;
  // `saved` is `saved_routing` (per serviced query, frontier price − routed
  // price), so the floor reconciles exactly with the Routing page's "saved by
  // routing". Same methodology in Live and Simulated — only the rows differ.
  const cost = stats?.cost ?? null
  const saved = stats?.saved_routing ?? null
  const wouldBe = cost != null && saved != null ? cost + saved : null
  const reduction = wouldBe != null && wouldBe > 0 && saved != null ? saved / wouldBe : null
  const serviced = stats?.lanes.serviced ?? 0
  const decided = stats?.decided ?? 0
  const queries = market?.queries ?? []
  // In Live mode an empty board isn't "loading" — it means no real traffic has
  // hit the gate yet. Point the presenter at the action that fills it.
  const liveEmpty = mode === 'live' && queries.length === 0

  return (
    <>
      <BursarPageHeader
        subtitle={
          mode === 'live'
            ? 'Your real Hermes traffic — every live LLM call priced, ranked, routed, and settled by Bursar.'
            : 'The live compute market — every query priced, ranked by value-per-token, routed, and settled before it runs.'
        }
        title="Trading Floor"
      />

      {/* The firehose/before-after controls only make sense for the synthetic
          demo; Live traffic is driven by real chats, gated by the shield. Pinned
          ABOVE the scroll region so it stays put while the dashboard scrolls. */}
      {mode === 'demo' && (
        <div className="shrink-0">
          <ControlBar />
        </div>
      )}

      {/* The scroll region — everything below the controls. The charts fill the
          region (dashboard feel) in both modes; only the order book scrolls
          inside its panel. */}
      <div className="mt-4 flex min-h-0 flex-1 flex-col gap-4 overflow-y-auto pb-6">
        <div className="grid shrink-0 grid-cols-2 gap-3 lg:grid-cols-4">
          <StatCard hint="real model spend, settled via Stripe" label="Spend" value={money(cost)} />
          <StatCard accent="loss" hint="no Bursar: every call on the frontier model" label="Would have spent" value={money(wouldBe)} />
          <StatCard accent="gain" hint="value-routing vs frontier-default" label="Saved" value={money(saved)} />
          <StatCard accent="gain" hint={`${serviced.toLocaleString()} of ${decided.toLocaleString()} serviced`} label="Cost reduction" value={pct(reduction)} />
        </div>

        <div className="grid min-h-0 flex-1 grid-cols-1 gap-4 lg:grid-cols-[minmax(0,1fr)_minmax(0,1.5fr)]">
          <Panel className="min-h-0" subtitle="this market clear" title="Lane distribution">
            <div className="min-h-0 flex-1 p-3">
              <LaneBar counts={stats?.lanes ?? {}} palette={palette} />
            </div>
          </Panel>

          <Panel className="min-h-0" subtitle="newest first · ranked by value-per-token" title="Order book">
            {liveEmpty ? (
              <div className="flex flex-1 flex-col items-center justify-center gap-1.5 p-8 text-center">
                <p className="text-sm font-medium text-foreground">No live traffic yet</p>
                <p className="max-w-xs text-xs text-(--ui-text-tertiary)">
                  Arm the gate and send a message in chat — your real query gets priced, gated, and lands here as it
                  runs.
                </p>
              </div>
            ) : (
              <OrderFeed queries={queries} />
            )}
          </Panel>
        </div>
      </div>
    </>
  )
}
