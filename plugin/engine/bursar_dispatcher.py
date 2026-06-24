"""Bursar — the dispatcher (features A1 + A5).

The trading floor's matching engine. Queries enter the exchange as
``pending``; a dispatch *tick* clears the market:

    ingest → dedup → value-score → cost-estimate → rank(value/cost)
           → route → budget-gate → service | queue | reject

Economic model (the thesis, not "just caching + routing")
---------------------------------------------------------
Each query carries a business **value** (0-100). We translate it to a
dollar worth: ``value_usd = value/100 * VALUE_PER_POINT``. A query is only
serviced if its worth clears what it costs to run plus the internal Stripe
**trading fee**:

    net = value_usd − (est_cost + fee)

* worth below ``MIN_WORTH`` → **rejected** as waste, or ``net <= 0`` → rejected:
                           the query isn't worth what it costs.
                           *Waste prices itself out.*
* ``net > 0`` but the team's **hard budget cap** is exhausted this period
                           → **queued**: valuable, but no budget now.
* otherwise              → **serviced**: charge the budget, meter the fee,
                           run it. Highest value-per-token first.

The **fee** is a proportional TAKE RATE on brokered compute (``fee_for`` =
``FEE_RATE`` × est_cost), metered through Stripe — not a flat toll. It scales
with the work so it never dwarfs a cheap call. Junk-rejection is decoupled onto
``MIN_WORTH`` (the worth floor) so the fee no longer has to be inflated to filter
waste. (Both jobs used to ride on one flat $0.05 fee, which made the fee 12× a
real DeepSeek call's cost on the live ledger.)

Dedup runs before any of this: a near-identical recent query collapses into
the original and returns its cached result for ~free (no fee, no budget hit).

The budget gate is *pre-execution* and *hard* — a tranche can never be
overspent, which is the whole point versus alerts that fire after the money's
gone.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, asdict
from typing import Optional

import numpy as np

import bursar_db as db
import bursar_dedup as dedup
import bursar_pricing as pricing
import bursar_scoring as scoring

# --- economic knobs (configurable; tuned for demo legibility) --------------

# Marginal business worth of a max-value (100) query, in dollars. Scales
# linearly with the value score (value=100 → $1.50, value=50 → $0.75). Tuned
# so worth straddles realistic per-query compute cost: a high-stakes query
# (value ~70+, ~$0.6 of context-heavy inference) clears its cost, while a
# near-zero-value flood (value < ~3) can't even clear the flat fee and is
# priced out. This is the value-vs-cost gate that makes "waste prices itself
# out" literal at real token prices.
VALUE_PER_POINT = float(os.environ.get("BURSAR_VALUE_PER_POINT", "1.5"))
# Internal trading fee per serviced query, metered through Stripe as usage-based
# billing. REDESIGNED 2026-06-22: a proportional TAKE RATE on brokered compute,
# not a flat toll. A flat toll had to be big enough to price out junk, which made
# it dwarf cheap real calls — a $0.004 model call carried a $0.05 fee = 12x compute
# on the live ledger, undercutting the "we save you money" thesis. A take rate
# scales with the work (tiny on a cheap call, larger on an expensive one), so it
# can never dominate compute at any model price:  fee = max(FEE_FLOOR, FEE_RATE*cost).
FEE_RATE = float(os.environ.get("BURSAR_FEE_RATE", "0.15"))     # 15% of brokered spend
FEE_FLOOR = float(os.environ.get("BURSAR_FEE_FLOOR", "0.0"))    # optional absolute minimum
TRADING_FEE = FEE_FLOOR  # legacy export name; the fee is now proportional via fee_for()
# Junk-rejection now lives on a WORTH floor, DECOUPLED from the fee. A query whose
# business worth is below this is rejected as waste regardless of the (now tiny)
# fee. Default 0.05 transplants the EXACT old cutoff (junk worth ~$0.045 < $0.05),
# so "waste prices itself out" is unchanged while the fee is freed to be a real
# take rate instead of doing double duty as both revenue and the junk filter.
MIN_WORTH = float(os.environ.get("BURSAR_MIN_WORTH", "0.05"))


def fee_for(est_cost: float) -> float:
    """Proportional trading fee on brokered compute (see FEE_RATE/FEE_FLOOR)."""
    return round(max(FEE_FLOOR, FEE_RATE * float(est_cost or 0.0)), 6)
# How many recently-serviced queries to keep in the dedup comparison window.
DEDUP_WINDOW = int(os.environ.get("BURSAR_DEDUP_WINDOW", "300"))


def value_usd(value: float) -> float:
    return round(value / 100.0 * VALUE_PER_POINT, 6)


# --------------------------------------------------------------------------
# Ingest — what the firehose calls
# --------------------------------------------------------------------------

def ingest(conn, prompt: str, team: str, *, ext_id: Optional[str] = None) -> str:
    """Insert a query into the exchange as ``pending`` and return its id.

    The embedding is computed at ingest (stored as compact float32 bytes) so
    the dispatch tick doesn't pay for it. No decision is made here — the next
    tick clears the market.
    """
    qid = db.new_query_id()
    emb = dedup.pack(dedup.embed_vec(prompt))
    with db.write_txn(conn):
        conn.execute(
            "INSERT INTO queries (id, team, prompt, embedding, status, created_at) "
            "VALUES (?, ?, ?, ?, 'pending', ?)",
            (qid, team, prompt, emb, db.now()),
        )
        db.emit_event(conn, "ingested", query_id=qid, payload={"team": team, "prompt": prompt[:120]})
    return qid


def ingest_many(conn, items: list[tuple[str, str]], *, emit_events: bool = False) -> int:
    """Batch-insert many ``(team, prompt)`` queries in a single transaction.

    Throughput path for scale tests / bursty firehose: one IMMEDIATE
    transaction instead of one per query. Embeddings are computed up front.
    Per-query ``ingested`` events are off by default (they'd swamp the event
    stream at scale); the dispatch tick still emits decision events.
    """
    ts = db.now()
    rows = []
    for team, prompt in items:
        rows.append((db.new_query_id(), team, prompt,
                     dedup.pack(dedup.embed_vec(prompt)), ts))
    with db.write_txn(conn):
        conn.executemany(
            "INSERT INTO queries (id, team, prompt, embedding, status, created_at) "
            "VALUES (?, ?, ?, ?, 'pending', ?)",
            rows,
        )
        if emit_events:
            for qid, team, prompt, _emb, _ts in rows:
                db.emit_event(conn, "ingested", query_id=qid,
                              payload={"team": team, "prompt": prompt[:120]})
    return len(rows)


# --------------------------------------------------------------------------
# Quote — score + cost + route a single query (no decision yet)
# --------------------------------------------------------------------------

@dataclass
class Quote:
    qid: str
    team: str
    prompt: str
    embedding: "np.ndarray"
    value: float
    tier: str
    rationale: str
    est_tokens: int
    est_cost: float
    naive_cost: float
    model: str
    fee: float
    worth: float          # value in dollars
    net: float            # worth − (est_cost + fee)
    vpt: float            # value-per-token rank key: worth / (est_cost + fee)


def _quote(row) -> Quote:
    prompt = row["prompt"]
    vs = scoring.score(prompt)
    est = pricing.estimate(prompt, vs.tier)
    fee = fee_for(est.est_cost)
    worth = value_usd(vs.value)
    spend = est.est_cost + fee
    net = round(worth - spend, 6)
    if worth < MIN_WORTH and net > 0:
        net = 0.0  # below the junk-worth floor → reject regardless of the proportional fee
    vpt = round(worth / spend, 4) if spend > 0 else float("inf")
    raw = row["embedding"]
    emb = dedup.unpack(raw) if raw else dedup.embed_vec(prompt)
    return Quote(
        qid=row["id"], team=row["team"], prompt=prompt, embedding=emb,
        value=vs.value, tier=vs.tier, rationale=vs.rationale,
        est_tokens=est.tokens, est_cost=est.est_cost, naive_cost=est.naive_cost,
        model=est.model, fee=fee, worth=worth, net=net, vpt=vpt,
    )


# --------------------------------------------------------------------------
# Tick — clear the market
# --------------------------------------------------------------------------

@dataclass
class TickResult:
    serviced: int = 0
    deduped: int = 0
    rejected: int = 0
    queued: int = 0
    bill: float = 0.0          # $ charged this tick (token cost + fees)
    saved_dedup: float = 0.0   # $ avoided by dedup
    saved_routing: float = 0.0 # $ avoided by routing vs naive premium model

    def as_dict(self) -> dict:
        return asdict(self)


class _Window:
    """Fixed-size circular buffer of recently-serviced embeddings for
    vectorized dedup. Insert is O(1); match is one (W x D) @ (D,) BLAS matvec.

    Empty slots stay zero, so their cosine is 0 and can never clear the
    dedup threshold — matching against the whole buffer is therefore safe
    even before it fills.
    """

    def __init__(self, size: int = DEDUP_WINDOW, dim: int = dedup.EMBED_DIM):
        self.size = size
        self.mat = np.zeros((size, dim), dtype=np.float32)
        self.ids: list[Optional[str]] = [None] * size
        self.pos = 0

    def add(self, qid: str, vec: "np.ndarray") -> None:
        self.mat[self.pos] = vec
        self.ids[self.pos] = qid
        self.pos = (self.pos + 1) % self.size

    def match(self, qvec: "np.ndarray", threshold: float) -> Optional[tuple[str, float]]:
        sims = self.mat @ qvec
        j = int(np.argmax(sims))
        best = float(sims[j])
        if best >= threshold and self.ids[j] is not None:
            return self.ids[j], best  # type: ignore[return-value]
        return None


def _seed_window(conn) -> _Window:
    """Pre-fill the window with the most-recently-serviced queries from the DB
    so dedup spans across ticks, not just within one."""
    win = _Window()
    rows = conn.execute(
        "SELECT id, embedding FROM queries WHERE status = 'serviced' "
        "AND embedding IS NOT NULL ORDER BY decided_at DESC LIMIT ?",
        (DEDUP_WINDOW,),
    ).fetchall()
    # Insert oldest-first so the newest end up at the front of the buffer.
    for r in reversed(rows):
        try:
            win.add(r["id"], dedup.unpack(r["embedding"]))
        except Exception:
            continue
    return win


def run_tick(conn, *, meter=None, bypass=False) -> TickResult:
    """Clear all pending+queued queries. ``meter`` is an optional callable
    ``meter(team, model, tokens, total) -> stripe_event_id|None`` so the
    Stripe layer (A7) can settle each serviced query; ``None`` records the
    ledger locally only.

    ``bypass=True`` is **Bursar OFF** — the demo's "before" world: every query
    is serviced on the premium model with *no* dedup, *no* value gate, and *no*
    budget gate (overspend allowed). It's how the trading floor shows the
    runaway bill the exchange exists to prevent. Default ``False`` preserves
    the full pipeline exactly (A1/A5)."""
    res = TickResult()

    pending = conn.execute(
        "SELECT * FROM queries WHERE status IN ('pending', 'queued') "
        "ORDER BY created_at ASC"
    ).fetchall()
    if not pending:
        _snapshot(conn, res)
        return res

    if bypass:
        return _run_tick_bypass(conn, pending, meter, res)

    quotes = [_quote(r) for r in pending]
    # Rank by value-per-token, highest first — fill the budget like a desk.
    quotes.sort(key=lambda q: q.vpt, reverse=True)

    window = _seed_window(conn)
    # Track per-team spend within this tick against the live budget.
    budgets = {b["team"]: dict(b) for b in db.list_budgets(conn)}

    for q in quotes:
        match = window.match(q.embedding, dedup.DEFAULT_THRESHOLD)
        if match is not None:
            orig_id, sim = match
            _mark_deduped(conn, q, orig_id, sim)
            res.deduped += 1
            res.saved_dedup += round(q.est_cost + q.fee, 6)
            continue

        if q.net <= 0:
            _mark_rejected(conn, q)
            res.rejected += 1
            res.saved_dedup += 0.0  # rejection saves the would-be inference too
            continue

        b = budgets.get(q.team)
        spend = round(q.est_cost + q.fee, 6)
        if b is not None and (b["spent"] + spend) > b["cap"] + 1e-9:
            _mark_queued(conn, q)
            res.queued += 1
            continue

        # Service it.
        stripe_event_id = None
        if meter is not None:
            try:
                stripe_event_id = meter(q.team, q.model, q.est_tokens, spend)
            except Exception:
                stripe_event_id = None
        _mark_serviced(conn, q, spend, stripe_event_id)
        if b is not None:
            b["spent"] = round(b["spent"] + spend, 6)
        res.serviced += 1
        res.bill += spend
        res.saved_routing += round(max(0.0, q.naive_cost - q.est_cost), 6)
        # Newly serviced query joins the dedup window so later same-tick
        # duplicates collapse into it.
        window.add(q.qid, q.embedding)

    res.bill = round(res.bill, 6)
    res.saved_dedup = round(res.saved_dedup, 6)
    res.saved_routing = round(res.saved_routing, 6)
    _snapshot(conn, res)
    return res


def _run_tick_bypass(conn, pending, meter, res: TickResult) -> TickResult:
    """Bursar OFF: service every pending query on the premium model with no
    gating. This is the pitch's "before" — what spend looks like when everyone
    defaults to the best model and nothing prices waste out. The bill is the
    naive all-premium cost; ``saved_*`` stay 0 (the exchange did nothing)."""
    premium = max(pricing.MODEL_CATALOG, key=lambda m: m.price_per_mtok)
    for r in pending:
        q = _quote(r)
        # Everyone defaults to the best model; charge its (much higher) cost.
        q.model = premium.name
        q.est_cost = q.naive_cost
        spend = round(q.naive_cost + q.fee, 6)
        stripe_event_id = None
        if meter is not None:
            try:
                stripe_event_id = meter(q.team, q.model, q.est_tokens, spend)
            except Exception:
                stripe_event_id = None
        _mark_serviced(conn, q, spend, stripe_event_id)
        res.serviced += 1
        res.bill += spend
    res.bill = round(res.bill, 6)
    _snapshot(conn, res)
    return res


# --------------------------------------------------------------------------
# State transitions — each writes the query row + an event + (maybe) ledger
# --------------------------------------------------------------------------

def _common_update(conn, q: Quote, status: str) -> None:
    conn.execute(
        "UPDATE queries SET value=?, tier=?, rationale=?, est_tokens=?, est_cost=?, "
        "fee=?, vpt=?, chosen_model=?, status=?, decided_at=? WHERE id=?",
        (q.value, q.tier, q.rationale, q.est_tokens, q.est_cost, q.fee, q.vpt,
         q.model, status, db.now(), q.qid),
    )


def _mark_serviced(conn, q: Quote, spend: float, stripe_event_id) -> None:
    result_text = f"[serviced via {q.model}] answer to: {q.prompt[:80]}"
    with db.write_txn(conn):
        _common_update(conn, q, "serviced")
        conn.execute("UPDATE queries SET result=? WHERE id=?", (result_text, q.qid))
        conn.execute(
            "INSERT INTO ledger (query_id, team, model, tokens, token_cost, fee, total, "
            "stripe_event_id, settled, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (q.qid, q.team, q.model, q.est_tokens, q.est_cost, q.fee, spend,
             stripe_event_id, 1 if stripe_event_id else 0, db.now()),
        )
        conn.execute(
            "UPDATE budgets SET spent = spent + ?, updated_at=? WHERE team=?",
            (spend, db.now(), q.team),
        )
        db.emit_event(conn, "serviced", query_id=q.qid, payload={
            "team": q.team, "model": q.model, "value": q.value, "tier": q.tier,
            "est_cost": q.est_cost, "fee": q.fee, "total": spend, "vpt": q.vpt,
            "stripe_event_id": stripe_event_id,
        })


def _mark_deduped(conn, q: Quote, orig_id: str, sim: float) -> None:
    orig = conn.execute("SELECT result FROM queries WHERE id=?", (orig_id,)).fetchone()
    cached = orig["result"] if orig else None
    with db.write_txn(conn):
        _common_update(conn, q, "deduped")
        conn.execute(
            "UPDATE queries SET dedup_of=?, result=? WHERE id=?",
            (orig_id, cached, q.qid),
        )
        db.emit_event(conn, "deduped", query_id=q.qid, payload={
            "team": q.team, "dedup_of": orig_id, "similarity": round(sim, 3),
            "saved": round(q.est_cost + q.fee, 6),
        })


def _mark_rejected(conn, q: Quote) -> None:
    with db.write_txn(conn):
        _common_update(conn, q, "rejected")
        db.emit_event(conn, "rejected", query_id=q.qid, payload={
            "team": q.team, "value": q.value, "worth": q.worth,
            "cost_plus_fee": round(q.est_cost + q.fee, 6), "reason": "value below cost+fee",
        })


def _mark_queued(conn, q: Quote) -> None:
    with db.write_txn(conn):
        _common_update(conn, q, "queued")
        db.emit_event(conn, "queued", query_id=q.qid, payload={
            "team": q.team, "value": q.value, "vpt": q.vpt, "reason": "budget exhausted",
        })


def _snapshot(conn, res: TickResult) -> None:
    """Persist a market snapshot for the burn-down / bill-over-time charts."""
    totals = conn.execute(
        "SELECT "
        " SUM(status='serviced') AS serviced,"
        " SUM(status='deduped')  AS deduped,"
        " SUM(status='rejected') AS rejected,"
        " SUM(status='queued')   AS queued FROM queries"
    ).fetchone()
    bill_row = conn.execute("SELECT COALESCE(SUM(total),0) AS bill FROM ledger").fetchone()
    with db.write_txn(conn):
        conn.execute(
            "INSERT INTO market_snapshots (serviced, deduped, starved, queued, "
            "total_bill, total_saved, created_at) VALUES (?,?,?,?,?,?,?)",
            (totals["serviced"] or 0, totals["deduped"] or 0, totals["rejected"] or 0,
             totals["queued"] or 0, round(bill_row["bill"], 6),
             round(res.saved_dedup + res.saved_routing, 6), db.now()),
        )


if __name__ == "__main__":
    # Tiny self-contained smoke run.
    os.environ.setdefault("BURSAR_DB", "/tmp/bursar_dispatch_demo/bursar.db")
    import shutil, pathlib
    shutil.rmtree(pathlib.Path(os.environ["BURSAR_DB"]).parent, ignore_errors=True)
    conn = db.connect()
    db.upsert_budget(conn, "research", 5.0)
    samples = [
        ("research", "We have a production outage affecting customer checkout — root cause?"),
        ("research", "production outage hitting customer checkout, what's the root cause"),  # dup
        ("research", "What's a good recipe for banana bread?"),                              # reject
        ("research", "Summarize this quarter's revenue forecast for the board."),
    ]
    for team, p in samples:
        ingest(conn, p, team)
    r = run_tick(conn)
    print(json.dumps(r.as_dict(), indent=2))
    for row in db.list_queries(conn):
        print(f"  {row['status']:9s} v={row['value']} {row['chosen_model']}  {row['prompt'][:45]}")
    conn.close()
