import { Bar, BarChart, Cell, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'

import { type LaneKey } from '../use-bursar'

import { type BursarPalette } from './primitives'

const LANES: ReadonlyArray<{ key: Exclude<LaneKey, 'pending'>; label: string }> = [
  { key: 'serviced', label: 'Serviced' },
  { key: 'deduped', label: 'Deduped' },
  { key: 'queued', label: 'Queued' },
  { key: 'rejected', label: 'Rejected' }
]

function laneColor(key: string, palette: BursarPalette): string {
  switch (key) {
    case 'serviced':
      return palette.serviced
    case 'deduped':
      return palette.deduped
    case 'queued':
      return palette.queued
    default:
      return palette.loss
  }
}

interface Datum {
  count: number
  key: string
  lane: string
}

function LaneTooltip({ active, payload }: { active?: boolean; payload?: Array<{ payload: Datum }> }) {
  if (!active || !payload?.length) {
    return null
  }
  const d = payload[0].payload
  return (
    <div className="rounded-lg border border-(--ui-stroke-secondary) bg-(--ui-bg-elevated) px-2.5 py-1.5 text-xs shadow-md">
      <div className="font-medium text-foreground">{d.lane}</div>
      <div className="tabular-nums text-(--ui-text-secondary)">{d.count.toLocaleString()} queries</div>
    </div>
  )
}

export function LaneBar({ counts, palette }: { counts: Partial<Record<LaneKey, number>>; palette: BursarPalette }) {
  const data: Datum[] = LANES.map(lane => ({ count: counts[lane.key] ?? 0, key: lane.key, lane: lane.label }))

  return (
    <ResponsiveContainer height="100%" width="100%">
      <BarChart data={data} margin={{ bottom: 0, left: -16, right: 8, top: 8 }}>
        <XAxis axisLine={false} dataKey="lane" tick={{ fill: palette.text, fontSize: 12 }} tickLine={false} />
        <YAxis allowDecimals={false} axisLine={false} tick={{ fill: palette.text, fontSize: 11 }} tickLine={false} width={40} />
        <Tooltip content={<LaneTooltip />} cursor={{ fill: 'rgba(120,120,120,0.08)' }} />
        <Bar dataKey="count" isAnimationActive={false} maxBarSize={72} radius={[6, 6, 0, 0]}>
          {data.map(datum => (
            <Cell fill={laneColor(datum.key, palette)} key={datum.key} />
          ))}
        </Bar>
      </BarChart>
    </ResponsiveContainer>
  )
}
