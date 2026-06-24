# Temporal-Class Router + Historical-Comparison Augment

A design note on how Bursar decides whether a re-asked question can reuse prior
work — and how it turns time-sensitive re-asks into a feature instead of a
blocked reuse.

---

## The problem it solves

Bursar dedups semantically: a reworded near-twin of an earlier expensive research
turn should reuse that work. But a naïve **freshness guard** (decline reuse when
the prompt says "now", "latest", "as of") misfires badly — *"the latest RAG
strategies teams use right now"* is **evolving knowledge**, not a live-data
lookup, yet a regex can't tell it apart from *"latest BTC price"*. The result:
Bursar refuses to reuse a large fraction of legitimate research re-asks — the
single most valuable dedup target.

## The key insight

The danger of serving **stale** data exists only for dedup-**SERVE** (return the
cached answer with no fresh call). **Augment always runs a fresh call** — it just
injects the prior answer as *context* — so an augmented answer is never stale; the
prior is a baseline, not the served result.

So freshness should gate SERVE only, never AUGMENT. And for time-sensitive
re-asks, augment becomes a feature: *"here's what changed since you last asked."*

## The taxonomy

Freshness isn't a uniform property — it's a per-query temporal class:

| Class | Example | Reuse strategy |
|---|---|---|
| **Timeless** (never changes) | "how does vector RAG work" | SERVE if the original was cheap / AUGMENT-plain if it was expensive. No freshness check. |
| **Evolving** (weeks–months) | "latest RAG strategies", "state of batteries 2026" | **AUGMENT-COMPARISON** — inject the prior as a dated baseline, re-derive fresh, surface the diff. ← the unlock |
| **Live** (sec–min) | "BTC price now", "is the server up" | AUGMENT-COMPARISON if a recent prior exists ("up $200 since you asked"), else FRESH. Never serve stale. |
| **Stateful / personal** | "my calendar", "my last commit" | FRESH. The prior is likely invalid. |

The decision is `temporal_class(query) × age(cached_answer) × original_cost`.

## How it works

1. **Classifier** — a Hermes-native auxiliary-LLM call sorts the prompt into one
   of the four classes (cached per prompt), with a regex + volatile-noun
   heuristic fallback when the aux client is unavailable. The fail-safe direction
   is **evolving** (augment-comparison), because augment runs fresh and is never
   stale; pure SERVE is chosen only when the class is confidently timeless.
2. **Router** (in the gate) — on a turn-opening call that has a reusable match:
   timeless → serve / augment-plain; evolving → augment-comparison always; live →
   augment-comparison if the prior is recent, else fresh; stateful → fresh. The
   whole block is fail-open.
3. **Comparison augment** — injects the prior answer framed as a *dated baseline*,
   with instructions to re-derive the current state and explicitly call out what
   changed since — never to present the prior as current.
4. **Surfacing** — the temporal class and reuse mode are persisted on the query
   and shown on the Trading Floor order feed, so a historical comparison reads as
   a distinct, premium outcome (not just dollars saved).
