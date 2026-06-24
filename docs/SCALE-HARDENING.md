# Bursar — Scale & Multi-Tenant Hardening

**Status: assessment done 2026-06-22. The two P1s (N1, N2) are DONE 2026-06-22c —
implemented + adversarially tested, all green (see "P1 implementation" below).
N3–N6 (P2/P3) also DONE 2026-06-22c (see "P2/P3 implementation"). Tracked as N1–N6 in [feature_list.json](feature_list.json).**

> ⚠️ Gateway-restart gotcha: the N1/N2 code is in the gate + engine. It takes
> effect live only after `pkill -f "hermes_cli.main"` + relaunch. The DB tenant
> column migrates automatically on the next `connect()` (additive ADD COLUMN,
> backfills existing rows to 'local'; idempotent).

This doc is the roadmap of risks the current base does NOT yet handle. It was written from a
code read (not speculation) after the temporal-router + fee-redesign work landed. The base is
**safe for the single-user demo and near-term**; everything below is about **multi-tenant /
high-concurrency scale**.

---

## Why deep re-asks & agent loops are safe TODAY

Two load-bearing guards, both confirmed in code:

1. **Turn-opening guard.** Dedup/augment/serve fires ONLY on the first LLM call of a turn:
   `turn_opening = first_call` keyed on `turn_id`
   ([plugins/bursar_gate/__init__.py:458](hermes-agent/plugins/bursar_gate/__init__.py:458);
   `_turn_question` / `_TURN_Q` at :82–108). An agent loop re-sends the same trailing prompt on
   every internal call, but all share one `turn_id`, so after the first call `first_call=False`
   → the gate stays out of the way. **It cannot fire mid-loop.** This is what stops it corrupting
   a workflow.
2. **Fail-open, per-branch.** Every enforcement branch is individually wrapped; any exception →
   `return next_call(request)`
   ([plugins/bursar_gate/__init__.py:625](hermes-agent/plugins/bursar_gate/__init__.py:625)). A
   bug literally cannot cut off a turn. Down-route only swaps trivial-tier models to a known-safe
   target, else shadows — it never blocks.

A deep re-ask therefore either **augments** (runs fresh, never stale) or **serves** a genuinely
cheap timeless repeat — neither derails the conversation.

---

## The risks NOT yet planned for (prioritized)

| ID | Risk | Severity | Where |
|----|------|----------|-------|
| **N1** ✅DONE | **Cross-tenant leakage.** `find_dup` runs with NO team filter — it matches across every session and team by design ("org memory", documented in its own docstring). Single-user = a feature. Multi-tenant at scale = team B's reworded question can be **served team A's cached answer verbatim** (substitute path), or have A's answer **injected as augment context** into B's fresh call. | 🔴 P1 security | `find_dup` [bursar_live.py:289](hermes-agent/optional-skills/finance/bursar/scripts/bursar_live.py:289) (already accepts an optional `team=`, just not passed by the live gate) |
| **N2** ✅DONE | **`_TURN_Q` is in-memory, capped at 256, evicts oldest-half.** Under real concurrency (many simultaneous sessions through one gateway), a long loop's `turn_id` could get evicted mid-flight → a later internal call is mis-seen as `first_call=True` → dedup fires **mid-loop** → derails it. This is exactly the "cut off an agent loop" failure; it just can't trigger at single-user scale. | 🔴 P1 loop-cutoff | [bursar_gate/__init__.py:82–108](hermes-agent/plugins/bursar_gate/__init__.py:82) (`_TURN_Q_MAX = 256`) |
| **N3** ✅DONE | **turn_id dependence.** If any `llm_execution` path omits `turn_id`, `turn_opening` falls back to the last-message heuristic `is_turn_opening_request`, which H2 proved is fragile to Hermes' injected pseudo-user messages (the "[System: your previous tool call timed out]" drift). | 🟠 P2 | [bursar_gate/__init__.py:458](hermes-agent/plugins/bursar_gate/__init__.py:458) |
| **N4** ✅DONE | **Stateful detection is a thin regex.** `_STATEFUL_INTENT` catches "my calendar/inbox/tasks", "do i have", "what's on my", "am i" — but NOT conversational deixis ("continue", "now do the same for X", "what did we decide", "as we discussed"). A context-dependent follow-up could in theory match a prior turn. Narrow (serve only fires for timeless, and such follow-ups rarely match semantically), but real. | 🟠 P2 | `_STATEFUL_INTENT` [bursar_live.py:971](hermes-agent/optional-skills/finance/bursar/scripts/bursar_live.py:971) |
| **N5** ✅DONE | **Augment context bloat.** The augment block injects the prior FULL answer + instructions before the user message. Deep in an already-long chat this adds tokens and could push a near-limit turn over. The win still holds (skips a multi-call loop); just needs a cap on injected prior length. | 🟡 P3 | `dedup_augment_block` (bursar_live.py) |
| **N6** ✅DONE | **Plaintext at rest + recall window.** Prompts/answers persist plaintext in `~/.hermes/bursar/bursar.db` (compliance: encryption at rest, retention/TTL, redaction). And `find_dup` only scans the 300 most-recent serviced rows (`DEDUP_WINDOW`) — older originals age out → just a MISSED save (not unsafe). | 🟡 P3 | already noted in `_meta.f_series_residual_risks` |

---

## P1 implementation (DONE 2026-06-22c)

Both shipped as small, surgical hardening of existing code — NOT new features. During
implementation TWO of the original sketches turned out to be subtly WRONG; the corrected
designs below are what actually shipped. (Kept here as a record so the reasoning survives.)

### N1 — tenant isolation of `find_dup` ✅

**Correction to the original sketch:** the sketch said "scope by `team` (find_dup already takes
`team=`)". That is WRONG — `team` is **per-session** (`live:<session>`), so scoping reuse by team
would have KILLED the flagship single-user "you asked this in another conversation" cross-session
reuse. The boundary had to be a NEW, **process-level** key distinct from team.

What shipped:
- New **`tenant`** column on `queries` (migration: additive `ADD COLUMN tenant TEXT NOT NULL
  DEFAULT 'local'`, built in `_migrate` after the column exists — NOT in `SCHEMA_SQL`, which would
  fail `executescript` on a pre-tenant DB). Legacy rows backfill to `'local'`.
- `bursar_live.tenant_id()` = `BURSAR_TENANT` env or `'local'`. PROCESS-level: one gateway per org
  sets it; unset → single shared `'local'` tenant, so single-user reuse is untouched.
- `_insert_decided_query` stamps `tenant=tenant_id()` on every live row.
- `find_dup(..., tenant=)` adds a `tenant = ?` WHERE clause; the gate passes its own tenant by
  default → a match can never come from another org.
- **SERVE requires same tenant** (`same_tenant = query_tenant(orig)==live_tenant`, fails CLOSED on
  an unreadable row). **Cross-tenant AUGMENT is opt-in** via `BURSAR_CROSS_TENANT_AUGMENT` (default
  off); even then it only AUGMENTS (fresh call, prior as context), never SERVES across tenants.
- Files: `bursar_db.py` (schema+migrate), `bursar_live.py` (`tenant_id`, `query_tenant`,
  `find_dup`, `_insert_decided_query`), `bursar_gate/__init__.py` (`_truthy`, tenant scoping +
  `same_tenant` gate + `cross_tenant` observability flag).
- Tested: cross-tenant SERVE blocked; same-tenant cross-session reuse preserved; cross match only
  with the flag and provably cross; default `'local'` reuse works; migration backfills + idempotent.

### N2 — `_TURN_Q` eviction by LAST-ACCESS age, not count-256 ✅

**Correction to the original sketch:** the sketch said "TTL on **first-seen** time". That is WRONG —
a turn that legitimately runs longer than the TTL would evict ITSELF mid-flight, re-introducing the
exact bug. The correct policy is TTL on **last-access** (time-based LRU): every call of a turn
touches its entry, so an ACTIVE turn never ages out no matter how long it runs; only a turn SILENT
for the whole TTL is reclaimed.

What shipped (`bursar_gate/__init__.py`):
- `_TURN_Q` value is now `(question, last_access_monotonic)`.
- Per call: pop expired entries from the front (entries kept in ascending last-access order via
  move-to-end on touch, so the sweep is O(actually-expired), cheap), then first/touch.
- `_TURN_Q_TTL` (env `BURSAR_TURN_TTL_S`, default **3600s**) is the max plausible GAP between two
  calls of one turn — NOT the turn's total duration. `_TURN_Q_MAX` (env `BURSAR_TURN_Q_MAX`,
  default **20000**) is a memory backstop only, not the eviction mechanism (was the bug at 256).
- Tested: the discriminating case is a **silent** loop (one call, then a slow-tool wait while 1500
  other turns arrive) — survives under the new policy; a simulation confirmed the OLD cap-256 policy
  evicts it (the bug). Plus: active loop survives a flood, pinned question survives drift, idle turn
  past TTL is reclaimed, fresh turn within TTL is not, backstop still caps a pure untouched flood.

---

## P2/P3 implementation (N3–N6, DONE 2026-06-22c)

All four shipped + adversarially tested (27+ checks green). Code-done, pending in-app run.

### N3 — turn_id-absent fallback hardened ✅
The primary opening signal is `first_call` keyed on `turn_id` (drift-immune). The FALLBACK
(`is_turn_opening_request`, used only when no turn_id is threaded) keyed on "last message is a user
message", which Hermes' injected pseudo-user messages (`[System: tool timed out]`) could spoof into
a false opening. Added `_looks_injected_pseudo_user` (`_INJECTED_PSEUDO_USER` regex: leading
`[`/`(` + `system|tool error|tool result|error|note|reminder`) so the fallback rejects turn
machinery; the gate also warns ONCE (`_warn_no_turn_id_once`) if a call ever arrives without a
turn_id. Tested: injected `[System:`/`[Tool error:`/`(system)` and list-content forms are NOT
openings; a real prompt (and a non-keyword `[urgent]` bracket) still IS.

### N4 — stateful detection extended to conversational deixis ✅
`_STATEFUL_INTENT` only caught private-state phrases ("my calendar", "do i have"). Added
`_STATEFUL_DEIXIS` (continue / keep going / do the same for X / what did we decide / as we
discussed / the previous answer / from before / finish that …) and `is_stateful_intent()` (used by
`_temporal_heuristic`), plus extended the aux-LLM `_TEMPORAL_SYSTEM` prompt to count continuations
as stateful. Effect: a continuation that semantically matches a prior turn is classified `stateful`
→ run FRESH, never served a context-blind cached answer. Tested incl. word-boundary non-matches
("going on" ≠ "go on", "continued" ≠ "continue"). Accepted false positive: "is X the same for
both?" → stateful — safe direction (costs only a missed reuse, never a stale serve).

### N5 — injected prior-answer capped ✅
`dedup_augment_block` injected the prior FULL answer; deep in a long chat that could push a
near-limit turn over. `_cap_prior` head+tail-truncates to `BURSAR_MAX_PRIOR_CHARS` with a
"[… N chars omitted …]" marker; 60/40 head/tail favors the opening framing. **Default raised
4000 → 16000 (~4K tokens) after review:** 4000 (~1k tokens) was too aggressive — it would
routinely trim NORMAL answers (a detailed comparison is 6–12K chars; the live rate-limiter answer
was already 3683) to guard a context overflow that essentially never happens at 100K–200K+ windows
(<2%), and the augment already replaces a multi-call loop costing far more than the injected prior.
So the cap is a SAFETY VALVE for a pathological prior, not a routine trimmer. The win (skipping the
rediscovery loop) holds — the prior is context, not the verbatim answer. Observability: the augment
record carries `prior_chars`/`injected_chars`/`prior_capped` (via `prior_cap_stats`). Tested: long
prior bounded with head+tail+marker; short prior untouched; empty → "(none captured)"; cap≤0
disables; the block injects the capped form; stats helper matches.

### N6 — retention (the provable part) + documented limits ✅
- **Implemented:** `purge_expired(conn, days, source='live')` deletes live rows older than the
  window plus their ledger + events (dependents first, one IMMEDIATE txn); `maybe_purge_expired`
  is an opt-in, throttled (`BURSAR_RETENTION_SWEEP_S`, default 1h) sweep wired into `connect()`.
  Default **OFF** (`BURSAR_RETENTION_DAYS` unset/0 → no-op, zero overhead) so the demo never
  deletes. Tested: deletes expired live + cascades, keeps fresh, leaves demo alone (source-scoped),
  idempotent, no-op when disabled.
- **Recall window:** `DEDUP_WINDOW` (300, `BURSAR_DEDUP_WINDOW`) is now applied AFTER the N1 tenant
  filter, so it is effectively the 300 most-recent rows *per tenant* — older originals aging out is
  a MISSED save, never unsafe.
- **NOT implemented, by decision:** (1) **Encryption at rest** is a deployment concern (SQLCipher /
  an encrypted volume), not a crypto layer baked into the engine — documented here, not faked in
  code. (2) **Redacting the cached prompt/answer is deliberately rejected** — the answer text IS the
  cache, so redacting it would corrupt dedup-serve; redaction belongs at the ingest/logging layer.

---

## In-app verification (2026-06-22c, live gateway)

Run live through the Hermes app after restart (DB auto-migrated on connect). All N1–N6 now `passing`.

- **N1 — PROVEN live.** Tenant column migrated non-destructively (all rows → `local`); cross-session
  reuse preserved (reworded re-ask deduped a prior in a *different* session, both `local`, sim 0.915,
  saved $0.0109). Tenant isolation itself (cross-tenant block) is test-proven — can't be exercised
  single-user (one app session = one `local` tenant).
- **N4 — PROVEN live (twice).** A deixis re-ask ("as we discussed, remind me of…") MATCHED a prior
  cross-session at sim 0.93 (`would_dedup_augment=true`) but was classified **stateful** and ran
  FRESH (0 deduped rows, shadow "stateful — ran fresh"). Clean A/B vs the no-deixis re-ask that
  SERVED at sim 0.915. Second independent hit: a "Review the conversation above…" turn also → stateful.
- **N5 — augment path + observability PROVEN live.** A reworded re-ask took **comparison-augment**,
  reusing a **13-call / $0.064** sharding original with the prior injected; `prior_chars /
  injected_chars / prior_capped` recorded on the row. The cap *truncation* fires via the same
  unit-proven `_cap_prior` on this proven path (a separate `capped=true` live capture was not chased
  — deterministic fn, already unit-proven). Also confirmed the router live: `"…today"` → evolving →
  augment vs `"How should I…"` → timeless → serve.
- **N2 / N3 / N6 — test-proven, NOT single-user in-app triggerable** (concurrency-flood eviction /
  turn_id-absent path / retention which is off by default). Live runs incidentally confirmed
  turn-grouping holds (multi-call turns, no mid-loop dedup misfire) and nothing was deleted.
- **Incidental live confirmations:** proportional fee (est_cost $0.001694 → fee $0.000254 = 15%),
  Stripe settlement (`be_…` ids on serviced rows), temporal router routing correctly.

---

## Standing context for whoever picks this up

- The base is demo-ready; all N1–N6 are `passing` (feature_list 54 passing / 75 total). Temporal
  router + proportional fee in-app proven 2026-06-22; N1/N4 in-app proven, N5 augment in-app 06-22c.
- **Tests (durable):** `hermes-agent/optional-skills/finance/bursar/tests/` — `test_n1_n2`,
  `test_migration`, `test_n3_n6`, `test_gate_integration` (all green), `monitor_inapp.py` + README.
- **Gateway-restart gotcha:** any gate-code edit needs `pkill -f "hermes_cli.main"` + relaunch
  (Cmd-R keeps stale gate code).
- **Fail-open is sacred** — never let any of these fixes introduce a path that can break a chat turn.
- See memory `bursar-scale-hardening`, and PROGRESS.md "RESUME HERE" checkpoint.
