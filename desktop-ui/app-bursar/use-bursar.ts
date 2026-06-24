import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { type BursarMode, useBursarMode } from './bursar-mode'
import { useBursarLiveStatus } from './use-bursar-live'

/**
 * Bursar data layer. The desktop app already spawns the Hermes dashboard
 * web_server as its local backend, and that server mounts the Bursar plugin
 * routes at `/api/plugins/bursar/*`. So we read the live engine through the
 * same authed IPC bridge the rest of the app uses (`window.hermesDesktop.api`)
 * — no extra server, no gateway changes. React Query polls for the live feel;
 * the queries unmount (and stop polling) when the lens closes.
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

// ---- Response shapes (mirror plugins/bursar/dashboard/plugin_api.py) --------

export type LaneKey = 'serviced' | 'deduped' | 'queued' | 'rejected' | 'pending'

export interface BursarStats {
  lanes: Partial<Record<LaneKey, number>>
  decided: number
  pending: number
  // Model-agnostic headline (real $): cost = real model spend; saved = dollars
  // actually avoided (dedup + gating + reuse), each at its own model's price;
  // reduction = saved / (cost + saved); fee = Bursar revenue (reported apart).
  cost: number
  saved: number
  reduction: number | null
  fee: number
  // Savings breakdown (drill-down).
  saved_dedup: number
  saved_gate: number
  saved_reuse?: number
  saved_routing: number
  // Secondary "modeled ceiling" — every call at the single priciest model.
  // Drill-down only, NOT the headline.
  naive_all_premium: number
  premium_model: string | null
  // Backward-compat: bill == spend + fee (what /summary reconciles against).
  bill: number
}

export interface MarketQuery {
  id: number
  team: string
  source: BursarMode
  prompt: string
  value: number
  tier: string
  vpt: number | null
  est_tokens: number | null
  est_cost: number | null
  fee: number | null
  model: string | null
  status: LaneKey
  dedup_of: number | null
  rationale: string | null
  // Temporal router (T-series): the per-query class, and — when this row reused
  // a prior answer — the reuse mode. `reuse_mode='comparison'` is the premium
  // "historical diff" outcome; 'plain' a straight augment reuse; 'serve' a pure
  // cache-serve (no fresh call, $0 new spend — only for a cheap timeless original).
  // All null on ordinary first-asks and demo rows.
  temporal_class: 'timeless' | 'evolving' | 'live' | 'stateful' | null
  reuse_mode: 'comparison' | 'plain' | 'serve' | null
  created_at: string | null
  decided_at: string | null
}

export interface MarketResponse {
  queries: MarketQuery[]
  counts: Partial<Record<LaneKey, number>>
  count: number
}

export interface BudgetRow {
  team: string
  period: string
  cap: number
  spent: number
  remaining: number
  pct_used: number | null
  reset_at: string | null
  updated_at: string | null
}

export interface LedgerRow {
  id: number
  query_id: number | null
  team: string
  model: string | null
  tokens: number | null
  token_cost: number | null
  fee: number | null
  total: number | null
  settled: number | null
  meter_event_id: string | null
  created_at: string | null
}

export interface LedgerResponse {
  ledger: LedgerRow[]
  summary: {
    rows: number
    total: number
    token_cost: number
    fee: number
    settled: number
    local_only: number
  }
}

export interface RoutingRow {
  model: string
  queries: number
  tokens: number
  token_cost: number
  fee: number
  total: number
  price_per_mtok: number | null
  quality: number | null
}

export interface RoutingResponse {
  routing: RoutingRow[]
  models: RoutingRow[]
  count: number
}

export interface CatalogModel {
  provider: string
  model: string
  input: number | null
  output: number | null
  blended: number | null
  source: string | null
  in_use: boolean
}

export interface CatalogResponse {
  catalog: CatalogModel[]
  count: number
  spread: number | null
  // True when prices came from Hermes's sourced pricing snapshot (vs the
  // engine-catalog fallback).
  sourced: boolean
}

export interface SnapshotRow {
  id?: number
  ts?: string
  serviced?: number
  deduped?: number
  queued?: number
  rejected?: number
  starved?: number
  bill?: number
  saved?: number
  [key: string]: unknown
}

export interface SnapshotsResponse {
  snapshots: SnapshotRow[]
}

export type ControlMode = 'bursar_on' | 'bursar_off'

export interface ControlStatus {
  running: boolean
  mode: ControlMode
  cfg: { rate: number; dup_rate: number; burst: boolean; tick: number }
}

export interface TickResult {
  serviced: number
  deduped: number
  rejected: number
  queued: number
  bill: number
  saved_dedup: number
  saved_routing: number
  [key: string]: unknown
}

// ---- Query keys -------------------------------------------------------------

// `source` (the Simulated/Live mode) is part of every scoped key so the two
// dashboards keep independent caches — switching modes never shows the other
// mode's stale numbers for a frame.
const KEYS = {
  budgets: ['bursar', 'budgets'] as const,
  catalog: (source: BursarMode) => ['bursar', 'catalog', source] as const,
  controlStatus: ['bursar', 'control-status'] as const,
  ledger: (source: BursarMode, team?: string) => ['bursar', 'ledger', source, team ?? 'all'] as const,
  market: (source: BursarMode, status?: string, limit?: number) =>
    ['bursar', 'market', source, status ?? 'all', limit ?? 200] as const,
  routing: (source: BursarMode) => ['bursar', 'routing', source] as const,
  snapshots: ['bursar', 'snapshots'] as const,
  stats: (source: BursarMode) => ['bursar', 'stats', source] as const
}

// Invalidate every Bursar read after a control action mutates the board.
function invalidateAll(qc: ReturnType<typeof useQueryClient>) {
  void qc.invalidateQueries({ queryKey: ['bursar'] })
}

// ---- Read hooks (polled, live-aware) ----------------------------------------

// When the live WebSocket feed is connected, event arrivals drive cache
// refreshes, so polling drops to a slow backstop. Without the socket (e.g. a
// gated/remote bind), we fall back to the original fast poll. `useQuery` reads
// `refetchInterval` each render, so flipping live↔offline retunes it live.
function useLiveInterval(fast: number, backstop = 12_000): number {
  return useBursarLiveStatus().status === 'live' ? backstop : fast
}

export function useBursarStats() {
  const source = useBursarMode()
  return useQuery({
    queryFn: () => bursarGet<BursarStats>(`/stats?source=${source}`),
    queryKey: KEYS.stats(source),
    refetchInterval: useLiveInterval(1500)
  })
}

export function useBursarMarket(status?: string, limit = 200) {
  const source = useBursarMode()
  return useQuery({
    queryFn: () => bursarGet<MarketResponse>(`/market?source=${source}&limit=${limit}${status ? `&status=${status}` : ''}`),
    queryKey: KEYS.market(source, status, limit),
    refetchInterval: useLiveInterval(1500)
  })
}

export function useBursarBudgets() {
  return useQuery({
    queryFn: () => bursarGet<{ budgets: BudgetRow[]; count: number }>('/budgets'),
    queryKey: KEYS.budgets,
    refetchInterval: useLiveInterval(2500)
  })
}

export function useBursarLedger(team?: string, limit = 500) {
  const source = useBursarMode()
  return useQuery({
    queryFn: () => bursarGet<LedgerResponse>(`/ledger?source=${source}&limit=${limit}${team ? `&team=${team}` : ''}`),
    queryKey: KEYS.ledger(source, team),
    refetchInterval: useLiveInterval(2500)
  })
}

export function useBursarRouting() {
  const source = useBursarMode()
  return useQuery({
    queryFn: () => bursarGet<RoutingResponse>(`/routing?source=${source}`),
    queryKey: KEYS.routing(source),
    refetchInterval: useLiveInterval(2500)
  })
}

// The price catalog is static config, so fetch it once and don't poll.
export function useBursarCatalog() {
  const source = useBursarMode()
  return useQuery({
    queryFn: () => bursarGet<CatalogResponse>(`/catalog?source=${source}`),
    queryKey: KEYS.catalog(source),
    staleTime: Infinity
  })
}

export function useBursarSnapshots(limit = 500) {
  return useQuery({
    queryFn: () => bursarGet<SnapshotsResponse>(`/snapshots?limit=${limit}`),
    queryKey: KEYS.snapshots,
    refetchInterval: useLiveInterval(2500)
  })
}

export function useBursarControlStatus() {
  return useQuery({
    queryFn: () => bursarGet<ControlStatus>('/control/status'),
    queryKey: KEYS.controlStatus,
    refetchInterval: useLiveInterval(1500)
  })
}

// ---- Control mutations ------------------------------------------------------

export function useBursarControls() {
  const qc = useQueryClient()
  const onDone = () => invalidateAll(qc)

  const firehose = useMutation({
    mutationFn: (body: { action: 'start' | 'stop'; rate?: number; dup_rate?: number; burst?: boolean; tick?: number }) =>
      bursarPost<{ ok: boolean; running: boolean }>('/control/firehose', body),
    onSuccess: onDone
  })
  const tick = useMutation({
    mutationFn: () => bursarPost<{ ok: boolean; mode: ControlMode; tick: TickResult }>('/control/tick'),
    onSuccess: onDone
  })
  const reset = useMutation({ mutationFn: () => bursarPost<{ ok: boolean }>('/control/reset'), onSuccess: onDone })
  const setMode = useMutation({
    mutationFn: (mode: ControlMode) => bursarPost<{ ok: boolean; mode: ControlMode }>('/control/mode', { mode }),
    onSuccess: onDone
  })
  const rogue = useMutation({
    mutationFn: (body?: { count?: number; team?: string; tick?: boolean }) =>
      bursarPost<{ ok: boolean; injected: number }>('/control/rogue', body ?? {}),
    onSuccess: onDone
  })

  return { firehose, reset, rogue, setMode, tick }
}
