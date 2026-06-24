# Bursar scale-hardening tests (N1–N6)

Adversarial + integration tests for the N-series scale/multi-tenant hardening
(see `/SCALE-HARDENING.md`). All are standalone (`python3 <file>`); each spins up
its own temp DB via `BURSAR_DB`, so they never touch the live `~/.hermes/bursar/bursar.db`.

| File | Proves |
|------|--------|
| `test_n1_n2.py` | **N1** tenant filter on `find_dup` (cross-tenant blocked, same-tenant reuse preserved, cross-augment opt-in); **N2** `_TURN_Q` last-access TTL — the discriminating case is a *silent* loop surviving a 1500-turn flood that the old count-256 policy (simulated inline) evicts. |
| `test_migration.py` | The `tenant` column migrates onto an existing pre-tenant DB non-destructively (backfills `local`), idempotently — the real user-DB upgrade path. |
| `test_n3_n6.py` | **N3** injected `[System:`/`[Tool error:` pseudo-user messages aren't seen as turn openings; **N4** conversational deixis → stateful (+ boundary non-matches); **N5** `_cap_prior` head+tail truncation; **N6** opt-in retention purge (deletes expired live + cascades, keeps fresh/demo, idempotent, off by default). |
| `test_gate_integration.py` | **N1 end-to-end through the armed `_gate`**: cross-tenant never serves/injects (default), opt-in flag only augments (never serves), fail-open holds. |
| `test_h6_h8_h9.py` | **H6/H8/H9 freshness/work-cache**, driven through the REAL gate (`_tool_gate`/`_freshness_for_turn`/`_find_relocated`), real `work_cache` schema, and real files on disk (28 checks). **H6** capture beyond `read_file` — `terminal` cat/head/tail/pipe, `search_files`, MCP `read_resource`; cat→mutate→CHANGED. **H8** stat (mtime+size) short-circuit proven by a `_hash_file` call-counter (0 hashes when unchanged), same-size edit still caught via hash fallback, pre-H8 NULL rows back-compat. **H9** relocate-recognized (UNCHANGED+MOVED, verified), pure-rename→GONE/re-discover (never stale, never "re-read dead path"), changed-bytes candidate rejected. |
| `monitor_inapp.py` | Read-only monitor of the LIVE DB for in-app verification — prints recent live rows' tenant/temporal_class/reuse_mode, dedup events, and N5 cap stats. `python3 monitor_inapp.py`. |

To force the N5 cap to fire in a live app run, launch the gateway with a low
`BURSAR_MAX_PRIOR_CHARS` (e.g. 600) and re-ask a question with a freshness word
(so it classifies `evolving` → comparison-augment, which injects the prior).
Production default is 24000 (≈6K tokens; ~2× the observed live max answer of 11,168 chars).
