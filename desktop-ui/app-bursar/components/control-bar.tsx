import { Button } from '@/components/ui/button'
import { Switch } from '@/components/ui/switch'
import { cn } from '@/lib/utils'

import { useBursarControls, useBursarControlStatus } from '../use-bursar'

/**
 * The live-demo control surface. Drives the engine's B2 control endpoints so a
 * presenter can run the floor without a terminal: stream synthetic traffic,
 * force a settlement tick, flip the before/after toggle, inject a rogue-agent
 * flood, and reset the board.
 */
export function ControlBar() {
  const { data: status } = useBursarControlStatus()
  const { firehose, reset, rogue, setMode, tick } = useBursarControls()

  const running = status?.running ?? false
  const bursarOn = (status?.mode ?? 'bursar_on') === 'bursar_on'
  const busy = firehose.isPending || tick.isPending || reset.isPending || setMode.isPending || rogue.isPending

  return (
    <div className="flex flex-wrap items-center gap-2">
      <Button
        disabled={firehose.isPending}
        onClick={() => firehose.mutate({ action: running ? 'stop' : 'start', burst: true, dup_rate: 0.31, rate: 6, tick: 1 })}
        size="sm"
        variant={running ? 'secondary' : 'default'}
      >
        {running ? 'Stop firehose' : 'Start firehose'}
      </Button>

      <Button disabled={busy} onClick={() => tick.mutate()} size="sm" variant="outline">
        Tick
      </Button>

      <Button disabled={busy} onClick={() => rogue.mutate({ count: 40, team: 'marketing', tick: true })} size="sm" variant="outline">
        Rogue flood
      </Button>

      <Button disabled={busy} onClick={() => reset.mutate()} size="sm" variant="ghost">
        Reset
      </Button>

      <div className="mx-1 h-5 w-px bg-(--ui-stroke-tertiary)" />

      <label className="flex select-none items-center gap-2 text-xs font-medium">
        <Switch checked={bursarOn} disabled={setMode.isPending} onCheckedChange={on => setMode.mutate(on ? 'bursar_on' : 'bursar_off')} />
        <span className={cn('tabular-nums', bursarOn ? 'text-(--bursar-accent)' : 'text-(--bursar-loss)')}>
          {bursarOn ? 'Bursar ON' : 'Bursar OFF'}
        </span>
      </label>

      {running && (
        <span className="flex items-center gap-1.5 text-xs text-(--ui-text-tertiary)">
          <span className="size-1.5 animate-pulse rounded-full bg-(--bursar-accent)" />
          live
        </span>
      )}
    </div>
  )
}
