"""Bursar — cost estimation and model routing.

Two jobs:

* **Cost estimator** (feature A2): predict how many tokens a query will
  consume and multiply by the per-model price to get a dollar estimate
  *before* the query runs.
* **Model router** (feature A6): given a query's value tier, pick the
  cheapest model that clears that tier's quality bar — the mechanism that
  kills the "4,500x price spread, everyone defaults to the best model"
  waste the pitch calls out.

Price table
-----------
``MODEL_CATALOG`` holds REAL, sourced public list prices. The numbers are
the blended (25% input / 75% output — agentic completions skew output-heavy)
figure derived from Hermes's own official pricing snapshot
(``agent.usage_pricing._OFFICIAL_DOCS_PRICING``, each entry carrying a
``source_url`` + ``pricing_version``). These five entries are Bursar's
routing tiers (one per quality band); the dashboard ``/catalog`` endpoint
serves the FULL sourced menu (~30 models) and its true ~500x price spread,
flagging which models the router actually sends work to. Model ids match the
snapshot exactly so the ``in_use`` join is clean.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class Model:
    name: str
    price_per_mtok: float   # blended USD / 1e6 tokens (representative list price)
    quality: int            # 1 (tiny) .. 5 (premium frontier)


# Real, sourced prices — blended USD / 1M tokens (0.25*input + 0.75*output)
# from Hermes's official pricing snapshot. Names match the snapshot ids exactly.
# One model per quality band = Bursar's routing tiers (cross-provider on purpose).
MODEL_CATALOG: list[Model] = [
    Model("gpt-4.1-nano",          0.325, quality=1),  # in 0.10 / out 0.40  (OpenAI)
    Model("claude-haiku-4-5",      4.000, quality=2),  # in 1.00 / out 5.00  (Anthropic)
    Model("gpt-4o",                8.125, quality=3),   # in 2.50 / out 10.00 (OpenAI) — standard tier
    Model("claude-opus-4-8",      20.000, quality=4),  # in 5.00 / out 25.00 (Anthropic) — high-stakes
    Model("claude-opus-4-8-fast", 40.000, quality=5),  # in 10.00 / out 50.00 (Anthropic) — premium ceiling
]

MODEL_BY_NAME = {m.name: m for m in MODEL_CATALOG}

# Map a value tier to the minimum model quality that satisfies it. Trivia
# can ride the floor model; high-stakes work needs a frontier model.
TIER_MIN_QUALITY = {
    "trivial": 1,
    "standard": 3,
    "high_stakes": 4,
}

# Expected completion length (tokens) by tier — high-stakes answers are
# longer and more expensive, trivia is terse.
TIER_COMPLETION_TOKENS = {
    "trivial": 200,
    "standard": 700,
    "high_stakes": 1800,
}

# Realistic *input* context per tier (system prompt + retrieved/RAG context +
# tool output + conversation history). Enterprise agentic queries are
# context-heavy — this is where the real token spend lives, far more than the
# short user prompt. Without it, costs at real per-token prices round to zero
# and the flat trading fee dominates the bill.
TIER_CONTEXT_TOKENS = {
    "trivial": 1_200,
    "standard": 9_000,
    "high_stakes": 30_000,
}

# ~4 characters per token is the standard rough rule for English text.
_CHARS_PER_TOKEN = 4.0


def predict_tokens(prompt: str, tier: str) -> int:
    """Predict total tokens (context + prompt + completion) for a query.

    Context tokens from the tier's typical retrieval/history footprint, prompt
    tokens from a chars/token heuristic, completion tokens from the tier's
    expected answer length. Deterministic so the estimate is reproducible and
    auditable.
    """
    prompt_tokens = math.ceil(len(prompt or "") / _CHARS_PER_TOKEN)
    context_tokens = TIER_CONTEXT_TOKENS.get(tier, TIER_CONTEXT_TOKENS["standard"])
    completion_tokens = TIER_COMPLETION_TOKENS.get(tier, TIER_COMPLETION_TOKENS["standard"])
    return int(context_tokens + prompt_tokens + completion_tokens)


def cost_for_model(tokens: int, model: Model) -> float:
    """Dollar cost of running ``tokens`` tokens through ``model``."""
    return round(tokens / 1_000_000 * model.price_per_mtok, 6)


def route(tier: str) -> Model:
    """Cheapest model whose quality clears the tier's bar (feature A6).

    Falls back to the highest-quality model if no catalog entry meets the
    bar (shouldn't happen with the default catalog, but keeps routing total).
    """
    min_q = TIER_MIN_QUALITY.get(tier, TIER_MIN_QUALITY["standard"])
    qualifying = [m for m in MODEL_CATALOG if m.quality >= min_q]
    if not qualifying:
        return max(MODEL_CATALOG, key=lambda m: m.quality)
    return min(qualifying, key=lambda m: m.price_per_mtok)


@dataclass(frozen=True)
class CostEstimate:
    tokens: int
    model: str
    est_cost: float
    naive_cost: float   # cost if everything ran on the premium model (the baseline waste)


def estimate(prompt: str, tier: str, *, model: Optional[str] = None) -> CostEstimate:
    """Predict tokens, route to a model (or use the forced ``model``), and
    return the dollar estimate alongside the naive premium-model baseline.

    ``naive_cost`` is what the same query would cost if the org did what the
    pitch says everyone does today — send everything to the best model. The
    gap between ``est_cost`` and ``naive_cost`` is the routing saving.
    """
    chosen = MODEL_BY_NAME[model] if model else route(tier)
    tokens = predict_tokens(prompt, tier)
    premium = max(MODEL_CATALOG, key=lambda m: m.price_per_mtok)
    return CostEstimate(
        tokens=tokens,
        model=chosen.name,
        est_cost=cost_for_model(tokens, chosen),
        naive_cost=cost_for_model(tokens, premium),
    )


if __name__ == "__main__":
    for t in ("trivial", "standard", "high_stakes"):
        est = estimate("Summarize our Q3 churn drivers in two sentences.", t)
        print(f"{t:12s} -> {est.model:14s} {est.tokens:5d} tok  ${est.est_cost:.5f}  (naive ${est.naive_cost:.5f})")
