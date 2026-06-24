import { cn } from '@/lib/utils'

import { type LaneKey, type MarketQuery } from '../use-bursar'

import { money } from './primitives'

const STATUS: Record<LaneKey, { className: string; label: string }> = {
  deduped: { className: 'text-(--ui-cyan)', label: 'Deduped' },
  pending: { className: 'text-(--ui-text-tertiary)', label: 'Pending' },
  queued: { className: 'text-(--bursar-queued)', label: 'Queued' },
  rejected: { className: 'text-(--bursar-loss)', label: 'Rejected' },
  serviced: { className: 'text-(--bursar-gain)', label: 'Serviced' }
}

function StatusCell({ status }: { status: LaneKey }) {
  const s = STATUS[status] ?? STATUS.pending
  return (
    <span className={cn('inline-flex items-center gap-1.5 font-medium', s.className)}>
      <span className="size-1.5 rounded-full bg-current" />
      {s.label}
    </span>
  )
}

// T4 — surface the temporal router's outcome on the row. A `reuse_mode` means
// this real call reused a prior answer (augment): 'comparison' is the premium
// historical-diff outcome (evolving / recent-live re-asks), 'plain' a straight
// timeless reuse. With no reuse_mode but a `temporal_class`, the turn was
// classified but ran fresh (a deliberate live/stateful decline) — show that
// quietly so the floor reads as intentional, not a missed dedup.
function ReuseBadge({ q }: { q: MarketQuery }) {
  if (q.reuse_mode === 'comparison') {
    return (
      <span
        className="inline-flex items-center gap-1 rounded-full bg-(--ui-cyan)/12 px-1.5 py-0.5 text-[10px] font-medium text-(--ui-cyan)"
        title={`Reused a prior ${q.temporal_class ?? ''} answer as a historical baseline and re-derived the current state — "what changed since" comparison.`}
      >
        ⟳ comparison
      </span>
    )
  }
  if (q.reuse_mode === 'plain') {
    return (
      <span
        className="inline-flex items-center gap-1 rounded-full bg-(--bursar-gain)/12 px-1.5 py-0.5 text-[10px] font-medium text-(--bursar-gain)"
        title="Reused a prior answer to skip the rediscovery work (timeless — no change expected)."
      >
        ⟳ reused
      </span>
    )
  }
  if (q.reuse_mode === 'serve') {
    return (
      <span
        className="inline-flex items-center gap-1 rounded-full bg-(--bursar-gain)/12 px-1.5 py-0.5 text-[10px] font-medium text-(--bursar-gain)"
        title={`Served the prior ${q.temporal_class ?? 'timeless'} answer straight from cache — no fresh call, $0 new spend (cheap original, nothing to re-derive).`}
      >
        ⟳ served free
      </span>
    )
  }
  if (q.temporal_class === 'live' || q.temporal_class === 'stateful') {
    return (
      <span
        className="text-[10px] font-medium text-(--ui-text-tertiary)"
        title={`Classified ${q.temporal_class} — ran fresh rather than reuse a prior answer (never serve stale).`}
      >
        {q.temporal_class} · fresh
      </span>
    )
  }
  return null
}

export function OrderFeed({ queries }: { queries: MarketQuery[] }) {
  if (!queries.length) {
    return (
      <div className="flex flex-1 items-center justify-center p-6 text-sm text-(--ui-text-tertiary)">
        No orders yet — start the firehose to fill the book.
      </div>
    )
  }

  return (
    <div className="min-h-0 flex-1 overflow-y-auto">
      <table className="w-full border-collapse text-xs">
        <thead className="sticky top-0 z-10 bg-(--ui-bg-elevated) text-(--ui-text-tertiary)">
          <tr className="[&>th]:px-3 [&>th]:py-2 [&>th]:text-left [&>th]:font-medium">
            <th className="w-24">Status</th>
            <th className="w-24">Team</th>
            <th>Query</th>
            <th className="w-16 text-right">Value</th>
            <th className="w-20 text-right">VPT</th>
            <th className="w-32">Model</th>
            <th className="w-20 text-right">Cost</th>
          </tr>
        </thead>
        <tbody className="tabular-nums">
          {queries.map(q => (
            <tr className="border-t border-(--ui-stroke-tertiary) [&>td]:px-3 [&>td]:py-1.5" key={q.id}>
              <td>
                <StatusCell status={q.status} />
              </td>
              <td className="truncate text-(--ui-text-secondary)">{q.team}</td>
              <td className="max-w-0 text-(--ui-text-secondary)">
                <div className="flex items-center gap-2">
                  <span className="min-w-0 flex-1 truncate" title={q.prompt}>
                    {q.prompt}
                  </span>
                  <ReuseBadge q={q} />
                </div>
              </td>
              <td className="text-right text-foreground">{q.value}</td>
              <td className="text-right text-(--ui-text-tertiary)">{q.vpt == null ? '—' : q.vpt.toFixed(2)}</td>
              <td className="truncate text-(--ui-text-tertiary)">{q.model ?? '—'}</td>
              <td className="text-right text-(--ui-text-secondary)">{money(q.est_cost)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
