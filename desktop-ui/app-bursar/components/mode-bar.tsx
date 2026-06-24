import { Button } from '@/components/ui/button'
import { SegmentedControl } from '@/components/ui/segmented-control'
import { cn } from '@/lib/utils'

import { type BursarMode, setBursarMode, useBursarMode } from '../bursar-mode'
import { useBursarEnforce } from '../use-bursar-enforce'

/**
 * The Simulated/Live switch that scopes the whole dashboard, plus a contextual
 * banner that answers "I flipped to Live — now what?". In Live it shows the
 * gate's arm state and an inline arm/disarm control, so the presenter never has
 * to hunt for the titlebar shield. The segmented value IS the `source` filter
 * (see bursar-mode), so switching here re-scopes every panel on every page.
 */

const MODE_OPTIONS = [
  { id: 'demo', label: 'Simulated' },
  { id: 'live', label: 'Live' }
] as const satisfies readonly { id: BursarMode; label: string }[]

export function BursarModeBar() {
  const mode = useBursarMode()

  return (
    <div className="flex flex-wrap items-center justify-between gap-x-4 gap-y-2 pb-3">
      <SegmentedControl<BursarMode> onChange={setBursarMode} options={MODE_OPTIONS} value={mode} />
      {mode === 'demo' ? <DemoNote /> : <LiveNote />}
    </div>
  )
}

function DemoNote() {
  return (
    <p className="flex items-center gap-2 text-xs text-(--ui-text-tertiary)">
      <span className="size-1.5 rounded-full bg-(--bursar-queued)" />
      Synthetic firehose — a safe sandbox. These numbers are simulated traffic.
    </p>
  )
}

function LiveNote() {
  const enforce = useBursarEnforce()

  if (!enforce.reachable) {
    return <p className="text-xs text-(--ui-text-tertiary)">Live engine not reachable yet…</p>
  }

  // Effective state, env override applied. envLocked means an operator pinned
  // it via BURSAR_ENFORCE, so the UI control is informational only.
  const { armed, envLocked, isPending } = enforce

  return (
    <div className="flex flex-wrap items-center gap-2.5">
      <span
        className={cn(
          'flex items-center gap-2 text-xs font-medium',
          armed ? 'text-emerald-500' : 'text-(--ui-text-secondary)'
        )}
      >
        <span
          className={cn('size-1.5 rounded-full', armed ? 'animate-pulse bg-emerald-500' : 'bg-(--ui-text-quaternary)')}
        />
        {armed ? 'LIVE — real LLM calls are priced & gated by Bursar' : 'Observe-only — the gate is not enforcing yet'}
      </span>

      {envLocked ? (
        <span className="rounded-md bg-(--ui-bg-tertiary) px-2 py-0.5 text-[0.6875rem] text-(--ui-text-tertiary)">
          locked by env
        </span>
      ) : armed ? (
        <Button disabled={isPending} onClick={() => enforce.toggle()} size="sm" variant="ghost">
          Disarm
        </Button>
      ) : (
        <Button disabled={isPending} onClick={() => enforce.toggle()} size="sm">
          Arm gate
        </Button>
      )}
    </div>
  )
}
