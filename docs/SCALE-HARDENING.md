# Bursar — Scale & Multi-Tenant Hardening

How Bursar behaves under multi-tenant / high-concurrency load, and the guards
that keep it from ever derailing a chat turn. Written from a read of the gate +
engine code.

---

## Why deep re-asks & agent loops are safe

Two load-bearing guards:

1. **Turn-opening guard.** Dedup/augment/serve fires ONLY on the first LLM call
   of a turn (`turn_opening = first_call`, keyed on `turn_id`, in
   [`plugin/bursar_gate/__init__.py`](../plugin/bursar_gate/__init__.py)). An
   agent loop re-sends the same trailing prompt on every internal call, but all
   share one `turn_id`, so after the first call the gate stays out of the way —
   **it cannot fire mid-loop**, so it can never corrupt a workflow.
2. **Fail-open, per-branch.** Every enforcement branch is individually wrapped;
   any exception returns `next_call(request)`. A bug literally cannot cut off a
   turn. Down-route only swaps trivial-tier models to a known-safe target, else
   shadows — it never blocks.

A deep re-ask therefore either **augments** (runs fresh, never stale) or
**serves** a genuinely cheap timeless repeat — neither derails the conversation.

---

## Multi-tenant & concurrency hardening

| Risk | How Bursar handles it |
|---|---|
| **Cross-tenant leakage.** Dedup matches across sessions by design ("org memory"). At multi-tenant scale, team B's reworded question could be served team A's cached answer. | A process-level **`tenant`** key (distinct from per-session `team`), set via `BURSAR_TENANT` — one gateway per org. SERVE requires the same tenant (fails *closed* on an unreadable row); cross-tenant AUGMENT is opt-in (`BURSAR_CROSS_TENANT_AUGMENT`) and even then only injects the prior as context for a fresh call, never serves it. Single-user stays one shared `local` tenant, so cross-session reuse is untouched. |
| **Turn-tracking eviction under load.** The in-memory turn map could evict a long loop's `turn_id` mid-flight, making a later internal call look like a new turn → dedup fires mid-loop. | Eviction is **time-based on last access**, not a fixed count: every call of a turn touches its entry, so an active turn never ages out no matter how long it runs; only a turn silent for the whole TTL (`BURSAR_TURN_TTL_S`, default 1h) is reclaimed. A large count cap remains purely as a memory backstop. |
| **turn_id-absent fallback spoofing.** When no `turn_id` is threaded, the fallback keys on "the last message is a user message", which Hermes' injected pseudo-user messages (`[System: tool timed out]`) could spoof into a false opening. | The fallback rejects injected turn machinery (a leading-bracket `[system\|tool error\|…]` detector), and the gate warns once if any call ever arrives without a `turn_id`. |
| **Stateful / deixis follow-ups.** A context-dependent follow-up ("continue", "do the same for X", "what did we decide") could match a prior turn and be served a context-blind answer. | Stateful intent — private-state phrases plus conversational deixis — is classified `stateful` → always run **fresh**, never served a cached answer. Errs toward a missed reuse, never a stale serve. |
| **Augment context bloat.** Injecting a prior full answer deep in a long chat adds tokens. | The injected prior is head+tail capped (`BURSAR_MAX_PRIOR_CHARS`) — a safety valve for a pathological prior, not a routine trimmer. The win (skipping the rediscovery loop) holds because the prior is *context*, not the served answer. |
| **Data at rest & recall window.** Prompts/answers persist plaintext in local SQLite; dedup only scans recent rows. | Opt-in retention sweep (`purge_expired`, off by default) deletes live rows + dependents older than `BURSAR_RETENTION_DAYS`. Encryption at rest is treated as a deployment concern (SQLCipher / encrypted volume), not faked in code; the cache text is deliberately **not** redacted (it *is* the cache — redaction belongs at the ingest/logging layer). The recall window is per-tenant; an aged-out original is a missed save, never unsafe. |

---

## Principle

**Fail-open is sacred** — none of this hardening introduces a path that can break
a chat turn. Every guard either runs fresh or steps aside.
