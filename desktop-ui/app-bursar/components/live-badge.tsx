import { cn } from '@/lib/utils'

import { type BursarLiveStatus, useBursarLiveStatus } from '../use-bursar-live'

const META: Record<BursarLiveStatus, { dot: string; label: string; pulse: boolean; title: string }> = {
  connecting: { dot: 'bg-(--bursar-queued)', label: 'Connecting', pulse: true, title: 'Connecting to the engine event stream' },
  live: { dot: 'bg-(--bursar-gain)', label: 'Live', pulse: true, title: 'Streaming engine events live' },
  offline: {
    dot: 'bg-(--ui-text-tertiary)',
    label: 'Polling',
    pulse: false,
    title: 'Live event stream unavailable — refreshing on a timer'
  },
  reconnecting: { dot: 'bg-(--bursar-queued)', label: 'Reconnecting', pulse: true, title: 'Reconnecting to the engine event stream' }
}

// A small status pill showing whether the trading floor is driven by the live
// WebSocket feed (`Live`) or the polling backstop (`Polling`).
export function LiveBadge({ className }: { className?: string }) {
  const { status } = useBursarLiveStatus()
  const meta = META[status]

  return (
    <span
      className={cn(
        'inline-flex shrink-0 items-center gap-1.5 rounded-full border border-(--ui-stroke-tertiary) bg-(--ui-bg-card) px-2.5 py-1 text-xs font-medium text-(--ui-text-secondary)',
        className
      )}
      title={meta.title}
    >
      <span className="relative flex size-1.5">
        {meta.pulse && (
          <span className={cn('absolute inline-flex size-full animate-ping rounded-full opacity-75', meta.dot)} />
        )}
        <span className={cn('relative inline-flex size-1.5 rounded-full', meta.dot)} />
      </span>
      {meta.label}
    </span>
  )
}
