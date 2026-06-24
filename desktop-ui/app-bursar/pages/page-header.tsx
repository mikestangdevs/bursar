import type { ReactNode } from 'react'

export function BursarPageHeader({ subtitle, title, trailing }: { subtitle: string; title: string; trailing?: ReactNode }) {
  return (
    <header className="flex shrink-0 items-start justify-between gap-4 pb-4 pt-1">
      <div className="min-w-0">
        <h1 className="text-lg font-semibold tracking-tight text-foreground">{title}</h1>
        <p className="mt-0.5 text-sm text-muted-foreground">{subtitle}</p>
      </div>
      {trailing}
    </header>
  )
}

// Interim panel shown on pages whose live data layer is still being wired.
export function BursarComingSoon({ note }: { note: string }) {
  return (
    <div className="flex flex-1 items-center justify-center pb-6">
      <div className="max-w-md rounded-xl border border-dashed border-(--ui-stroke-tertiary) bg-(--ui-bg-card) px-6 py-8 text-center text-sm text-muted-foreground">
        {note}
      </div>
    </div>
  )
}
