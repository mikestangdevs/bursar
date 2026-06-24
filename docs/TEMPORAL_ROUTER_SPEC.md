# Temporal-Class Router + Historical-Comparison Augment — design + execution spec

**Status: T1–T4 BUILT + verified offline. In-app A–D test pending (needs gateway restart +
Electron is already rebuilt).** Designed 2026-06-21; built same day.

## State as of 2026-06-21 (build session)
**Done (verified offline):**
- **T1 — classifier** `bursar_live.classify_temporal()` (aux-LLM primary, regex+volatile-noun
  heuristic fallback, per-prompt cache, fail-safe=`evolving`). Adversarial test 51/51
  (`scratchpad/test_temporal_classifier.py`). One real defect found+fixed (totality on non-str input).
- **T2 — router** in `bursar_gate/__init__.py` `_gate`: classifies ONLY when a reusable match
  exists; routes timeless→serve/augment-plain, **evolving+live→augment-comparison**, stateful→fresh;
  non-synth api_mode = shadow only. **Replaces the H5 binary veto.** Driven through the REAL
  `_gate` against a seeded temp DB: 33/33 (`scratchpad/test_temporal_router.py`).
  - **In-app fix 2026-06-22:** dropped the original `live + prior older than TTL → run plain fresh`
    branch (and `_LIVE_REUSE_TTL`). It was over-conservative — comparison always runs fresh with the
    prior as a dated baseline, so it's never stale at any age — and a single evolving→live
    misclassification (real run: B tagged `live` off "right now / in production", 220s-old prior)
    killed the feature. Live and evolving now behave identically; class is a badge label only.
- **T3 — comparison augment** `dedup_augment_block(compare=, prior_age=, temporal_class=)`:
  prior=BASELINE framing, re-derive current, required "what changed since <when>" section,
  humanized age. Verified inside the router test.
- **Embed backend: no change needed** — the live gate already opts into numpy for its own
  process via `os.environ.setdefault("BURSAR_EMBED_BACKEND","numpy")` at registration
  (`bursar_gate/__init__.py` ~line 914), so real traffic gets semantic recall (reworded twins
  match). The module default stays `hashing` for firehose/standalone. (I briefly flipped the
  module default and reverted it — the gate setdefault already covers the live path, and a global
  flip would have moved the synthetic demo onto numpy.)
- **Real-DB proof:** the actual prompt-A row's faithful reworded twin → class=evolving,
  find_dup MATCH sim=0.8406 (≥0.78), servable=True → will augment-comparison in-app. The exact
  scenario the old veto blocked.

- **T4 — floor surfacing DONE:** `queries` gains `temporal_class` + `reuse_mode` (SCHEMA_SQL +
  idempotent `_migrate`); threaded through `record_serviced`/`_insert_decided_query`; set in
  `bursar_gate._record`; exposed on `/market`; `dedup_augment` event enriched
  (temporal_class/mode/cache_age_s). Frontend `OrderFeed` shows a `ReuseBadge`
  (comparison / reused / "<class> fresh"). `test_t4_persist.py` 13/13; apps/desktop typecheck +
  full build (badge in shipped JS); dist-bundle repackaged.

**In flight / next:**
- **In-app A–D test** — REQUIRES `pkill -f hermes_cli.main` (reloads gate + dashboard; both run
  under hermes_cli.main — gate code loads from the dev tree, no installed copy) + relaunch the
  Electron app (already rebuilt, so it loads the new JS). Then re-fire the RAG twin in a NEW chat:
  confirm a `dedup_augment` event/row with `temporal_class='evolving'`, `reuse_mode='comparison'`,
  a "what changed" answer, and the ⟳ comparison badge on the order feed.

---

## Why (the discovery that motivates this)

In-app test 2026-06-21: prompt A (a deep RAG-research turn, 9 live calls) then prompt B
(a fully reworded near-twin). B did **not** dedup. Root-caused with certainty:

- The matcher is FINE: `find_dup` returns the match; B↔A cosine = **0.85 ≥ 0.78** (numpy
  MiniLM threshold). Enforcement was armed, numpy backend active.
- The blocker was the **H5 freshness guard** (`bursar_gate/__init__.py` ~line 487): A's
  prompt contains "as of 2026" and B's contains "right now" + "the latest benchmarks".
  `is_freshness_intent()` matched `now`/`latest`/`as of` → the gate **deliberately declined
  to dedup** and ran fresh, to avoid serving stale data.

The guard misfires: "the *latest* RAG *strategies* teams use *right now*" is NOT a request
for live data — it's evolving knowledge. The regex can't tell it from "latest BTC price".
Net effect: Bursar refuses to reuse a large fraction of legitimate research re-asks (the
single most valuable dedup target).

**Key safety insight that reshapes the whole design:** the danger of "stale" exists ONLY
for dedup-**SERVE** (return cached answer, no fresh call). **Augment ALWAYS runs a fresh
call** — it just injects the prior answer as context — so an augmented answer is NEVER
stale; the prior is a baseline, not the served result. Therefore the freshness guard should
gate SERVE only, never AUGMENT. And for time-sensitive re-asks, augment becomes a *feature*:
"here's what changed since you last asked."

User decision (verbatim intent): build the real feature — a **temporal-class router** +
**augment-with-comparison** mode. Turn the veto into compounding value: the user gets
historical insight / source comparison "magically" from past research.

---

## The taxonomy (the core model)

Freshness is NOT a uniform query property — it's a per-query temporal class. Four classes,
each with a reuse strategy:

| Class | Example | Strategy |
|---|---|---|
| **TIMELESS** (never changes) | "how does vector RAG work" | SERVE if cheap original / AUGMENT-plain if expensive (current H12 path). No freshness check. |
| **EVOLVING** (weeks–months) | "latest RAG strategies", "state of batteries 2026" | **AUGMENT-COMPARISON** — inject prior as historical baseline, re-derive fresh, surface the diff. ← the unlock |
| **LIVE** (sec–min) | "BTC price now", "is the server up", "score" | AUGMENT-COMPARISON if a recent prior exists ("up $200 since you asked"), else FRESH. NEVER serve-stale. |
| **STATEFUL/personal** | "my calendar", "my last commit" | FRESH (or very short TTL). Prior likely invalid. |

Decision = `temporal_class(query)` × `age(cached_answer)` × `original_cost (H12)`.
The current code collapses EVOLVING into LIVE and vetoes both. That's the entire bug.

---

## Design

### 1. Temporal classifier — `bursar_live.py` (new fn, e.g. `classify_temporal(prompt) -> str`)
- **Primary:** Hermes-native aux LLM. Reuse the exact pattern in `bursar_model.py:212`
  (`_aux_client()` → `agent.auxiliary_client.get_text_auxiliary_client("bursar_temporal")`,
  guarded by `_AuxUnavailable`). One cheap call, returns one of
  {timeless, evolving, live, stateful}. Cache per prompt (in-process + maybe a `queries`
  column) so it's not re-run on every loop call.
- **Fallback (aux unavailable):** the existing `_FRESHNESS_INTENT` regex as a SECONDARY
  signal, not a veto. Heuristic: a freshness word ADJACENT to a volatile-data noun
  (price/score/news/weather/status/stock) → LIVE; a freshness word alone → EVOLVING;
  nothing → TIMELESS.
- **Fail-safe direction:** uncertain → EVOLVING (augment-comparison), because augment runs
  fresh and is never stale. Only route to pure SERVE when confidently TIMELESS. NEVER let an
  uncertain classification produce a stale SERVE.

### 2. Router — replace the H5 branch in `bursar_gate/__init__.py` `_gate` (~lines 487–575)
Current: `if turn_opening and is_freshness_intent: decline (shadow only)`.
New: `if turn_opening and is_synthesizable_api_mode:` → call `classify_temporal(prompt)` →
- TIMELESS → existing H12 logic (SERVE cheap / AUGMENT-plain expensive). Unchanged.
- EVOLVING → AUGMENT-COMPARISON (always, regardless of freshness words).
- LIVE → if cached answer age < small TTL → AUGMENT-COMPARISON; else FRESH (record a shadow
  "ran fresh — live class" so the floor shows the deliberate decline).
- STATEFUL → FRESH (shadow).
Keep the whole block fail-open (any error → `next_call(request)`).

### 3. Comparison-augment mode — `bursar_live.py` `dedup_augment_block` (~line 480)
Add a `mode="comparison"` (or a `compare: bool`) variant. Instead of "reuse this, skip the
lookup", inject: "You answered a version of this before — here is that answer AND when. The
question may be time-sensitive: re-derive the CURRENT state, then explicitly call out what
changed since the prior answer (a 'since last time' / historical-comparison section). Do not
present the prior answer as current." Carry the prior answer's timestamp/age into the block.

### 4. Surface it (optional, dashboard) — `apps/desktop/src/app/bursar/`
Emit the temporal class + "compared to prior from <when>" on the `dedup_augment` event /
order feed so the Trading Floor can show "historical comparison" as a distinct, premium
outcome (not just $ saved). Ties to the model-agnostic savings work already shipped.

---

## Test plan (adversarial — do this, don't eyeball)

The official in-app battery is four seed→reworded-re-ask pairs (fire seed, let it finish,
then the re-ask in a NEW chat). Restart the gateway first (`pkill -f hermes_cli.main`).

**A/B — EVOLVING reuse (the fix). ✅ PASSED in-app 2026-06-22.**
- A (seed): "Research and write a detailed technical report comparing the three leading
  approaches to retrieval-augmented generation in production LLM systems as of 2026 …"
- B (re-ask): "I need a thorough technical write-up contrasting the main strategies teams use
  for retrieval-augmented generation in real-world LLM deployments right now — namely vector-DB
  RAG, long context windows, and agent-style multi-step retrieval …"
- Result: B augment-COMPARED against A at sim 0.853 — 2 calls vs the prior 13-call loop, reused
  $0.728, this turn $0.058; dedup_augment class=live mode=comparison, "what changed" section.

**C — TIMELESS control (reuse must NOT add comparison framing).**
- C (seed): "Explain how the TLS 1.3 handshake works — walk through the key exchange, certificate
  verification, and how the session keys get established. Be thorough."
- C (re-ask): "Can you walk me through the mechanics of a TLS 1.3 handshake — the key exchange,
  the cert checks, and how the session keys are set up?"
- Expect: classified `timeless` → plain reuse (or cache-serve if the seed was cheap). NO
  "what changed" section. Row: temporal_class=timeless, reuse_mode=plain (or status=deduped).

**D — LIVE control (the safety gate).**
- D (seed): "What's the current price of Bitcoin right now, and what's driving today's move?"
- D (re-ask): "Give me Bitcoin's latest price as of right now and what's moving it today."
- Expect: classified `live` → comparison-augment. The prior price MUST be framed as a dated
  baseline and the answer re-derived — a stale number is NEVER returned as current. (Topics for
  C/D are deliberately non-RAG so they can't match A.)

- **Classifier accuracy:** hand-label ~15 prompts across the 4 classes, check the aux-LLM
  tag; confirm the heuristic fallback agrees on the obvious ones.
- Reconcile in the ledger/events (similarity, reused_saved, temporal class), per the
  scorecard pattern used in this session.

---

## Read first on resume
- `optional-skills/finance/bursar/scripts/bursar_live.py` — `find_dup` (287), `is_freshness_intent`
  + `_FRESHNESS_INTENT` (~735), `is_synthesizable_api_mode` (768; `_SYNTHESIZABLE_API_MODES =
  {'', 'chat_completions'}`), `dedup_augment_block` (480), `dedup_reply`, `original_turn_summary`,
  `is_servable_answer` (347).
- `optional-skills/finance/bursar/scripts/bursar_model.py` — `_aux_client()` (212) + `_AuxUnavailable`:
  the EXACT Hermes aux-LLM pattern to copy for the classifier. **Hermes-native principle: use this.**
- `plugins/bursar_gate/__init__.py` — `_gate` (412), capture+arm (440–458), H5 branch (487),
  augment/serve branch (496–575), H12 cost logic (510–520). This is where the router goes.
- `feature_list.json` (repo root) — new "Temporal-class routing" group (T1–T4) added this session.
- DB for verification: `~/.hermes/bursar/bursar.db` (real); query `queries`/`events`/`ledger`.

## Known facts / dead-ends (don't re-derive)
- Matcher, threshold (0.78 numpy), enforcement: all confirmed WORKING. Do not chase them.
- DEBUG logging is OFF by default → gate `log.debug` decisions don't hit `~/.hermes/logs/agent.log`.
  Turn on DEBUG to see router decisions, or persist the temporal class on the row.
- The active embed backend is selected by `BURSAR_EMBED_BACKEND=numpy` (NOT `BURSAR_EMBED`).
  Rows store `embed_model`; `find_dup` only compares same-space rows.
- Augment runs a fresh call → never stale. SERVE is the only stale-risk path. Gate freshness on SERVE only.

---

## Standing constraints (verbatim — survive every compaction)
- **Stripe TEST mode only** (`sk_test_`). Refuse `sk_live_`. Account = a dedicated Stripe **test** account.
- **Never commit/push git unless explicitly asked.** Repo root is NOT a git repo; nested `hermes-agent/` IS.
- **Never fabricate numbers.** Honest/prove-every-number accounting. Real OpenModel spend is the live
  gate only (~$0.21 to date); the demo/firehose is SYNTHETIC (placeholder results, no model calls).
- **Fail-open always.** Bursar never breaks a chat turn; Stripe/gate errors never surface to the user.
- **Never serve stale data as current.** Freshness safety applies to dedup-SERVE; augment (fresh call)
  is safe. When classification is uncertain, bias to fresh/augment-comparison, never stale-serve.
- **Gateway-restart gotcha:** new gate/engine code needs `pkill -f hermes_cli.main` + relaunch;
  Cmd-R / app restart alone keeps stale gate code.
- **Don't hand-edit `plugins/bursar/dist-bundle/`** — regenerate via `plugins/bursar/package.sh`.
- **The LIVE Trading Floor UI is `apps/desktop/src/app/bursar/`** (Electron); the plugin frontend
  (`plugins/bursar/dashboard/frontend` → dist) is a D0 SMOKE STUB — do not edit it. Rebuild apps/desktop
  with `npm run build`.
- **Hermes-native principle:** prefer Hermes primitives — the temporal classifier uses the Hermes aux
  LLM client (`agent.auxiliary_client`), like `bursar_model.py`.
- **SUPERSEDED:** the old "byte-stable calibrated demo ~31%/34.7%" invariant is GONE. Savings are now
  model-agnostic real $ (cost=spend, saved=dedup+gate+reuse), same methodology for demo and live.
