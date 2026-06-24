"""Bursar Trading-Floor dashboard plugin — backend API routes.

Mounted at ``/api/plugins/bursar/`` by the dashboard plugin loader
(``hermes_cli.web_server._mount_plugin_api_routes``).

This is the **read side** of the exchange. The engine (the Bursar skill in
``optional-skills/finance/bursar/scripts``) is the only writer: the dispatcher
and the cron settlement tick write ``queries``, ``ledger``, ``budgets``,
``events`` and ``market_snapshots`` to a WAL-mode SQLite DB. This plugin only
reads — WAL lets it tail the ``events`` table over a WebSocket while the
dispatcher writes, so the dashboard can never make the engine block or drift.

B0 ships the skeleton: a ``GET /`` health probe and the ``/events`` WebSocket
that streams new event rows as the dispatcher appends them. The REST
read-models that feed each panel (``/market``, ``/ledger``, ``/budgets``,
``/snapshots``, ``/stats``, ``/routing``) land in B1.

Engine import
-------------
The engine lives outside the plugin tree (it is a *skill*, shipped separately
from this *plugin*), so we add its ``scripts`` dir to ``sys.path`` and import
``bursar_db`` by name. ``bursar_db`` is stdlib-only, so this is a cheap,
side-effect-free import. ``BURSAR_SCRIPTS`` overrides the location for tests.

Security
--------
HTTP plugin routes sit behind the dashboard's session-token auth middleware
(``web_server.auth_middleware``) like every other ``/api/plugins/...`` route.
The ``/events`` WebSocket can't carry an ``Authorization`` header on the
upgrade, so it authorizes via the dashboard's canonical WS gate
(``web_server._ws_auth_ok``) — the credential rides in the query string
(loopback token / gated single-use ticket / server-internal credential),
exactly as the kanban live-events WS does.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import threading
import time
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, WebSocket, WebSocketDisconnect, status as http_status
from pydantic import BaseModel

log = logging.getLogger(__name__)

router = APIRouter()


# ---------------------------------------------------------------------------
# Engine import — put the Bursar skill's scripts dir on sys.path and import
# bursar_db by name. The skill is a sibling of the plugin tree:
#
#   <repo>/plugins/bursar/dashboard/plugin_api.py      <- this file
#   <repo>/optional-skills/finance/bursar/scripts/     <- the engine
#
# parents[3] of this file is <repo> (dashboard -> bursar -> plugins -> repo).
# ---------------------------------------------------------------------------

def _engine_scripts_dir() -> Path:
    env = os.environ.get("BURSAR_SCRIPTS")
    if env:
        return Path(env).expanduser()
    here = Path(__file__).resolve()
    # Self-contained install: the installer drops the engine alongside the
    # plugin at ~/.hermes/plugins/bursar/engine (sibling of this dashboard/
    # dir), so a copied-out plugin finds its engine with no BURSAR_SCRIPTS set.
    colocated = here.parents[1] / "engine"
    if colocated.is_dir():
        return colocated
    repo = here.parents[3]
    return repo / "optional-skills" / "finance" / "bursar" / "scripts"


def _import_engine():
    """Import and return ``(bursar_db, bursar_pricing)``, or ``(None, None)``
    if the engine can't be located. Import failure must never take down the
    dashboard — the tab just reports ``engine: false`` from the health probe.

    ``bursar_pricing`` is imported so ``/stats`` and ``/routing`` reuse the
    engine's *own* cost functions (``cost_for_model`` / ``predict_tokens`` /
    ``MODEL_CATALOG``) rather than re-deriving the economics — that's how the
    read-models reconcile exactly with the dispatcher's tick summary and with
    ``bench.py``'s headline numbers."""
    scripts = _engine_scripts_dir()
    if scripts.is_dir():
        sp = str(scripts)
        if sp not in sys.path:
            sys.path.insert(0, sp)
    try:
        import bursar_db  # type: ignore
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("Bursar plugin: engine import failed (%s): %s", _engine_scripts_dir(), exc)
        return None, None
    try:
        import bursar_pricing  # type: ignore
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("Bursar plugin: pricing import failed: %s", exc)
        bursar_pricing = None
    return bursar_db, bursar_pricing


# Resolve once at import time. ``None`` is tolerated end-to-end so a
# misplaced engine degrades to a clear health signal rather than a 500 wall.
db, pricing = _import_engine()


# ---------------------------------------------------------------------------
# Auth helper — WebSocket only (HTTP routes ride the dashboard's session-token
# middleware). Copied from the kanban plugin so this can never drift from core
# auth: delegate to the canonical gate, accept in test contexts where the
# dashboard web_server module isn't importable.
# ---------------------------------------------------------------------------

def _ws_upgrade_authorized(ws: "WebSocket") -> bool:
    try:
        from hermes_cli import web_server as _ws
    except Exception:
        # No dashboard context (bare-FastAPI test harness). Accept so the
        # tail loop stays testable; in production web_server always imports
        # cleanly because it is the caller.
        return True
    return bool(_ws._ws_auth_ok(ws))


# ---------------------------------------------------------------------------
# GET /  — health probe. Mounted at /api/plugins/bursar/ .
# ---------------------------------------------------------------------------

@router.get("/")
def health():
    """Cheap liveness + engine-reachability probe.

    Returns 200 always (so the tab loads even before any traffic), with
    ``engine`` / ``db_reachable`` flags and the current max event id so a
    fresh dashboard can seed its WS cursor without a separate call.
    """
    info = {
        "ok": True,
        "service": "bursar",
        "engine": db is not None,
        "scripts_dir": str(_engine_scripts_dir()),
        "db_reachable": False,
        "db_path": None,
        "latest_event_id": 0,
    }
    if db is not None:
        try:
            conn = db.connect()
            try:
                row = conn.execute("SELECT COALESCE(MAX(id), 0) AS m FROM events").fetchone()
                info["latest_event_id"] = int(row["m"] if row else 0)
                info["db_path"] = str(db.db_path())
                info["db_reachable"] = True
            finally:
                conn.close()
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("Bursar health: DB probe failed: %s", exc)
            info["error"] = str(exc)
    return info


# ---------------------------------------------------------------------------
# REST read-models (B1) — every panel's data, read-only over the WAL DB.
#
# These never write. They reuse the engine's own modules (bursar_db for rows,
# bursar_pricing for the cost functions) so the numbers reconcile exactly with
# the dispatcher's TickResult and bench.py — no parallel re-derivation that
# could drift.
# ---------------------------------------------------------------------------

def _require_engine():
    """503 if the engine couldn't be imported, so REST callers get a clean
    'service unavailable' instead of an AttributeError 500."""
    if db is None:
        raise HTTPException(status_code=503, detail="bursar engine unavailable")


def _conn():
    """Open a short-lived read connection (WAL; honors BURSAR_DB)."""
    return db.connect()


def _premium_model():
    """The catalog's most expensive model — the 'everyone defaults to the best
    model' baseline the router exists to beat. Mirrors bench.py / estimate()."""
    return max(pricing.MODEL_CATALOG, key=lambda m: m.price_per_mtok)


@router.get("/market")
def market(
    status: Optional[str] = Query(None, description="Filter to one lane: pending|serviced|deduped|rejected|queued"),
    source: Optional[str] = Query(None, description="Filter by traffic source: demo (firehose) | live (real gate traffic)"),
    limit: int = Query(500, ge=1, le=5000),
):
    """The order book: recent queries with their decision economics
    (value, tier, value-per-token, routed model, status). Newest first.

    This is the live trading-floor feed the UI ranks by ``vpt``. Lane counts
    come straight from the ``queries`` table so they match the dispatcher.
    ``source`` scopes the feed + counts to one traffic mode (the dashboard's
    Simulated vs Live split).
    """
    _require_engine()
    conn = _conn()
    try:
        rows = db.list_queries(conn, status=status, source=source, limit=limit)
        count_where, count_params = (" WHERE source = ?", (source,)) if source else ("", ())
        counts = {
            r["status"]: r["n"]
            for r in conn.execute(
                f"SELECT status, COUNT(*) AS n FROM queries{count_where} GROUP BY status",
                count_params,
            ).fetchall()
        }
        out = [
            {
                "id": r["id"],
                "team": r["team"],
                "source": r["source"],
                "prompt": (r["prompt"] or "")[:200],
                "value": r["value"],
                "tier": r["tier"],
                "vpt": r["vpt"],
                "est_tokens": r["est_tokens"],
                "est_cost": r["est_cost"],
                "fee": r["fee"],
                "model": r["chosen_model"],
                "status": r["status"],
                "dedup_of": r["dedup_of"],
                "rationale": r["rationale"],
                # T4 — temporal router: the per-query class and (when this row
                # reused a prior answer) the reuse mode, so the floor can badge
                # "historical comparison" as a distinct outcome.
                "temporal_class": r["temporal_class"] if "temporal_class" in r.keys() else None,
                "reuse_mode": r["reuse_mode"] if "reuse_mode" in r.keys() else None,
                "created_at": r["created_at"],
                "decided_at": r["decided_at"],
            }
            for r in rows
        ]
        return {"queries": out, "counts": counts, "count": len(out)}
    finally:
        conn.close()


@router.get("/ledger")
def ledger(
    team: Optional[str] = Query(None),
    source: Optional[str] = Query(None, description="Filter by traffic source: demo | live"),
    limit: int = Query(500, ge=1, le=5000),
):
    """The metered settlement: one settlement row per serviced query, with the
    Stripe meter-event id (``None`` when recorded local-only on a Stripe
    hiccup). ``summary.total`` is ``SUM(total)`` — the same value the
    dispatcher reports as ``bill`` and that ``/stats`` reconciles against.
    ``source`` scopes the bill to one traffic mode."""
    _require_engine()
    conn = _conn()
    try:
        rows = db.list_ledger(conn, team=team, source=source, limit=limit)
        clauses, params = [], []
        if team:
            clauses.append("team = ?"); params.append(team)
        if source:
            clauses.append("source = ?"); params.append(source)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        agg = conn.execute(
            "SELECT COUNT(*) AS n, COALESCE(SUM(total),0) AS total, "
            "COALESCE(SUM(token_cost),0) AS token_cost, COALESCE(SUM(fee),0) AS fee, "
            # `settled` is a 0/1 flag — SUM(settled) is the COUNT of settled rows
            # (used for local_only); the StatCard wants the settled DOLLARS, so
            # sum `total` over rows that actually metered to Stripe.
            "COALESCE(SUM(settled),0) AS settled_rows, "
            "COALESCE(SUM(CASE WHEN settled=1 THEN total ELSE 0 END),0) AS settled_total "
            "FROM ledger" + where,
            tuple(params),
        ).fetchone()
        return {
            "ledger": rows,
            "summary": {
                "rows": int(agg["n"]),
                "total": round(agg["total"], 6),
                "token_cost": round(agg["token_cost"], 6),
                "fee": round(agg["fee"], 6),
                "settled": round(agg["settled_total"], 6),
                "local_only": int(agg["n"]) - int(agg["settled_rows"]),
            },
        }
    finally:
        conn.close()


@router.get("/budgets")
def budgets():
    """Per-team envelopes: cap, spent, and remaining headroom. ``spent`` is
    maintained by the dispatcher's hard pre-execution gate, so ``remaining``
    can never go negative — the whole point versus post-hoc alerts."""
    _require_engine()
    conn = _conn()
    try:
        out = []
        for b in db.list_budgets(conn):
            cap = b["cap"] or 0.0
            spent = b["spent"] or 0.0
            out.append({
                "team": b["team"],
                "period": b["period"],
                "cap": round(cap, 6),
                "spent": round(spent, 6),
                "remaining": round(max(0.0, cap - spent), 6),
                "pct_used": round(spent / cap, 4) if cap > 0 else None,
                "reset_at": b["reset_at"],
                "updated_at": b["updated_at"],
            })
        return {"budgets": out, "count": len(out)}
    finally:
        conn.close()


@router.get("/snapshots")
def snapshots(limit: int = Query(500, ge=1, le=5000)):
    """The market time series (one row per dispatch tick): cumulative lane
    counts, running bill, and savings — chronological, for the burn-down and
    bill-over-time charts. ``starved`` is the rejected-lane count."""
    _require_engine()
    conn = _conn()
    try:
        return {"snapshots": db.list_snapshots(conn, limit=limit)}
    finally:
        conn.close()


@router.get("/stats")
def stats(source: Optional[str] = Query(None, description="Filter by traffic source: demo | live")):
    """Headline economics — model-agnostic, real dollars:

    * ``cost``       — ``SUM(ledger.token_cost)`` — real model spend (total cost).
    * ``saved``      — total dollars AVOIDED = dedup + gating + reuse, each skipped
                       call priced at its OWN model (no catalog-max baseline).
    * ``reduction``  — ``saved / (cost + saved)`` — share of would-be spend avoided.
    * ``fee``        — ``SUM(ledger.fee)`` — Bursar revenue, reported separately
                       (never folded into cost).
    * breakdown: ``saved_dedup`` (deduped est_cost), ``saved_gate`` (rejected
      est_cost), ``saved_reuse`` (live ``dedup_augment.reused_saved``),
      ``saved_routing`` (drill-down).
    * ``naive_all_premium`` — SECONDARY modeled ceiling (every call at the single
      priciest model). Drill-down only, NOT the headline.

    The computation is identical for every ``source`` — the Simulated dashboard
    uses the same real-numeric methodology as Live; it just runs over firehose
    rows. ``source`` scopes every figure so the two never bleed together.
    """
    _require_engine()
    conn = _conn()
    # A reusable " AND source = ?" / " WHERE source = ?" pair so every sub-query
    # is scoped identically — Simulated and Live never bleed into each other.
    src_and = " AND source = ?" if source else ""
    src_where = " WHERE source = ?" if source else ""
    sp = (source,) if source else ()
    try:
        lanes = {
            r["status"]: r["n"]
            for r in conn.execute(
                f"SELECT status, COUNT(*) AS n FROM queries{src_where} GROUP BY status", sp
            ).fetchall()
        }
        bill = conn.execute(
            f"SELECT COALESCE(SUM(total),0) AS s FROM ledger{src_where}", sp
        ).fetchone()["s"]
        # Model-agnostic TOTAL COST = real model spend only. The fee is Bursar's
        # revenue (reported separately), never folded into "cost".
        cost = conn.execute(
            f"SELECT COALESCE(SUM(token_cost),0) AS s FROM ledger{src_where}", sp
        ).fetchone()["s"]
        fee = conn.execute(
            f"SELECT COALESCE(SUM(fee),0) AS s FROM ledger{src_where}", sp
        ).fetchone()["s"]

        # Savings are counted as DOLLARS ACTUALLY AVOIDED — each skipped call at
        # its OWN model's price (est_cost), summed. No single model is privileged,
        # so the figure is model-agnostic: total cost vs total saved, in real $.
        saved_dedup = conn.execute(
            "SELECT COALESCE(SUM(COALESCE(est_cost,0)),0) AS s "
            f"FROM queries WHERE status='deduped'{src_and}", sp
        ).fetchone()["s"]
        # Gating: worthless calls the value-gate rejected, at their own est_cost.
        saved_gate = conn.execute(
            "SELECT COALESCE(SUM(COALESCE(est_cost,0)),0) AS s "
            f"FROM queries WHERE status='rejected'{src_and}", sp
        ).fetchone()["s"]

        # S6 — LIVE dedup-augment savings. The live gate doesn't write
        # status='deduped' rows; it runs ONE cheap real call and emits a
        # 'dedup_augment' event carrying the MEASURED cost of the rediscovery it
        # skipped (``reused_saved``, summed from the original turn's real ledger).
        # That saving is invisible to saved_dedup AND to naive−bill (the augment
        # row only carries the cheap call's tokens), so the live "saved" figure
        # under-reports. Surface it from the events stream. The firehose NEVER
        # augments, so every dedup_augment event is live: include it only when the
        # scope is live or unscoped — source='demo' genuinely has no augment events
        # (same methodology as live, it just has nothing to sum).
        saved_reuse = 0.0
        if source in (None, "live"):
            for r in conn.execute(
                "SELECT payload FROM events WHERE kind='dedup_augment'"
            ).fetchall():
                try:
                    p = json.loads(r["payload"]) if r["payload"] else {}
                    saved_reuse += float(p.get("reused_saved") or 0.0)
                except Exception:
                    continue
        saved_reuse = round(saved_reuse, 6)

        # Routing + naive baseline reuse the engine's price functions over the
        # stored est_tokens/tier so they match estimate()'s naive_cost exactly.
        saved_routing = 0.0
        naive_all_premium = 0.0
        if pricing is not None:
            prem = _premium_model()
            for r in conn.execute(
                f"SELECT prompt, tier, est_tokens, est_cost, status FROM queries{src_where}", sp
            ).fetchall():
                tier = r["tier"] or "standard"
                toks = r["est_tokens"]
                if toks is None:  # undecided (pending): fall back to a prediction
                    toks = pricing.predict_tokens(r["prompt"] or "", tier)
                naive_c = pricing.cost_for_model(toks, prem)
                naive_all_premium += naive_c
                if r["status"] == "serviced":
                    saved_routing += max(0.0, naive_c - (r["est_cost"] or 0.0))

        bill = round(bill, 6)
        cost = round(cost, 6)
        fee = round(fee, 6)
        saved_dedup = round(saved_dedup, 6)
        saved_gate = round(saved_gate, 6)
        naive_all_premium = round(naive_all_premium, 6)
        # HEADLINE (model-agnostic): total saved = dollars actually avoided across
        # every lever, each at its own model's price. No catalog-max baseline.
        saved = round(saved_dedup + saved_gate + saved_reuse, 6)
        # reduction = share of would-be model spend that Bursar avoided.
        would_be = cost + saved
        reduction = round(saved / would_be, 4) if would_be > 0 else None
        decided = sum(lanes.get(k, 0) for k in ("serviced", "deduped", "rejected", "queued"))
        return {
            "lanes": lanes,
            "decided": decided,
            "pending": lanes.get("pending", 0),
            # --- model-agnostic headline (real $) ---
            "cost": cost,                 # real model spend (total cost)
            "saved": saved,               # total dollars avoided
            "reduction": reduction,       # saved / (cost + saved)
            "fee": fee,                   # Bursar revenue, reported separately
            # --- savings breakdown (drill-down) ---
            "saved_dedup": saved_dedup,
            "saved_gate": saved_gate,
            "saved_reuse": saved_reuse,
            "saved_routing": round(saved_routing, 6),
            # --- secondary "modeled ceiling" (NOT the headline): every call priced
            #     at the single most expensive model. Kept for drill-down only. ---
            "naive_all_premium": naive_all_premium,
            "premium_model": _premium_model().name if pricing is not None else None,
            # backward-compat: bill == spend + fee (what /summary reconciles against)
            "bill": bill,
        }
    finally:
        conn.close()


@router.get("/stripe/billing")
def stripe_billing():
    """S3 — the closed money loop: each team's UPCOMING Stripe invoice total.

    Read-only and best-effort. Previews each team's metered subscription invoice
    (``bursar_stripe.billing_summary`` → ``Invoice.create_preview``) so the floor
    can point at a real $ amount that reconciles to the metered usage. Returns
    ``{active, teams, total_usd}``; ``active=False`` with an empty list when no
    test key / no subscriptions yet (never raises into the dashboard)."""
    _, _, stripe = _control_deps()
    if stripe is None or not stripe.is_active():
        return {"active": False, "teams": [], "total_usd": 0.0}
    try:
        rows = stripe.billing_summary()
    except Exception as exc:  # pragma: no cover - defensive
        log.debug("bursar dashboard: billing_summary failed: %s", exc)
        return {"active": False, "teams": [], "total_usd": 0.0}
    total = round(sum(r.get("upcoming_total_usd", 0.0) for r in rows if "error" not in r), 4)
    return {"active": True, "teams": rows, "total_usd": total}


@router.get("/routing")
def routing(source: Optional[str] = Query(None, description="Filter by traffic source: demo | live")):
    """Spend + volume by model — proves the router's work: how much landed on
    cheap models vs the premium ceiling, and the dollars that bought. Ordered
    by total spend. ``price_per_mtok`` is the catalog list price for context.
    ``source`` scopes to one traffic mode."""
    _require_engine()
    conn = _conn()
    src_where = " WHERE source = ?" if source else ""
    sp = (source,) if source else ()
    try:
        catalog = (
            {m.name: {"price_per_mtok": m.price_per_mtok, "quality": m.quality}
             for m in pricing.MODEL_CATALOG}
            if pricing is not None else {}
        )
        rows = conn.execute(
            "SELECT model, COUNT(*) AS queries, COALESCE(SUM(tokens),0) AS tokens, "
            "COALESCE(SUM(token_cost),0) AS token_cost, COALESCE(SUM(fee),0) AS fee, "
            f"COALESCE(SUM(total),0) AS total FROM ledger{src_where} GROUP BY model "
            "ORDER BY total DESC", sp
        ).fetchall()
        out = []
        for r in rows:
            meta = catalog.get(r["model"], {})
            out.append({
                "model": r["model"],
                "queries": int(r["queries"]),
                "tokens": int(r["tokens"]),
                "token_cost": round(r["token_cost"], 6),
                "fee": round(r["fee"], 6),
                "total": round(r["total"], 6),
                "price_per_mtok": meta.get("price_per_mtok"),
                "quality": meta.get("quality"),
            })
        return {"routing": out, "models": out, "count": len(out)}
    finally:
        conn.close()


# Blend weights for a single $/Mtok headline figure: agentic completions skew
# output-heavy, so weight output 3:1 over input. Matches bursar_pricing.Model.
_BLEND_IN, _BLEND_OUT = 0.25, 0.75


def _load_hermes_pricing():
    """Hermes's own official, sourced pricing snapshot ({(provider, model):
    PricingEntry}) — or None if it can't be imported. We read it directly so
    the catalog is the real Hermes menu (with source_url + pricing_version),
    not a table we hand-maintain in the plugin."""
    try:
        from agent.usage_pricing import _OFFICIAL_DOCS_PRICING  # type: ignore
        return _OFFICIAL_DOCS_PRICING
    except Exception:
        try:
            repo_root = Path(__file__).resolve().parents[3]
            if str(repo_root) not in sys.path:
                sys.path.insert(0, str(repo_root))
            from agent.usage_pricing import _OFFICIAL_DOCS_PRICING  # type: ignore
            return _OFFICIAL_DOCS_PRICING
        except Exception:
            return None


def _is_dated(model: str) -> bool:
    """True for a dated snapshot id like ``claude-opus-4-20250514``."""
    tail = model.rsplit("-", 1)[-1]
    return len(tail) == 8 and tail.isdigit()


def _hermes_catalog_rows(in_use: set):
    """Build the display catalog from Hermes's snapshot: one row per distinct
    (provider, input, output) price point, collapsing dated/version duplicates.
    The representative id prefers an in-use routing model, then an undated id,
    then the shortest — so Bursar's routed models stay visible + flaggable."""
    snap = _load_hermes_pricing()
    if not snap:
        return None
    groups: dict = {}
    for (provider, model), entry in snap.items():
        inp, out = entry.input_cost_per_million, entry.output_cost_per_million
        if inp is None or out is None:
            continue
        inp, out = float(inp), float(out)
        key = (provider, inp, out)
        groups.setdefault(key, []).append({
            "provider": provider,
            "model": model,
            "input": round(inp, 4),
            "output": round(out, 4),
            "blended": round(inp * _BLEND_IN + out * _BLEND_OUT, 4),
            "source": entry.pricing_version,
        })
    rows = []
    for members in groups.values():
        rep = sorted(members, key=lambda m: (m["model"] not in in_use, _is_dated(m["model"]), len(m["model"])))[0]
        rep["in_use"] = rep["model"] in in_use
        rows.append(rep)
    rows.sort(key=lambda r: r["blended"])
    return rows


# The real LIVE executors — what ACTUALLY ran the traffic (via openrouter), not
# in Hermes's pricing snapshot, so we add them explicitly. Nemotron runs on the
# free tier ($0); DeepSeek v4 at its real measured blended cost (USD / 1M tok,
# from the live ledger). These are the execution substrate behind the market-rate
# routing tiers — shown in-use for live traffic so the menu reflects what ran.
_LIVE_EXECUTORS = [
    {"provider": "nvidia",   "model": "nemotron-3-super-120b", "input": 0.0,  "output": 0.0,  "blended": 0.0,  "source": "openrouter · free"},
    {"provider": "deepseek", "model": "deepseek-v4-pro",       "input": 0.05, "output": 0.13, "blended": 0.11, "source": "openrouter"},
    {"provider": "deepseek", "model": "deepseek-v4-flash",     "input": 0.03, "output": 0.07, "blended": 0.06, "source": "openrouter"},
]


@router.get("/catalog")
def catalog(source: Optional[str] = Query(None, description="Filter by traffic source: demo | live")):
    """The model price menu Bursar prices against. Tier prices come straight from
    Hermes's official pricing snapshot (``agent.usage_pricing``) — input + output
    USD / 1M tokens with the provider's ``pricing_version``. ``in_use`` reflects
    the models that actually served work in ``source`` (the current mode), plus
    the real free-Nemotron / DeepSeek v4 executors behind live traffic — so the
    menu, its true spread, and the engaged-model count are all mode-accurate."""
    _require_engine()
    # Models that ACTUALLY served work in this mode — replaces the old hardcode
    # that flagged every routing tier in-use regardless of mode or real usage.
    conn = _conn()
    try:
        src_where = " WHERE source = ?" if source else ""
        sp = (source,) if source else ()
        in_use = {
            r["model"] for r in conn.execute(
                f"SELECT DISTINCT model FROM ledger{src_where}", sp).fetchall()
            if r["model"]
        }
    finally:
        conn.close()
    rows = _hermes_catalog_rows(in_use)
    sourced = True
    if rows is None:  # snapshot unavailable — fall back to the engine's own tiers
        sourced = False
        rows = [
            {"provider": "bursar", "model": m.name, "input": None, "output": None,
             "blended": m.price_per_mtok, "source": None, "in_use": m.name in in_use}
            for m in sorted(pricing.MODEL_CATALOG, key=lambda m: m.price_per_mtok)
        ] if pricing is not None else []
    # Add the real live executors (free Nemotron + DeepSeek v4) at the rate we
    # actually paid on openrouter — overriding any official-snapshot row for the
    # same id (the snapshot lists a different list price; we show what ran).
    exec_in_use = source == "live"
    exec_ids = {ex["model"] for ex in _LIVE_EXECUTORS}
    rows = [r for r in rows if r["model"] not in exec_ids]
    for ex in _LIVE_EXECUTORS:
        rows.append({**ex, "in_use": exec_in_use})
    rows.sort(key=lambda r: r["blended"] if r["blended"] is not None else 0.0)
    blended = [r["blended"] for r in rows if r["blended"]]
    spread = (max(blended) / min(blended)) if len(blended) >= 2 else None
    return {"catalog": rows, "count": len(rows), "spread": spread, "sourced": sourced}


# ---------------------------------------------------------------------------
# Demo control surface (B2) — POST endpoints that drive the live trading floor
# from the UI: run the firehose, force a tick, reset, toggle Bursar on/off, and
# inject a rogue-agent flood. These WRITE (via the engine), unlike B1.
#
# The heavier engine pieces (dispatcher pulls numpy; firehose pulls the
# generator + Stripe glue) are imported lazily here so the health/read path and
# plugin discovery stay light and can't be broken by an optional dep.
#
# Control ticks force heuristic scoring (BURSAR_USE_NEMOTRON=0): a live floor at
# several queries/sec can't wait 2–10 s per Nemotron valuation. The Nemotron
# escalation path (A4) stays available for its own showcase; it just isn't the
# bulk live-demo path — exactly the tiered-escalation argument the ecosystem
# notes make.
# ---------------------------------------------------------------------------

def _control_deps():
    """Lazily import ``(dispatcher, firehose, stripe_or_None)``."""
    import bursar_dispatcher as disp  # type: ignore
    import firehose as fh  # type: ignore
    try:
        import bursar_stripe as stripe  # type: ignore
    except Exception:  # pragma: no cover - defensive
        stripe = None
    return disp, fh, stripe


class _Control:
    """Process-wide demo state: the firehose thread + the on/off mode. The mode
    is read live by each tick, so toggling it mid-stream flips the floor between
    the gated exchange and the naive 'before' world without a restart."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.thread: Optional[threading.Thread] = None
        self.stop_evt: Optional[threading.Event] = None
        self.mode = "bursar_on"  # | "bursar_off"
        self.cfg = {"rate": 5.0, "dup_rate": 0.31, "burst": False, "tick": 1.0}

    @property
    def running(self) -> bool:
        return self.thread is not None and self.thread.is_alive()


control = _Control()


def _meter_if_active(stripe):
    """A real Stripe meter when a test key is configured, else None. ``make_meter``
    refuses live keys and no-ops on any failure, so this is always safe."""
    try:
        return stripe.make_meter() if (stripe is not None and stripe.is_active()) else None
    except Exception:  # pragma: no cover - defensive
        return None


def _firehose_loop(stop_evt: threading.Event, cfg: dict) -> None:
    """Background generator: emit ~``rate`` queries/sec, dispatch every ``tick``
    seconds, until ``stop_evt`` is set. Owns its own DB connection (sqlite
    handles aren't shareable across threads). Reads ``control.mode`` live."""
    os.environ["BURSAR_USE_NEMOTRON"] = "0"  # heuristic — keep the floor snappy
    disp, fhmod, stripe = _control_deps()
    conn = db.connect()
    try:
        meter = _meter_if_active(stripe)
        fh = fhmod.Firehose(conn, fhmod.FirehoseConfig(
            dup_rate=cfg["dup_rate"], burst=cfg["burst"]))
        interval = 1.0 / max(cfg["rate"], 0.1)
        last = 0.0
        while not stop_evt.is_set():
            fh.emit(1)
            t = time.time()
            if t - last >= cfg["tick"]:
                disp.run_tick(conn, meter=meter, bypass=(control.mode == "bursar_off"))
                last = t
            stop_evt.wait(interval)
    finally:
        conn.close()


class FirehoseBody(BaseModel):
    action: str  # "start" | "stop"
    rate: float = 5.0
    dup_rate: float = 0.31
    burst: bool = False
    tick: float = 1.0


@router.post("/control/firehose")
def control_firehose(body: FirehoseBody):
    """Start/stop the background synthetic firehose. While running it streams
    queries and ticks, so ``/events`` lights up and the lanes fill live."""
    _require_engine()
    action = (body.action or "").lower()
    with control.lock:
        if action == "start":
            if control.running:
                return {"ok": True, "running": True, "note": "already running",
                        "cfg": control.cfg, "mode": control.mode}
            cfg = {"rate": float(body.rate), "dup_rate": float(body.dup_rate),
                   "burst": bool(body.burst), "tick": float(body.tick)}
            control.cfg = cfg
            control.stop_evt = threading.Event()
            control.thread = threading.Thread(
                target=_firehose_loop, args=(control.stop_evt, cfg),
                daemon=True, name="bursar-firehose")
            control.thread.start()
            return {"ok": True, "running": True, "cfg": cfg, "mode": control.mode}
        if action == "stop":
            if control.stop_evt is not None:
                control.stop_evt.set()
            control.thread = None
            return {"ok": True, "running": False}
        raise HTTPException(status_code=400, detail="action must be 'start' or 'stop'")


@router.post("/control/tick")
def control_tick():
    """Force one dispatch tick now (clears all pending+queued). Returns the
    TickResult so the UI can show the decision counts + bill for this tick."""
    _require_engine()
    os.environ["BURSAR_USE_NEMOTRON"] = "0"
    disp, _fh, stripe = _control_deps()
    conn = _conn()
    try:
        res = disp.run_tick(conn, meter=_meter_if_active(stripe),
                            bypass=(control.mode == "bursar_off"))
        return {"ok": True, "mode": control.mode, "tick": res.as_dict()}
    finally:
        conn.close()


@router.post("/control/reset")
def control_reset():
    """Wipe the board (queries/ledger/events/snapshots), reset budget spend to
    zero, and return to bursar_on. Budget *envelopes* are kept (spent zeroed)."""
    _require_engine()
    with control.lock:
        if control.stop_evt is not None:
            control.stop_evt.set()
        control.thread = None
        control.mode = "bursar_on"
    _disp, fhmod, _stripe = _control_deps()
    conn = _conn()
    try:
        with db.write_txn(conn):
            for tbl in ("ledger", "events", "market_snapshots", "queries"):
                conn.execute(f"DELETE FROM {tbl}")
        fhmod._seed_budgets(conn)  # ensure the standard teams exist
        for b in db.list_budgets(conn):  # zero spend on every envelope
            db.upsert_budget(conn, b["team"], b["cap"], period=b["period"], reset_spent=True)
        db.emit_event(conn, "reset", payload={"by": "dashboard"})
        return {"ok": True, "mode": control.mode, "budgets": db.list_budgets(conn)}
    finally:
        conn.close()


class ModeBody(BaseModel):
    mode: str  # "bursar_on" | "bursar_off"


@router.post("/control/mode")
def control_mode(body: ModeBody):
    """Toggle the before/after: ``bursar_on`` = the gated exchange (dedup +
    value gate + budget cap + routing); ``bursar_off`` = everyone defaults to
    the premium model, nothing gated, the bill runs away."""
    m = (body.mode or "").lower()
    if m not in ("bursar_on", "bursar_off"):
        raise HTTPException(status_code=400, detail="mode must be 'bursar_on' or 'bursar_off'")
    with control.lock:
        control.mode = m
    if db is not None:
        conn = _conn()
        try:
            db.emit_event(conn, "mode", payload={"mode": m})
        finally:
            conn.close()
    return {"ok": True, "mode": m}


class RogueBody(BaseModel):
    count: int = 30
    team: str = "marketing"
    tick: bool = True


@router.post("/control/rogue")
def control_rogue(body: RogueBody):
    """Inject a scripted rogue-agent flood: many near-identical low-value asks
    from one team (a stuck loop). In bursar_on this produces a visible reject +
    dedup spike (waste prices itself out); in bursar_off it just inflates the
    bill. Optionally runs a tick so the spike shows immediately."""
    _require_engine()
    os.environ["BURSAR_USE_NEMOTRON"] = "0"
    disp, fhmod, stripe = _control_deps()
    n = max(1, min(int(body.count), 2000))
    conn = _conn()
    try:
        base = fhmod._compose(fhmod.ACTIONS_LOW)  # one low-value template
        # Mostly paraphrases of the same ask → dedup collapses, the rest reject
        # on value-below-cost. Either way: a sharp non-serviced spike.
        items = [(body.team, base if i % 3 == 0 else fhmod._paraphrase(base))
                 for i in range(n)]
        disp.ingest_many(conn, items, emit_events=False)
        res = None
        if body.tick:
            res = disp.run_tick(conn, meter=_meter_if_active(stripe),
                                bypass=(control.mode == "bursar_off"))
        db.emit_event(conn, "rogue", payload={"team": body.team, "count": n})
        return {"ok": True, "injected": n, "team": body.team,
                "mode": control.mode, "tick": res.as_dict() if res else None}
    finally:
        conn.close()


@router.get("/control/status")
def control_status():
    """Current control state for the UI's toggle/buttons."""
    return {"running": control.running, "mode": control.mode, "cfg": control.cfg}


# ---------------------------------------------------------------------------
# Live enforcement arm/disarm (the desktop titlebar toggle).
#
# This is DISTINCT from /control/mode above: that flips the synthetic firehose
# demo between gated/naive. THIS flips the *live* gate (``bursar_gate`` in the
# agent process) that prices/dedups/down-routes the user's real LLM calls. The
# agent runs in a separate process, so we route the flag through the shared
# control table; the gate reads it live per call (instant kill switch).
#
# When ``BURSAR_ENFORCE`` is set in the agent's environment it WINS over this
# flag (an explicit operator override), so we report ``env_locked`` to the UI
# so it can show the toggle as locked rather than silently ignored. We can only
# observe THIS process's env, so it's a best-effort hint.
# ---------------------------------------------------------------------------

_ENFORCE_TRUTHY = {"1", "true", "yes", "on"}


def _enforce_env_lock():
    """Return ``(locked, armed_by_env)`` if BURSAR_ENFORCE is explicitly set in
    this process's env, else ``(False, None)``. Best-effort — the agent process
    may have a different env, but in the desktop both inherit the same one."""
    raw = os.environ.get("BURSAR_ENFORCE")
    if raw is not None and raw.strip() != "":
        return True, raw.strip().lower() in _ENFORCE_TRUTHY
    return False, None


@router.get("/control/enforce")
def control_enforce_get():
    """Current live-enforcement state for the titlebar toggle. ``armed`` is the
    effective state the gate will use; ``env_locked`` true means an env var
    overrides the UI flag (toggle should render locked)."""
    _require_engine()
    locked, env_armed = _enforce_env_lock()
    conn = _conn()
    try:
        flag = (db.get_control(conn, "enforce", "0") or "").strip().lower()
    finally:
        conn.close()
    db_armed = flag in _ENFORCE_TRUTHY
    armed = env_armed if locked else db_armed
    return {"armed": armed, "db_armed": db_armed, "env_locked": locked}


class EnforceBody(BaseModel):
    armed: bool


@router.post("/control/enforce")
def control_enforce_set(body: EnforceBody):
    """Arm/disarm live enforcement from the UI. Writes ``control['enforce']``
    and emits an event so the floor reflects the change immediately. A no-op on
    the effective state when an env var is locking it, but we still persist the
    flag so it takes effect the moment the env override is removed."""
    _require_engine()
    locked, env_armed = _enforce_env_lock()
    conn = _conn()
    try:
        db.set_control(conn, "enforce", "1" if body.armed else "0")
        db.emit_event(conn, "enforce", payload={"armed": bool(body.armed), "source": "dashboard"})
        # connect() runs in deferred-transaction mode, so the standalone
        # emit_event INSERT needs an explicit commit or it's rolled back on
        # close (set_control already committed the flag itself).
        conn.commit()
    finally:
        conn.close()
    armed = env_armed if locked else bool(body.armed)
    return {"ok": True, "armed": armed, "db_armed": bool(body.armed), "env_locked": locked}


# ---------------------------------------------------------------------------
# WebSocket: /events?since=<event_id>
#
# Tails the append-only ``events`` table on a short poll. WAL mode lets these
# reads run alongside the dispatcher's IMMEDIATE write transactions. Same
# shape as the kanban live feed: {"events": [...], "cursor": <int>}.
# ---------------------------------------------------------------------------

# 300 ms poll: a fraction of a percent of CPU, no shared state to synchronize,
# and visually instant on the trading floor. Matches the kanban plugin.
_EVENT_POLL_SECONDS = 0.3


@router.websocket("/events")
async def stream_events(ws: WebSocket):
    # Authorize the upgrade via the dashboard's canonical WS gate so the right
    # credential is accepted in every mode (loopback token / gated single-use
    # ticket / server-internal credential). The browser SDK's buildWsUrl()
    # assembles it into the query string.
    if not _ws_upgrade_authorized(ws):
        await ws.close(code=http_status.WS_1008_POLICY_VIOLATION)
        return
    await ws.accept()

    if db is None:
        # Engine unreachable: tell the client once and close cleanly rather
        # than holding open a socket that can never produce events.
        await ws.send_json({"events": [], "cursor": 0, "error": "engine unavailable"})
        await ws.close(code=http_status.WS_1011_INTERNAL_ERROR)
        return

    try:
        since_raw = ws.query_params.get("since", "0")
        try:
            cursor = int(since_raw)
        except ValueError:
            cursor = 0

        def _fetch_new(cursor_val: int) -> tuple[int, list[dict]]:
            # Open a fresh connection per poll (cheap; honors BURSAR_DB) so we
            # never share a sqlite handle across the event loop's threads.
            conn = db.connect()
            try:
                return db.events_since(conn, cursor_val, limit=200)
            finally:
                conn.close()

        while True:
            cursor, events = await asyncio.to_thread(_fetch_new, cursor)
            if events:
                await ws.send_json({"events": events, "cursor": cursor})
            await asyncio.sleep(_EVENT_POLL_SECONDS)
    except WebSocketDisconnect:
        return
    except asyncio.CancelledError:
        # Normal shutdown: dashboard process exit cancels the WS task while it
        # sleeps in the poll loop. CancelledError is a BaseException, so the
        # bare Exception handler below won't catch it; quiet it here so
        # uvicorn doesn't log the cancellation as an app traceback.
        return
    except Exception as exc:  # defensive: never crash the dashboard worker
        log.warning("Bursar event stream error: %s", exc)
        try:
            await ws.close()
        except Exception:
            pass
