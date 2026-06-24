import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { setBursarMode } from './bursar-mode'

/**
 * Live-enforcement arm/disarm — the master switch for the Bursar gate that
 * prices / dedups / down-routes the user's *real* LLM calls (distinct from the
 * synthetic-firehose before/after toggle in the control bar).
 *
 * The gate runs in the agent's PTY child, a different process from this UI, so
 * the flag rides the shared control table via `/api/plugins/bursar/control/
 * enforce`. The gate reads it live per call, so flipping this is an instant
 * kill switch with no restart.
 */

const BASE = '/api/plugins/bursar'

async function bursarGet<T>(path: string): Promise<T> {
  const desktop = window.hermesDesktop
  if (!desktop?.api) {
    throw new Error('Hermes Desktop bridge unavailable')
  }

  return desktop.api<T>({ path: `${BASE}${path}` })
}

async function bursarPost<T>(path: string, body: Record<string, unknown> = {}): Promise<T> {
  const desktop = window.hermesDesktop
  if (!desktop?.api) {
    throw new Error('Hermes Desktop bridge unavailable')
  }

  return desktop.api<T>({ path: `${BASE}${path}`, method: 'POST', body })
}

export interface EnforceStatus {
  // Effective state the gate will use (env override applied).
  armed: boolean
  // The persisted DB flag (what the toggle controls).
  db_armed: boolean
  // True when a BURSAR_ENFORCE env var overrides the UI flag.
  env_locked: boolean
}

const ENFORCE_KEY = ['bursar', 'control-enforce'] as const

export function useBursarEnforce() {
  const qc = useQueryClient()

  const status = useQuery({
    queryFn: () => bursarGet<EnforceStatus>('/control/enforce'),
    queryKey: ENFORCE_KEY,
    // Poll modestly so the titlebar reflects a flag flipped elsewhere (env,
    // another window) without leaning on the live WS, which the shell tree
    // doesn't subscribe to.
    refetchInterval: 3000,
    // The titlebar is always mounted; don't thrash on every window focus.
    refetchOnWindowFocus: false
  })

  const mutation = useMutation({
    mutationFn: (armed: boolean) => bursarPost<EnforceStatus & { ok: boolean }>('/control/enforce', { armed }),
    onSuccess: (data: EnforceStatus & { ok: boolean }) => {
      // Write through immediately so the icon flips without waiting for the poll.
      qc.setQueryData<EnforceStatus>(ENFORCE_KEY, {
        armed: data.armed,
        db_armed: data.db_armed,
        env_locked: data.env_locked
      })
    }
  })

  const armed = status.data?.armed ?? false
  const envLocked = status.data?.env_locked ?? false
  // The toggle target is the DB flag; the env lock only gates the effective
  // state, so we still let it persist (it applies once the env is removed).
  const dbArmed = status.data?.db_armed ?? false

  return {
    armed,
    available: status.isSuccess || status.isError,
    dbArmed,
    envLocked,
    isPending: mutation.isPending,
    // Reachable only when the engine + bridge answered at least once.
    reachable: status.isSuccess,
    // Arming the gate flips the dashboard to Live so "arm the shield" has a
    // destination — you land where your real traffic shows up. Disarming leaves
    // the view put (you may want to keep reading the live result).
    toggle: () => {
      const next = !dbArmed
      if (next) {
        setBursarMode('live')
      }
      mutation.mutate(next)
    }
  }
}
