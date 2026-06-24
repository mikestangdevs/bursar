"""Bursar — agentic-loop savings proof (G-series).

Drives a realistic long-running AGENT loop through the live gate's REAL decision
path (``bursar_live``: ``quote`` -> ``find_dup`` -> ``record_serviced`` /
``record_deduped``) and reports the OFF-vs-ON bill: what the loop would cost
without Bursar versus with it. This is the "agents leverage past work" thesis
made measurable.

Distinct from its siblings on purpose:
  * ``bench.py``     — dispatcher throughput / correctness at enterprise scale.
  * ``realcheck.py`` — real external API (Stripe/Nemotron) ceilings.
  * ``agentloop.py`` — the GATE path that real agent LLM calls traverse, scored
                       for SAVINGS on a repetitive agent workload.

Why a loop (not a fresh task): the gate's leverage — dedup-serve + down-route —
only fires on repetition. A fresh task's steps are mostly unique, so savings are
flat. An agent that polls / retries / re-researches re-issues the same and
near-same calls, and that is exactly where "you already paid to think about
this" pays off. So the workload is an honest multi-step task (genuine unique
progress steps) wrapping a repetitive core (exact repeats + paraphrased
near-dups).

G0  ``build_workload`` — seeded, reproducible scenario.
G1  ``replay``         — per-step real gate decision + cost + lane.
G2  ``summarize`` / ``report`` — OFF-vs-ON bill, $ saved, % saved, calls
                                 collapsed, tokens skipped.

Pricing note (see memory bursar-live-pricing-decision): figures are market-rate
regardless of executor. ``naive_cost`` (the estimator's premium-model baseline)
is the no-Bursar bill; ``est_cost`` is Bursar's routed price; a dedup hit costs
~$0. Scoring is the fast heuristic by default (reproducible, free); ``--nemotron``
swaps in the real value-scorer.

Usage:
    python3 agentloop.py --steps 40 --repeat-rate 0.35 --near-dup-rate 0.25 --seed 1
    python3 agentloop.py --reset        # clear this run's prior loop rows first
    python3 agentloop.py --json         # emit a machine-readable record too
"""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import asdict, dataclass
from typing import Optional

import bursar_db as db
import bursar_live as live
# Realism reuse: the firehose's composer + paraphraser already manufacture
# value-tiered prompts and >=0.90-cosine near-duplicates.
from firehose import (
    ACTIONS_HIGH,
    ACTIONS_LOW,
    ACTIONS_STD,
    _compose,
    _paraphrase,
)

LOOP_TEAM = "agent-loop"


# --------------------------------------------------------------------------
# G0 — the scenario
# --------------------------------------------------------------------------

@dataclass
class Step:
    idx: int
    prompt: str
    kind: str  # "unique" | "repeat" | "near_dup"


def build_workload(*, steps: int, repeat_rate: float, near_dup_rate: float,
                   value_mix: tuple[float, float, float], seed: int) -> list[Step]:
    """Emit a reproducible agent loop as an ordered list of steps.

    ``repeat_rate`` / ``near_dup_rate`` are the fraction of steps that re-issue
    an earlier *unique* step verbatim / as a paraphrase. The remainder are fresh
    unique steps, value-tiered by ``value_mix`` = (high, standard, low) weights.
    Seeds the module ``random`` (which the firehose helpers use) so the same
    seed yields a byte-identical sequence.
    """
    import random
    random.seed(seed)
    w_high, w_std, _w_low = value_mix
    seen: list[str] = []
    out: list[Step] = []
    for i in range(steps):
        r = random.random()
        if seen and r < repeat_rate:
            out.append(Step(i, random.choice(seen), "repeat"))
        elif seen and r < repeat_rate + near_dup_rate:
            out.append(Step(i, _paraphrase(random.choice(seen)), "near_dup"))
        else:
            rr = random.random()
            actions = (ACTIONS_HIGH if rr < w_high
                       else ACTIONS_STD if rr < w_high + w_std
                       else ACTIONS_LOW)
            prompt = _compose(actions)
            seen.append(prompt)
            out.append(Step(i, prompt, "unique"))
    return out


# --------------------------------------------------------------------------
# G1 — gate-faithful decision replay
# --------------------------------------------------------------------------

@dataclass
class StepResult:
    idx: int
    kind: str
    lane: str            # "served" (ran a real call) | "deduped" (from cache)
    tier: str
    naive_cost: float    # premium-for-everything price (the naive enterprise bill)
    routed_cost: float   # right-sized model price (what running it WOULD cost)
    actual_cost: float   # what Bursar billed: 0 if deduped, else routed_cost
    fee: float           # Bursar's per-call governance toll (charged only when served)
    tokens: int          # predicted tokens for this call (all skipped if deduped)
    sim: Optional[float]  # cosine on a dedup hit, else None


def _stub_answer(prompt: str) -> str:
    """A servable stand-in answer for a full serve, so subsequent dups are
    genuinely servable from cache (``is_servable_answer``: >=16 chars, not the
    ``[serviced via …]`` placeholder). The *cost* is real (model-priced); only
    the answer text is stubbed in this compute-free mode."""
    return (f"Findings for: {prompt} Analysis complete — key drivers identified, "
            f"risks flagged, and recommended next steps outlined.")


def replay(conn, workload: list[Step], *, team: str = LOOP_TEAM,
           threshold: Optional[float] = None, armed: bool = True) -> list[StepResult]:
    """Run each step through the real gate decision path and record it.

    Armed (the gate's true behaviour): dedup-serve a servable near-dup for ~$0,
    else full serve at the routed (possibly down-routed) model. Disarmed: no
    dedup / no routing — every call pays the premium-model price (used by G3's
    on-camera before pass).
    """
    results: list[StepResult] = []
    for step in workload:
        q = live.quote(step.prompt, team=team)
        naive, routed = q.naive_cost, q.est_cost

        if armed:
            # Scope dedup to the loop's own team so the proof reflects the loop's
            # repetition, not accidental matches against unrelated live traffic
            # (real chats) that may already be in the DB.
            dup = live.find_dup(conn, step.prompt, threshold=threshold, team=team)
            if dup is not None and live.is_servable_answer(dup[2]):
                orig_id, sim, cached = dup
                # Saved vs what running it WOULD have cost (the right-sized
                # routed price), not the premium strawman — honest dedup credit.
                # Pass the would-run economics so the stored row (and the floor's
                # baseline) value the reuse instead of treating it as free.
                live.record_deduped(
                    conn, team=team, prompt=step.prompt, orig_id=orig_id,
                    sim=sim, saved=routed, cached_result=cached,
                    value=q.value, tier=q.tier, rationale=q.rationale,
                    est_tokens=q.est_tokens, est_cost=routed,
                )
                results.append(StepResult(step.idx, step.kind, "deduped", q.tier,
                                          naive, routed, 0.0, q.fee, q.est_tokens, float(sim)))
                continue

        actual = routed if armed else naive  # disarmed (G3 'before' pass) pays premium
        live.record_serviced(
            conn, team=team, prompt=step.prompt, model=q.model,
            tokens=q.est_tokens, token_cost=actual, value=q.value, tier=q.tier,
            rationale=q.rationale, naive_cost=naive, result=_stub_answer(step.prompt),
        )
        results.append(StepResult(step.idx, step.kind, "served", q.tier,
                                   naive, routed, round(actual, 6), q.fee, q.est_tokens, None))
    return results


# --------------------------------------------------------------------------
# G2 — OFF-vs-ON savings report
# --------------------------------------------------------------------------

def summarize(results: list[StepResult]) -> dict:
    """Aggregate a replay into a LAYERED, honest proof record.

    Three bills, each a strict superset of savings over the last — so the
    headline (leverage past work = dedup) is never inflated by the routing
    baseline, and every layer is auditable:
      * ``premium_bill``  — every call on the premium model, no reuse (naive).
                            No Bursar in this world, so no governance fee.
      * ``routed_bill``   — right-sized models, still no reuse: every call runs
                            and pays Bursar's per-call fee.
      * ``bursar_bill``   — right-sized + dedup: only served calls run + pay the
                            fee; reused calls cost $0. This is exactly the live
                            ledger's ``bill`` (token_cost + fee over served), so
                            the in-app panel reconciles to the floor's Spend.
    """
    deduped = [r for r in results if r.lane == "deduped"]
    served = [r for r in results if r.lane == "served"]

    # The fee is Bursar's toll, charged per call that actually runs. The naive
    # (no-Bursar) world pays none; the routed world runs every call (fee on all);
    # Bursar runs only the served calls (fee on those).
    fee_all = sum(r.fee for r in results)
    fee_served = sum(r.fee for r in served)

    premium_bill = round(sum(r.naive_cost for r in results), 6)
    routed_bill = round(sum(r.routed_cost for r in results) + fee_all, 6)  # would-run, no dedup
    bursar_bill = round(sum(r.actual_cost for r in results) + fee_served, 6)

    dedup_saved = round(routed_bill - bursar_bill, 6)            # the leverage headline
    routing_saved = round(premium_bill - routed_bill, 6)         # right-sizing bonus
    total_saved = round(premium_bill - bursar_bill, 6)

    def pct(part, whole):
        return round(100.0 * part / whole, 1) if whole > 0 else 0.0

    return {
        "steps": len(results),
        "premium_bill": premium_bill,
        "routed_bill": routed_bill,
        "bursar_bill": bursar_bill,
        # Headline: leverage past work (dedup), measured against the would-run
        # routed bill so routing can't pad it.
        "dedup_saved": dedup_saved,
        "dedup_saved_pct": pct(dedup_saved, routed_bill),
        # Bonus + grand total, each clearly attributed.
        "routing_saved": routing_saved,
        "total_saved": total_saved,
        "total_saved_pct": pct(total_saved, premium_bill),
        "served": len(served),
        "deduped": len(deduped),
        "tokens_skipped": sum(r.tokens for r in deduped),
    }


def report(summary: dict) -> str:
    """Human-readable terminal summary — layered so each saving is attributed."""
    s = summary
    return "\n".join([
        "",
        "  Bursar — agentic-loop savings proof",
        "  " + "-" * 52,
        f"  Loop steps                  {s['steps']}  "
        f"({s['served']} served, {s['deduped']} reused from cache)",
        "  " + "-" * 52,
        f"  Naive (premium, no reuse)   ${s['premium_bill']:.4f}",
        f"  + right-size each call      ${s['routed_bill']:.4f}   "
        f"(routing saved ${s['routing_saved']:.4f})",
        f"  + reuse past work (Bursar)  ${s['bursar_bill']:.4f}   "
        f"(dedup saved ${s['dedup_saved']:.4f}, {s['dedup_saved_pct']}%)",
        "  " + "-" * 52,
        f"  LEVERAGE-PAST-WORK saving   ${s['dedup_saved']:.4f}  ({s['dedup_saved_pct']}%)  "
        f"<- the headline",
        f"  Total vs naive enterprise   ${s['total_saved']:.4f}  ({s['total_saved_pct']}%)",
        f"  Tokens skipped (never ran)  {s['tokens_skipped']:,}",
        "",
    ])


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def _clear_loop_rows(conn, team: str) -> int:
    """Delete only this run's rows so a re-run starts from a clean, isolated
    cache (the headline number is precise only against a clean loop team)."""
    with db.write_txn(conn):
        cur = conn.execute(
            "DELETE FROM ledger WHERE query_id IN (SELECT id FROM queries WHERE team=?)",
            (team,),
        )
        conn.execute("DELETE FROM queries WHERE team=?", (team,))
        conn.execute("DELETE FROM budgets WHERE team=?", (team,))
    return cur.rowcount


def main():
    ap = argparse.ArgumentParser(description="Bursar agentic-loop savings proof")
    ap.add_argument("--steps", type=int, default=40, help="total loop steps")
    ap.add_argument("--repeat-rate", type=float, default=0.30,
                    help="fraction of steps that re-issue a prior step verbatim")
    ap.add_argument("--near-dup-rate", type=float, default=0.25,
                    help="fraction of steps that re-issue a prior step paraphrased")
    ap.add_argument("--value-mix", default="0.30,0.40,0.30", help="high,standard,low weights")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--team", default=LOOP_TEAM)
    ap.add_argument("--threshold", type=float, default=None, help="override dedup cosine threshold")
    ap.add_argument("--reset", action="store_true", help="clear this run's prior loop rows first")
    ap.add_argument("--json", action="store_true", help="also print the machine-readable record")
    ap.add_argument("--nemotron", action="store_true", help="use the real Nemotron value-scorer")
    args = ap.parse_args()

    if not args.nemotron:
        os.environ["BURSAR_USE_NEMOTRON"] = "0"

    wh, ws, wl = (float(x) for x in args.value_mix.split(","))
    workload = build_workload(
        steps=args.steps, repeat_rate=args.repeat_rate,
        near_dup_rate=args.near_dup_rate, value_mix=(wh, ws, wl), seed=args.seed,
    )

    conn = db.connect()
    if args.reset:
        n = _clear_loop_rows(conn, args.team)
        print(f"cleared {n} prior loop ledger rows for team '{args.team}'")

    results = replay(conn, workload, team=args.team, threshold=args.threshold)
    summary = summarize(results)
    print(report(summary))
    if args.json:
        print(json.dumps({"summary": summary, "steps": [asdict(r) for r in results]}, indent=2))
    conn.close()


if __name__ == "__main__":
    main()
