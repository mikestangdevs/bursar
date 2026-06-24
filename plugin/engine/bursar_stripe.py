"""Bursar — Stripe test-mode usage-based (metered) billing (feature A7).

Every serviced query is a billable event on the internal compute exchange.
This module turns each one into a Stripe **billing meter event** so the
ledger reconciles against a finance-grade settlement system — the "trading
fee + token cost" metered to the team that placed the order.

It exposes one thing the dispatcher cares about: a *meter callable* matching
``bursar_dispatcher.run_tick(conn, meter=…)``:

    meter(team, model, tokens, total) -> stripe_event_id | None

``make_meter()`` builds it. On a serviced query the dispatcher calls it; a
returned id is written to ``ledger.stripe_event_id`` with ``settled=1``. The
dispatcher already wraps the call in try/except, and ``make_meter`` itself
degrades to a no-op that returns ``None`` whenever Stripe is unavailable —
so a Stripe hiccup, a missing key, or a network blip can **never** sink the
demo. The tick still completes; the ledger still records locally (settled=0).

Safety
------
TEST MODE ONLY. ``make_meter`` reads ``STRIPE_SECRET_KEY`` (or the alias
``STRIPE_API_KEY``) and **refuses any key that is not** ``sk_test_…`` — a
live key would bill a real account. A live key is treated exactly like "no
key": loud one-time warning, no-op meter, engine runs local-only.

Setup is idempotent and cached to ``~/.hermes/bursar/stripe_state.json``:
one billing meter (``event_name`` = ``BURSAR_STRIPE_METER``, default
``bursar_query``) and one test customer per team.
"""

from __future__ import annotations

import json
import os
import sys
import secrets
import threading
from pathlib import Path
from typing import Callable, Optional

import bursar_db as db

# Serializes the state-mutating customer/subscription ensure + _save_state so the
# deferred (off-thread) live-gate meter sends don't race each other into duplicate
# Stripe customers or a torn state file. Cheap: warm calls just read the cache.
_STATE_LOCK = threading.Lock()

# A meter callable: (team, model, tokens, total_usd) -> stripe_event_id | None
Meter = Callable[[str, str, int, float], Optional[str]]

ENV_FILENAME = ".env"
DEFAULT_METER_EVENT = "bursar_query"
# We meter integer **micro-dollars** (total_usd * 1e6) so fractional cents
# survive without float drift; sum-aggregation in Stripe stays exact.
USD_TO_MICROS = 1_000_000

# --- S3: the money loop (Product + metered Price + Subscription -> invoice) ---
# A metered Price bills the *aggregated meter value* (our micro-dollars) at this
# rate. ``unit_amount_decimal`` is in CENTS per unit, so 0.0001 cents per
# micro-dollar means a query metered at $0.05 (= 50,000 micro-dollars) invoices
# for exactly 5 cents. Verified live against the test account (50,000 units ->
# $0.05 upcoming preview). This keeps the dollar Bursar computed as the dollar
# Stripe bills — no per-token rate guess needed (that's why S5 is unnecessary).
PRODUCT_NAME = "Bursar compute"
PRICE_UNIT_AMOUNT_DECIMAL = "0.0001"


# --------------------------------------------------------------------------
# .env loading (zero-dependency; does not override already-set env vars)
# --------------------------------------------------------------------------

def _skill_root() -> Path:
    # scripts/ -> bursar/   (the skill root holds .env next to README.md)
    return Path(__file__).resolve().parent.parent


def _candidate_env_paths() -> list[Path]:
    """``.env`` files to load, NEAREST-FIRST.

    The skill historically expected its own ``.env`` next to README.md, but in
    this tree the real Stripe keys live in the **repo-root** ``hermes-agent/.env``
    (the skill-root file does not exist). Rather than hardcode that, walk up from
    the skill root and collect every ``.env`` along the way, so the loader finds
    the keys wherever the repo is checked out or however it is bundled.

    We stop at the repository / home boundary (a directory holding ``.git`` is
    the last one searched; ``~`` is never crossed) so we never pull in a stray
    ``~/.env`` belonging to the user's shell. Nearest file wins because
    ``load_dotenv`` never overrides an already-set var, and we load in order.
    """
    out: list[Path] = []
    seen: set[Path] = set()
    home = Path.home()
    start = _skill_root()
    for d in [start, *start.parents]:
        p = d / ENV_FILENAME
        if p not in seen:
            seen.add(p)
            if p.exists():
                out.append(p)
        # A .git dir marks a repo root (here: hermes-agent/) — search it, then
        # stop. Also never climb past the user's home directory.
        if (d / ".git").exists() or d == home:
            break
    return out


def load_dotenv(path: Optional[Path] = None) -> None:
    """Populate os.environ from the bursar/repo ``.env`` file(s) if present.
    Existing environment variables win (so a real export overrides the file),
    and a nearer ``.env`` wins over a farther one. Pass ``path`` to load one
    specific file instead of the walk-up search."""
    paths = [path] if path is not None else _candidate_env_paths()
    for p in paths:
        if p is None or not p.exists():
            continue
        for raw in p.read_text().splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, val = line.split("=", 1)
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = val


# --------------------------------------------------------------------------
# Key resolution + guardrail
# --------------------------------------------------------------------------

def resolve_key() -> tuple[Optional[str], str]:
    """Return ``(usable_test_key | None, reason)``.

    ``reason`` is one of: ``ok``, ``missing``, ``live`` (refused),
    ``malformed``. Only an ``sk_test_…`` key is ever returned as usable.
    """
    key = (os.environ.get("STRIPE_SECRET_KEY")
           or os.environ.get("STRIPE_API_KEY") or "").strip()
    if not key or key in ("sk_test_replace_me",):
        return None, "missing"
    if key.startswith("sk_live_") or key.startswith("rk_live_"):
        return None, "live"
    if key.startswith("sk_test_") or key.startswith("rk_test_"):
        return key, "ok"
    return None, "malformed"


_WARNED = False


def _warn_once(msg: str) -> None:
    global _WARNED
    if not _WARNED:
        print(f"[bursar.stripe] {msg}", file=sys.stderr)
        _WARNED = True


# --------------------------------------------------------------------------
# Cached account state (meter id + per-team customer ids)
# --------------------------------------------------------------------------

def _state_path() -> Path:
    return db.hermes_home() / "bursar" / "stripe_state.json"


def _load_state() -> dict:
    p = _state_path()
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception:
            return {}
    return {}


def _save_state(state: dict) -> None:
    p = _state_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(state, indent=2))


# --------------------------------------------------------------------------
# Stripe object wiring
# --------------------------------------------------------------------------

def _ensure_meter(stripe, event_name: str, state: dict) -> Optional[str]:
    """Idempotently ensure a billing meter for ``event_name`` exists; return
    its id. Cached in state; verified against the API; created if absent."""
    cached = state.get("meter_id")
    if cached:
        try:
            m = stripe.billing.Meter.retrieve(cached)
            if getattr(m, "status", "active") == "active":
                return m.id
        except Exception:
            pass  # cache stale (account reset?) — fall through and re-resolve
    # Look for an existing active meter with this event name.
    try:
        for m in stripe.billing.Meter.list(status="active", limit=100).auto_paging_iter():
            if getattr(m, "event_name", None) == event_name:
                state["meter_id"] = m.id
                return m.id
    except Exception:
        pass
    # Create it.
    m = stripe.billing.Meter.create(
        display_name="Bursar query",
        event_name=event_name,
        default_aggregation={"formula": "sum"},
        customer_mapping={"type": "by_id", "event_payload_key": "stripe_customer_id"},
        value_settings={"event_payload_key": "value"},
    )
    state["meter_id"] = m.id
    return m.id


def _ensure_customer(stripe, team: str, state: dict) -> Optional[str]:
    """Idempotently ensure a test customer for ``team``; return its id."""
    customers = state.setdefault("customers", {})
    cached = customers.get(team)
    if cached:
        try:
            c = stripe.Customer.retrieve(cached)
            if not getattr(c, "deleted", False):
                return c.id
        except Exception:
            pass
    c = stripe.Customer.create(
        name=f"Bursar team: {team}",
        email=f"{team}@bursar.test",  # test-mode address; lets send_invoice work too
        metadata={"bursar_team": team},
    )
    customers[team] = c.id
    return c.id


# --------------------------------------------------------------------------
# S3 — the money loop: Product + metered Price + per-team Subscription so the
# meter events we already send roll into a real (draft/upcoming) invoice.
# Everything here is idempotent and cached in stripe_state.json next to the
# meter + customer ids. Each helper is best-effort: a failure provisioning the
# invoice loop must NEVER disable metering (the core) — make_meter() wraps these
# so a billing-setup hiccup just means "metered, no invoice yet".
# --------------------------------------------------------------------------

def _ensure_product(stripe, state: dict) -> Optional[str]:
    """Idempotently ensure the ``Bursar compute`` product; return its id."""
    cached = state.get("product_id")
    if cached:
        try:
            p = stripe.Product.retrieve(cached)
            if getattr(p, "active", True):
                return p.id
        except Exception:
            pass  # cache stale — recreate below
    p = stripe.Product.create(name=PRODUCT_NAME)
    state["product_id"] = p.id
    return p.id


def _ensure_price(stripe, meter_id: str, product_id: str, state: dict) -> Optional[str]:
    """Idempotently ensure a metered Price tied to our meter; return its id. The
    cached price is reused only while it still points at the SAME meter + product
    (so swapping the meter rebuilds the price instead of mis-billing)."""
    cached = state.get("price_id")
    if cached:
        try:
            pr = stripe.Price.retrieve(cached)
            rec = dict(getattr(pr, "recurring", None) or {})
            if (getattr(pr, "active", True) and rec.get("meter") == meter_id
                    and getattr(pr, "product", None) == product_id):
                return pr.id
        except Exception:
            pass
    pr = stripe.Price.create(
        currency="usd",
        unit_amount_decimal=PRICE_UNIT_AMOUNT_DECIMAL,
        billing_scheme="per_unit",
        product=product_id,
        recurring={"interval": "month", "usage_type": "metered", "meter": meter_id},
    )
    state["price_id"] = pr.id
    return pr.id


# Subscription statuses that mean "usage is accruing to an invoice" — anything
# else (canceled/incomplete_expired) means we should re-create.
_LIVE_SUB_STATUSES = {"active", "trialing", "past_due", "unpaid", "incomplete"}


def _ensure_subscription(stripe, customer_id: str, team: str, price_id: str,
                         state: dict) -> Optional[str]:
    """Idempotently ensure a metered Subscription for ``team`` on ``price_id``;
    return its id. Default ``charge_automatically`` collection — in test mode the
    subscription goes ``active`` with no card, and meter events for the customer
    accrue to its upcoming invoice."""
    subs = state.setdefault("subscriptions", {})
    cached = subs.get(team)
    if cached:
        try:
            s = stripe.Subscription.retrieve(cached)
            if getattr(s, "status", "") in _LIVE_SUB_STATUSES:
                return s.id
        except Exception:
            pass
    s = stripe.Subscription.create(customer=customer_id, items=[{"price": price_id}])
    subs[team] = s.id
    return s.id


# --------------------------------------------------------------------------
# The public surface: make_meter
# --------------------------------------------------------------------------

def make_meter(*, event_name: Optional[str] = None, strict: bool = False) -> Meter:
    """Build the dispatcher meter callable.

    Returns a no-op meter (always ``None``) — with a single stderr warning —
    if Stripe is unavailable for any reason (no key, a refused live key, the
    SDK missing, or setup failing). With ``strict=True`` a refused live key
    raises instead of degrading (used by tests/verification, never the demo).
    """
    load_dotenv()
    key, reason = resolve_key()

    if reason == "live":
        if strict:
            raise RuntimeError("refusing to run against a LIVE Stripe key (sk_live_…)")
        _warn_once("STRIPE key is a LIVE key — refusing it; running local-only "
                   "(set a sk_test_… key). Ledger will record settled=0.")
        return _noop_meter
    if reason == "missing":
        _warn_once("no STRIPE_SECRET_KEY set — running local-only (ledger settled=0).")
        return _noop_meter
    if reason == "malformed":
        _warn_once("STRIPE key is neither sk_test_ nor sk_live_ — ignoring; running local-only.")
        return _noop_meter

    try:
        import stripe  # type: ignore
    except Exception:
        _warn_once("the `stripe` package is not importable — running local-only.")
        return _noop_meter

    stripe.api_key = key
    ev = event_name or os.environ.get("BURSAR_STRIPE_METER") or DEFAULT_METER_EVENT
    state = _load_state()

    try:
        meter_id = _ensure_meter(stripe, ev, state)
        _save_state(state)
    except Exception as e:
        if strict:
            raise
        _warn_once(f"Stripe meter setup failed ({e!r}) — running local-only.")
        return _noop_meter

    # S3 — best-effort: provision the invoice loop (Product + metered Price) so
    # the meter events below roll into a real upcoming invoice. A failure here
    # must NOT disable metering — we keep ``price_id=None`` and meter only.
    price_id: Optional[str] = None
    try:
        product_id = _ensure_product(stripe, state)
        price_id = _ensure_price(stripe, meter_id, product_id, state)
        _save_state(state)
    except Exception as e:
        if strict:
            raise
        _warn_once(f"Stripe billing setup (product/price) failed ({e!r}) — "
                   "metering only, no invoice loop.")

    def _send(team: str, total: float, identifier: str) -> bool:
        """Do the network work for one meter event (customer/subscription ensure
        + MeterEvent.create) under ``identifier``. Returns True on success.
        Used both inline (sync path) and on a daemon thread (deferred path)."""
        try:
            with _STATE_LOCK:
                cid = _ensure_customer(stripe, team, state)
                # S3 — link this customer to the metered Price once per team so its
                # usage accrues to an invoice. Best-effort: a subscription hiccup must
                # not stop the meter event (the row would lose its invoice, not its
                # settlement). Skipped entirely if the price wasn't provisioned.
                if price_id:
                    try:
                        _ensure_subscription(stripe, cid, team, price_id, state)
                    except Exception as e:
                        _warn_once(f"subscription ensure failed for team {team!r} "
                                   f"({e!r}) — metering this query without an invoice link.")
                _save_state(state)
            micros = max(1, round(float(total) * USD_TO_MICROS))
            stripe.billing.MeterEvent.create(
                event_name=ev,
                identifier=identifier,
                payload={"value": str(micros), "stripe_customer_id": cid},
            )
            return True
        except Exception as e:
            _warn_once(f"meter event failed ({e!r}) — recording this query local-only.")
            return False

    def meter(team: str, model: str, tokens: int, total: float,
              defer: bool = False) -> Optional[str]:
        # The event identifier is generated CLIENT-SIDE, before any network call,
        # so a caller can persist it immediately. Stripe dedupes by identifier, so
        # an off-thread retry of the same id is safe.
        identifier = "be_" + secrets.token_hex(12)
        if defer:
            # Live-gate path: hand the id back instantly (the ledger row writes
            # settled=1 now) and fire the ~network MeterEvent.create off-thread so
            # the agent turn isn't blocked ~per call. Fail-open: a failed send just
            # never reaches Stripe; it's logged via _warn_once.
            threading.Thread(
                target=_send, args=(team, total, identifier),
                name="bursar-meter-send", daemon=True,
            ).start()
            return identifier
        # Synchronous path (firehose/dispatcher, latency-insensitive): only claim
        # the id if the event actually landed.
        return identifier if _send(team, total, identifier) else None

    print(f"[bursar.stripe] metering live (test mode): event={ev!r} meter={meter_id} "
          f"price={price_id or 'none'}", file=sys.stderr)
    return meter


def _noop_meter(team: str, model: str, tokens: int, total: float) -> None:
    return None


def is_active() -> bool:
    """True iff a usable test key is configured (cheap pre-check for callers
    deciding whether to pass meter= into run_tick)."""
    load_dotenv()
    _, reason = resolve_key()
    return reason == "ok"


# --------------------------------------------------------------------------
# S3 — invoice readout (the money-loop affordance for dashboard / CLI)
# --------------------------------------------------------------------------

def billing_summary(teams: Optional[list] = None) -> list:
    """Per-team upcoming-invoice totals — the money loop's readout.

    Read-only and best-effort: previews each team's metered subscription invoice
    via ``stripe.Invoice.create_preview`` and returns
    ``[{team, customer, subscription, currency, upcoming_total_cents,
    upcoming_total_usd}]`` (an ``error`` key instead of totals if that one team's
    preview fails). Returns ``[]`` when Stripe is unavailable — NEVER raises, so a
    dashboard endpoint or CLI can call it unconditionally.
    """
    load_dotenv()
    key, reason = resolve_key()
    if reason != "ok":
        return []
    try:
        import stripe  # type: ignore
    except Exception:
        return []
    stripe.api_key = key
    state = _load_state()
    subs = state.get("subscriptions", {}) or {}
    customers = state.get("customers", {}) or {}
    pairs = list(subs.items()) if teams is None else [(t, subs.get(t)) for t in teams]
    out: list = []
    for team, sub_id in pairs:
        cid = customers.get(team)
        if not sub_id or not cid:
            continue
        entry = {"team": team, "customer": cid, "subscription": sub_id}
        try:
            prev = stripe.Invoice.create_preview(customer=cid, subscription=sub_id)
            cents = int(getattr(prev, "total", 0) or 0)
            entry.update({
                "currency": getattr(prev, "currency", "usd"),
                "upcoming_total_cents": cents,
                "upcoming_total_usd": round(cents / 100.0, 4),
            })
        except Exception as e:  # one team's preview failing must not sink the rest
            entry["error"] = repr(e)
        out.append(entry)
    return out


# --------------------------------------------------------------------------
# CLI — self-test / verification
# --------------------------------------------------------------------------

def _selftest() -> int:
    """Send one meter event and report. Returns process exit code."""
    load_dotenv()
    key, reason = resolve_key()
    print(f"key status: {reason}")
    if reason != "ok":
        print("No usable test key — meter would run as no-op. "
              "Set STRIPE_SECRET_KEY=sk_test_… in .env.")
        return 1
    meter = make_meter(strict=True)
    eid = meter("research", "nemotron-nano", 1234, 0.05)
    if eid:
        print(f"OK — meter event sent: {eid}")
        return 0
    print("Meter returned None (event failed) — see warnings above.")
    return 2


def _billing_cli() -> int:
    """Print each team's upcoming Stripe invoice total (the closed money loop)."""
    load_dotenv()
    key, reason = resolve_key()
    print(f"key status: {reason}")
    if reason != "ok":
        print("No usable test key — no invoices to read.")
        return 1
    rows = billing_summary()
    if not rows:
        print("No team subscriptions provisioned yet — send a live/metered query "
              "first (make_meter() provisions the Product/Price/Subscription).")
        return 0
    total = 0.0
    for r in rows:
        if "error" in r:
            print(f"  {r['team']:<12} {r['subscription']}  preview failed: {r['error']}")
            continue
        total += r["upcoming_total_usd"]
        print(f"  {r['team']:<12} {r['subscription']}  upcoming: "
              f"${r['upcoming_total_usd']:.4f} {r['currency']}")
    print(f"  {'TOTAL':<12} {'':<28}  ${total:.4f}")
    return 0


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "billing":
        raise SystemExit(_billing_cli())
    raise SystemExit(_selftest())
