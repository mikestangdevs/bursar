import { useStore } from '@nanostores/react'
import { atom } from 'nanostores'

/**
 * Which traffic the whole Bursar dashboard is showing: the synthetic firehose
 * ('demo') or the user's real, gated LLM calls ('live'). It's a nanostore (not
 * React context) on purpose — the titlebar shield lives in a different tree
 * from BursarView, and arming it flips this store so "arm the gate" lands the
 * presenter in the Live dashboard. The string values double as the `source`
 * filter the read endpoints take, so a hook can pass the mode straight through.
 */
export type BursarMode = 'demo' | 'live'

export const $bursarMode = atom<BursarMode>('demo')

export function useBursarMode(): BursarMode {
  return useStore($bursarMode)
}

export function setBursarMode(mode: BursarMode): void {
  $bursarMode.set(mode)
}
