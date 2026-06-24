"""Bursar — query value scoring (feature A4).

Every query gets a **Value** score (0-100, business priority / expected
payoff) and a **tier** (trivial | standard | high_stakes) with a short
rationale. Value ÷ Cost is the rank key the dispatcher fills the budget by.

**Bursar prices its own cognition.** Scoring a query *costs* compute, so we
apply Bursar's own economics to the valuation step itself — a graduated
ladder instead of paying the biggest model to confirm the obvious:

1. ``heuristic`` (always first, free, instant): a transparent keyword model.
   High-stakes signals (revenue, incident, security, legal, board, churn,
   outage) lift the score; low-value signals (trivia, jokes) sink it.
2. **escalation gate** (behind ``BURSAR_USE_NEMOTRON=1``): escalate to a model
   only when a better estimate is worth its cost — *value of information*:
   * obviously trivial (heuristic < ``BURSAR_ESCALATE_FLOOR``, def 20) → keep
     the heuristic. Spending a 550B model to re-confirm "tell me a joke" is the
     exact waste Bursar exists to price out.
   * ambiguous middle (FLOOR ≤ v < ``BURSAR_ESCALATE_DEEP``, def 80) → a **fast
     Nemotron** (``…super-49b``, ~1.6 s): cheap, and this is where a better
     estimate most often flips the serve/reject/rank decision.
   * high stakes (v ≥ DEEP) → **Nemotron 3 Ultra** (``…ultra-550b``): the best
     brain where a wrong valuation is most expensive — and the model behind the
     "explain this valuation" drill-down.

Any model call falls back to the heuristic on failure/timeout, so a slow
endpoint can never sink the demo. For high-throughput / scale runs the flag is
left off (pure heuristic); the model prices the realistic-rate traffic.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Optional

VALID_TIERS = ("trivial", "standard", "high_stakes")


@dataclass(frozen=True)
class ValueScore:
    value: float          # 0..100
    tier: str
    rationale: str
    backend: str          # "heuristic" | "nemotron"
    model: str = ""       # wire model id when backend == "nemotron" (which tier)


# --- escalation ladder knobs (read at call time so .env is loaded) ---------

def _escalation_model(value: float) -> Optional[str]:
    """Which model (if any) is worth calling for a query the heuristic scored
    at ``value``. Returns a wire model id, or None to keep the heuristic."""
    floor = float(os.environ.get("BURSAR_ESCALATE_FLOOR", "20"))
    deep = float(os.environ.get("BURSAR_ESCALATE_DEEP", "80"))
    if value < floor:
        return None  # obviously trivial — not worth a model call
    if value >= deep:
        return os.environ.get("BURSAR_NEMOTRON_MODEL_DEEP",
                              "nvidia/nemotron-3-ultra-550b-a55b")
    return os.environ.get("BURSAR_NEMOTRON_MODEL_FAST",
                          "nvidia/llama-3.3-nemotron-super-49b-v1.5")


# Keyword → weight. Positive lifts business value, negative sinks it. These
# are illustrative signals for the demo, not a learned model — kept explicit
# so the score is auditable on screen.
_HIGH_VALUE_TERMS = {
    "revenue": 22, "churn": 22, "incident": 25, "outage": 25, "security": 24,
    "breach": 28, "legal": 20, "compliance": 18, "board": 18, "executive": 16,
    "customer": 14, "contract": 16, "deadline": 14, "production": 16,
    "forecast": 12, "budget": 12, "audit": 14, "risk": 12, "escalation": 18,
}
_LOW_VALUE_TERMS = {
    "joke": -30, "poem": -24, "trivia": -28, "meme": -28, "weather": -18,
    "recipe": -20, "horoscope": -30, "story": -16, "fun": -14, "chitchat": -26,
    "lunch": -18, "weekend": -14, "capital": -12,
}

_WORD_RE = re.compile(r"[a-z0-9]+")


# Words that carry no business signal — used to gauge query "substance" so a
# contentless query ("ok", "thanks", "test") scores near zero instead of the
# neutral default, and is correctly priced out as waste.
_FILLER = {
    "the", "a", "an", "is", "are", "to", "for", "of", "in", "on", "and", "or",
    "ok", "okay", "thanks", "thx", "hi", "hello", "hey", "test", "testing",
    "please", "pls", "yes", "no", "nvm", "hmm", "lol", "cool", "nice", "sure",
    "what", "how", "this", "that", "it", "me", "my", "our", "we", "you",
}


def _heuristic(prompt: str) -> ValueScore:
    tokens = _WORD_RE.findall((prompt or "").lower())
    words = set(tokens)
    content = [t for t in tokens if t not in _FILLER]
    base = 45.0  # neutral business query
    score = base
    hits: list[str] = []
    for term, w in _HIGH_VALUE_TERMS.items():
        if term in words:
            score += w
            hits.append(f"+{term}")
    for term, w in _LOW_VALUE_TERMS.items():
        if term in words:
            score += w
            hits.append(term)
    # Substance gate: a query with almost no content words can't carry much
    # business value regardless of keywords. Cap it low so junk is priced out.
    if len(content) <= 1:
        score = min(score, 3.0)
        hits.append("low-substance")
    elif len(content) == 2:
        score = min(score, 30.0)
    score = max(0.0, min(100.0, score))
    tier = _tier_for(score)
    if hits:
        rationale = "signals: " + ", ".join(hits[:6])
    else:
        rationale = "no strong signals; default business priority"
    return ValueScore(value=round(score, 1), tier=tier, rationale=rationale,
                      backend="heuristic", model="")


def _tier_for(value: float) -> str:
    if value >= 70:
        return "high_stakes"
    if value >= 35:
        return "standard"
    return "trivial"


def _nemotron(prompt: str, *, model: str) -> Optional[ValueScore]:
    """Ask ``model`` (a Nemotron tier chosen by the escalation gate) to rate
    the query. Returns None on any failure so the caller falls back to the
    heuristic. Strict JSON ``{"value", "rationale"}``; tier derived from value
    so tiering stays consistent with the heuristic path."""
    try:
        import bursar_model  # lazy: optional glue
    except Exception:
        return None
    # Skip a doomed network round-trip (the model timeout per query) when
    # nothing is wired — fail straight to the heuristic instead.
    if not bursar_model.is_configured():
        return None
    try:
        raw = bursar_model.score_value_with_model(prompt, model=model)
        value = float(raw.get("value"))
        value = max(0.0, min(100.0, value))
        rationale = str(raw.get("rationale") or "nemotron rating")[:200]
        return ValueScore(value=round(value, 1), tier=_tier_for(value),
                          rationale=rationale, backend="nemotron", model=model)
    except Exception:
        return None


def _flag_on() -> bool:
    """Read BURSAR_USE_NEMOTRON, loading the skill-root .env first so a flag
    set there (not just an exported var) is honored."""
    try:
        import bursar_model
        bursar_model._ensure_env()
    except Exception:
        pass
    return os.environ.get("BURSAR_USE_NEMOTRON") in ("1", "true", "yes", "on")


def score(prompt: str) -> ValueScore:
    """Score a query's business value via the escalation ladder: always take
    the free heuristic first, then escalate to a Nemotron tier only when the
    gate says a better estimate is worth its cost. Falls back to the heuristic
    on any model failure so the demo can't stall."""
    h = _heuristic(prompt)
    if not _flag_on():
        return h
    model = _escalation_model(h.value)
    if model is None:
        return h  # obviously trivial — not worth a model call
    ns = _nemotron(prompt, model=model)
    return ns if ns is not None else h


if __name__ == "__main__":
    samples = [
        "We have a production outage affecting customer checkout — root cause?",
        "Summarize this quarter's revenue forecast for the board.",
        "What's a good recipe for banana bread?",
        "Reformat these meeting notes into bullet points.",
    ]
    for s in samples:
        r = score(s)
        print(f"{r.value:5.1f} {r.tier:11s} [{r.backend}] {r.rationale}  | {s[:50]}")
