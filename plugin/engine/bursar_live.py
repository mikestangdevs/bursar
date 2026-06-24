"""Bursar live engine API — the shared surface the `bursar_gate` agent plugin
calls to price / dedup / record the user's REAL Hermes traffic.

The synthetic pipeline (firehose → ingest pending → ``run_tick``) clears a batch
of queued queries against a budget. Live traffic is different: each LLM call is
a single event that *already happened* (or is about to), so there's no batch to
rank — we decide and record one query at a time, then immediately mark it
serviced/deduped with the TRUE numbers from the agent.

This module reuses the engine's own economics verbatim (``bursar_scoring`` for
value, ``bursar_pricing`` for token/cost estimates, ``bursar_dedup`` for the
embedding + similarity window, and the dispatcher's ``VALUE_PER_POINT`` /
``TRADING_FEE`` / ``value_usd``) so live rows reconcile with the firehose rows
on the exact same ledger. It writes through ``bursar_db`` (the same DB the
desktop Trading Floor reads over the E4 ``/events`` WebSocket), so real traffic
lights up the floor live.

Pure engine code — no Hermes imports, no side effects on import. Safe to import
from the agent process (via the plugin) or unit tests.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Optional

import bursar_db as db
import bursar_dedup as dedup
import bursar_embed as embed
import bursar_pricing as pricing
import bursar_scoring as scoring
from bursar_dispatcher import fee_for, value_usd, MIN_WORTH

# Default cap for an auto-created live team budget ($). Live teams aren't in the
# firehose's preset caps, so the floor's Budgets page would otherwise miss them;
# we create a generous envelope so real traffic is visible without being gated
# (budget-stop is shadow-only in the F-series anyway).
LIVE_BUDGET_CAP = float(os.environ.get("BURSAR_LIVE_CAP", "25.0"))


def tenant_id() -> str:
    """The isolation boundary a row belongs to — the org/deployment, NOT the
    conversation (``team`` is per-session: ``live:<session>``, so it can't gate
    cross-session reuse without killing it). Tenant is PROCESS-level: a
    multi-tenant deployment runs one gateway per org (or sets ``BURSAR_TENANT``
    per process), so every row that process writes is tagged with its org, and
    dedup-SERVE never crosses tenants. Unset → ``'local'``: a single shared
    tenant, so the single-user "you asked this in another conversation" reuse is
    fully preserved (every row is 'local')."""
    return (os.environ.get("BURSAR_TENANT", "") or "").strip() or "local"


# Known-safe cheaper model per provider for down-route (F3). Only ever swap to a
# model the SAME provider will actually accept — never invent an id that would
# make the provider reject the turn. ``BURSAR_CHEAP_MODEL`` overrides globally.
_CHEAP_BY_PROVIDER = {
    "anthropic": "claude-haiku-4-5",
    "openai": "gpt-4.1-nano",
    "openai-codex": "gpt-4.1-nano",
    # This setup runs provider=nous with model ids in the ``anthropic/<id>``
    # portal namespace (default ``anthropic/claude-opus-4.8`` — confirmed the
    # portal serves anthropic-prefixed Claude ids). The safe cheaper sibling is
    # haiku in that same namespace. The EXACT valid id must be validated against
    # the live portal /v1/models before arming down-route (F6 does this and
    # shadows if absent). Overridable via BURSAR_NOUS_CHEAP_MODEL /
    # BURSAR_CHEAP_MODEL; cheap_target returns None (→ shadow) if unknown.
    "nous": os.environ.get("BURSAR_NOUS_CHEAP_MODEL", "anthropic/claude-haiku-4.5"),
}


def prompt_from_request(request: dict) -> str:
    """Best-effort extract the prompt text to score/dedup from an LLM request.

    Prefers the last user message; falls back to the last message of any role.
    Handles both string content and the OpenAI/Anthropic list-of-parts shape.
    Returns "" if nothing usable is found (caller then skips recording).
    """
    if not isinstance(request, dict):
        return ""
    messages = request.get("messages") or []
    if not isinstance(messages, list):
        return ""

    def _text(content) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = []
            for p in content:
                if isinstance(p, dict):
                    parts.append(p.get("text") or p.get("content") or "")
                elif isinstance(p, str):
                    parts.append(p)
            return " ".join(s for s in parts if s)
        return ""

    last_any = ""
    last_user = ""
    for m in messages:
        if not isinstance(m, dict):
            continue
        txt = _text(m.get("content"))
        if txt:
            last_any = txt
            if m.get("role") == "user":
                last_user = txt  # keep scanning; want the LAST user msg
    return last_user or last_any


def _is_tool_result_message(m: dict) -> bool:
    """True if a message is a tool RESULT, not a fresh user prompt.

    OpenAI uses ``role='tool'``; Anthropic encodes tool results as
    ``role='user'`` with ``tool_result`` content parts. Either way it's the agent
    loop feeding itself, never the human asking something new."""
    if not isinstance(m, dict):
        return False
    if m.get("role") == "tool":
        return True
    content = m.get("content")
    if isinstance(content, list):
        for p in content:
            if isinstance(p, dict) and p.get("type") in ("tool_result", "tool_use"):
                return True
    return False


def _message_text(m: dict) -> str:
    """Flatten a message's content to plain text (content may be a string or a
    list of parts). Best-effort; never raises."""
    if not isinstance(m, dict):
        return ""
    c = m.get("content")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        out = []
        for p in c:
            if isinstance(p, str):
                out.append(p)
            elif isinstance(p, dict):
                t = p.get("text") or p.get("content")
                if isinstance(t, str):
                    out.append(t)
        return " ".join(out)
    return ""


# Hermes injects USER-role pseudo-messages mid-turn when machinery needs to talk to
# the model — a timed-out / errored tool call surfaces as a user message like
# "[System: Your previous tool call ...]" or "[Tool error: ...]". These have
# role=user and are NOT tool_result parts, so the plain last-message check (N3)
# mistakes them for a fresh human prompt and would let dedup fire MID-turn. We
# never SERVE off the heuristic path anyway, but a mid-turn augment-inject would
# still corrupt the loop's context, so the opening check must reject them.
_INJECTED_PSEUDO_USER = re.compile(
    r"^\s*[\[(]\s*(?:system|tool\s+error|tool\s+result|error|note|reminder)\b",
    re.IGNORECASE,
)


def _looks_injected_pseudo_user(m: dict) -> bool:
    """True if a user-role message is actually Hermes-injected turn machinery (a
    timed-out tool notice, a system retry), not a human prompt. Used to keep the
    turn-opening fallback robust when no turn_id is available (N3)."""
    return bool(_INJECTED_PSEUDO_USER.match(_message_text(m).lstrip()))


def is_turn_opening_request(request: dict) -> bool:
    """True iff this LLM call is the *start* of a human turn — the last message is
    the user's prompt, with no assistant/tool steps appended after it yet.

    Why this gates dedup: in an agentic loop ONE user turn fans out into many LLM
    calls — the opening call ends with the user message; every follow-up call ends
    with a tool result or an assistant step (growing context). But
    ``prompt_from_request`` keys on the last *user* message, which is invariant
    across the whole turn. Without this guard the loop's own internal calls look
    like a dozen re-asks of the same prompt, and dedup fires mid-turn on the very
    first ask (the bug we hit: a bogus "you asked this before" surfaced during the
    agent's own search loop). Dedup must engage ONLY on the opening call so it
    fires once per turn — on a genuine re-ask, never on the agent talking to
    itself.

    This is the FALLBACK path (used only when no ``turn_id`` is threaded — the
    turn_id-based ``first_call`` in the gate is the primary, drift-immune signal).
    N3 hardens it against Hermes' injected pseudo-user messages: a last message
    that is really turn machinery ("[System: …]", "[Tool error: …]") is NOT a turn
    opening, so the fallback can't be drifted into firing dedup mid-loop."""
    if not isinstance(request, dict):
        return False
    messages = request.get("messages") or []
    if not isinstance(messages, list):
        return False
    last = next((m for m in reversed(messages) if isinstance(m, dict)), None)
    if last is None or last.get("role") != "user":
        return False
    if _is_tool_result_message(last):
        return False
    return not _looks_injected_pseudo_user(last)


# --------------------------------------------------------------------------
# Quote — score + cost a single live prompt (no DB write)
# --------------------------------------------------------------------------

@dataclass
class LiveQuote:
    value: float
    tier: str
    rationale: str
    est_tokens: int
    est_cost: float
    naive_cost: float
    model: str           # the model Bursar would route this tier to
    fee: float
    worth: float         # value in dollars
    net: float           # worth − (est_cost + fee)
    vpt: float           # worth / (est_cost + fee)


def quote(prompt: str, team: str = "live") -> LiveQuote:
    """Score + price a prompt exactly as the dispatcher's ``_quote`` does, but
    standalone (no DB row). Used to attach value/tier/rationale to a live call
    and to decide dedup-serve / down-route / shadow actions."""
    vs = scoring.score(prompt)
    est = pricing.estimate(prompt, vs.tier)
    fee = fee_for(est.est_cost)
    worth = value_usd(vs.value)
    spend = est.est_cost + fee
    net = round(worth - spend, 6)
    if worth < MIN_WORTH and net > 0:
        net = 0.0  # below the junk-worth floor → reject regardless of the proportional fee
    vpt = round(worth / spend, 4) if spend > 0 else float("inf")
    return LiveQuote(
        value=vs.value, tier=vs.tier, rationale=vs.rationale,
        est_tokens=est.tokens, est_cost=est.est_cost, naive_cost=est.naive_cost,
        model=est.model, fee=fee, worth=worth, net=net, vpt=vpt,
    )


# --------------------------------------------------------------------------
# True cost — what this real call actually costs, in dollars
# --------------------------------------------------------------------------
#
# Decision (see memory bursar-live-pricing-decision): the agent runs on nous,
# but Bursar is the *pricer* — figures are market-rate regardless of executor.
# Native first (Hermes's own estimate_usage_cost, which may return real
# per-token cost even for nous), market-rate fallback so the floor is never
# $0/blank. Dropping in raw Anthropic/OpenAI keys later changes nothing here.

def _usage_total_tokens(usage: dict) -> int:
    return (
        int(usage.get("input_tokens", 0) or 0)
        + int(usage.get("output_tokens", 0) or usage.get("completion_tokens", 0) or 0)
        + int(usage.get("cache_read_tokens", 0) or 0)
        + int(usage.get("cache_write_tokens", 0) or 0)
    )


def reasoning_tokens_from_usage(usage) -> int:
    """Best-effort reasoning/thinking-token count from a provider usage dict.

    Reasoning models bill thinking tokens that the visible answer never shows;
    they're the bulk of a reasoning call's cost and the thing dedup most wants to
    credit ("you already paid to think about this"). Providers report them in
    several shapes — a flat field, or nested under completion/output details
    (OpenAI: ``completion_tokens_details.reasoning_tokens``). Returns 0 when none
    is reported, so the headline simply omits the clause rather than guessing."""
    if not isinstance(usage, dict):
        return 0
    flat = usage.get("reasoning_tokens") or usage.get("thinking_tokens")
    if flat:
        return int(flat or 0)
    for key in ("completion_tokens_details", "output_tokens_details", "completion_token_details"):
        details = usage.get(key)
        if isinstance(details, dict):
            rt = details.get("reasoning_tokens") or details.get("thinking_tokens")
            if rt:
                return int(rt or 0)
    return 0


def original_reasoning_tokens(conn, qid) -> int:
    """Reasoning tokens recorded on a serviced query (the original of a dedup
    hit), or 0 if none/unknown. Lets a dedup-serve credit the thinking the user
    already paid for on the first ask."""
    row = conn.execute("SELECT reasoning_tokens FROM queries WHERE id=?", (qid,)).fetchone()
    return int((row["reasoning_tokens"] if row else 0) or 0)


def _normalize_model_name(model: Optional[str]) -> str:
    """Map a provider/portal model id to Bursar's catalog naming.

    nous serves ids like ``anthropic/claude-opus-4.8``; the catalog uses
    ``claude-opus-4-8``. Strip the provider prefix, a ``:free`` openrouter
    suffix, and a trailing ``-YYYYMMDD`` provider date stamp (e.g.
    ``nemotron-3-super-120b-a12b-20230311``), then turn version dots into dashes
    so the lookup hits.
    """
    name = (model or "").split("/")[-1].strip()
    if name.lower().endswith(":free"):
        name = name[: -len(":free")]
    name = re.sub(r"-\d{8}$", "", name)  # drop a trailing provider date stamp
    return name.replace(".", "-")


def _market_cost(model: Optional[str], total_tokens: int,
                 fallback_quote: Optional[LiveQuote] = None) -> float:
    """Bursar's own market-rate cost for *model* at *total_tokens* ($)."""
    m = pricing.MODEL_BY_NAME.get(model or "") or pricing.MODEL_BY_NAME.get(_normalize_model_name(model))
    if m is not None:
        return pricing.cost_for_model(total_tokens, m)
    # Unknown id: use the routed quote's implied $/token, else a mid-tier proxy.
    if fallback_quote is not None and fallback_quote.est_tokens:
        rate = fallback_quote.est_cost / fallback_quote.est_tokens
        return round(total_tokens * rate, 6)
    proxy = pricing.MODEL_BY_NAME.get("gpt-4o") or pricing.MODEL_CATALOG[len(pricing.MODEL_CATALOG) // 2]
    return pricing.cost_for_model(total_tokens, proxy)


def true_cost(model: Optional[str], usage: dict, *, provider: Optional[str] = None,
              base_url: Optional[str] = None,
              fallback_quote: Optional[LiveQuote] = None) -> tuple[float, int, str]:
    """Return ``(cost_usd, total_tokens, source)`` for a real call.

    ``source`` is ``"native"`` when Hermes's ``estimate_usage_cost`` produced a
    positive figure, else ``"market"`` (Bursar's own catalog). Never raises and
    never returns a negative cost; ``cost`` is 0.0 only when there were 0 tokens.
    """
    total = _usage_total_tokens(usage)
    # A ``:free`` executor id (openrouter free tier) ran at zero marginal cost.
    # Bursar still prices it at MARKET — but the native pricer can mis-map a
    # date-stamped free id to a premium tier (we saw ``nemotron-…:free`` priced
    # at opus-tier ~$18.75/Mtok, a phantom on a free model). A free model must
    # never out-price its market rate, so price these via Bursar's own catalog/
    # routed-quote rate instead of trusting native.
    is_free = (model or "").strip().lower().endswith(":free")
    # 1) Native — Hermes's own per-token pricing (may cover nous directly).
    if not is_free:
        try:
            from agent.usage_pricing import CanonicalUsage, estimate_usage_cost  # lazy: keep module Hermes-free
            cu = CanonicalUsage(
                input_tokens=int(usage.get("input_tokens", 0) or 0),
                output_tokens=int(usage.get("output_tokens", 0) or usage.get("completion_tokens", 0) or 0),
                cache_read_tokens=int(usage.get("cache_read_tokens", 0) or 0),
                cache_write_tokens=int(usage.get("cache_write_tokens", 0) or 0),
            )
            res = estimate_usage_cost(model, cu, provider=provider, base_url=base_url)
            amt = getattr(res, "amount_usd", None)
            if amt is not None and float(amt) > 0:
                return float(amt), total, "native"
        except Exception:
            pass
    # 2) Market-rate fallback — Bursar's catalog at the real token count.
    return _market_cost(model, total, fallback_quote), total, "market"


# --------------------------------------------------------------------------
# Dedup — is this prompt a near-duplicate of a recently-serviced one?
# --------------------------------------------------------------------------

def find_dup(conn, prompt: str, *, threshold: Optional[float] = None,
             team: Optional[str] = None, tenant: Optional[str] = None):
    """Return ``(orig_id, similarity, cached_result)`` if *prompt* matches a
    recently-serviced LIVE query **that has a real cached answer**, else ``None``.

    Crucially the dedup window is seeded ONLY from rows whose result is a
    servable answer (``is_servable_answer``). A complex agentic turn records
    several serviced rows for the same prompt — plan/tool steps with empty
    ``content`` (placeholder result) plus one final summary row. Excluding the
    empty steps means the embedding match lands on the summary row, so dedup
    reliably serves the real answer instead of an intermediate fragment. Scoped
    to ``source='live'`` so real questions only match other real answers, never
    the synthetic firehose.

    ``team`` is normally ``None`` — the live gate WANTS cross-session reuse ("you
    asked this in another conversation"), so it matches across every live team.
    Passing a ``team`` restricts the window to that team's own rows; the
    agentic-loop proof (G-series) uses this so its headline reflects the loop's
    own repetition and can't drift on accidental matches against unrelated live
    traffic that happens to be in the DB.

    ``tenant`` is the SECURITY boundary (N1): when set, the window is restricted
    to that tenant's rows, so a match can never come from another org/deployment.
    The live gate passes its own ``tenant`` by default (cross-tenant reuse stays
    OFF), so team B's reworded question can never be served — or shown — team A's
    answer. Cross-tenant AUGMENT is opt-in (gate passes ``tenant=None`` only when
    ``BURSAR_CROSS_TENANT_AUGMENT`` is set, and even then never SERVES across).
    Distinct from ``team`` precisely because team is per-session: scoping reuse by
    team would kill the single-user cross-conversation reuse, scoping by tenant
    does not (all of one user's sessions share the default 'local' tenant).
    """
    from bursar_dispatcher import _Window, DEDUP_WINDOW  # local import: avoids cycle at module load

    # Embed with the active backend and only compare against rows from the SAME
    # embedding space (``embed_model``) — a 256-dim hashing vector and a 384-dim
    # semantic vector are not comparable. The query vector's own length sizes the
    # window matrix, so a backend switch can't cause a shape mismatch.
    qvec = embed.embed_vec(prompt)
    space = embed.active_model()
    clauses = ["status='serviced'", "source='live'", "embedding IS NOT NULL",
               "result IS NOT NULL", "embed_model = ?"]
    params: list = [space]
    if team is not None:
        clauses.append("team = ?")
        params.append(team)
    if tenant is not None:
        clauses.append("tenant = ?")
        params.append(tenant)
    params.append(DEDUP_WINDOW)
    rows = conn.execute(
        "SELECT id, embedding, result FROM queries WHERE " + " AND ".join(clauses)
        + " ORDER BY decided_at DESC LIMIT ?",
        tuple(params),
    ).fetchall()
    win = _Window(dim=int(qvec.shape[0]))
    cached_by_id: dict = {}
    # Insert oldest-first so the newest land at the front of the ring buffer.
    for r in reversed(rows):
        if not is_servable_answer(r["result"]):
            continue
        try:
            win.add(r["id"], dedup.unpack(r["embedding"]))
            cached_by_id[r["id"]] = r["result"]
        except Exception:
            continue
    thr = embed.threshold() if threshold is None else threshold
    match = win.match(qvec, thr)
    if match is None:
        return None
    orig_id, sim = match
    return orig_id, float(sim), cached_by_id.get(orig_id)


def is_servable_answer(text) -> bool:
    """True if *text* is a real cached answer worth replaying from cache.

    Guards dedup-serve so it can never hand back a non-answer: rejects None,
    blank, the ``record_serviced`` placeholder, and trivially short fragments
    (a reasoning preamble that slipped through, an interrupted turn). When this
    is False the gate falls through to a real API call instead of replaying."""
    if not isinstance(text, str):
        return False
    t = text.strip()
    if len(t) < 16:
        return False
    if t.startswith("[serviced via"):  # record_serviced's no-answer placeholder
        return False
    return True


def query_ref(conn, qid):
    """Return ``(session_id, prompt, created_at)`` for a query, or ``(None, None,
    None)``. Used to build a dedup jump-link back to the conversation where the
    original answer lives."""
    row = conn.execute(
        "SELECT session_id, prompt, created_at FROM queries WHERE id=?", (qid,)
    ).fetchone()
    if row is None:
        return None, None, None
    return row["session_id"], row["prompt"], row["created_at"]


def query_tenant(conn, qid) -> Optional[str]:
    """The tenant a query row belongs to, or ``None`` if unknown/unreadable.

    Used by the gate to decide whether a dedup match is same-tenant. Returns
    ``None`` (not a guessed tenant) on any miss so the caller fails CLOSED — a
    None can never compare equal to the live tenant, so an unreadable row is
    treated as cross-tenant and is never SERVED."""
    try:
        row = conn.execute("SELECT tenant FROM queries WHERE id=?", (qid,)).fetchone()
    except Exception:
        return None
    return row["tenant"] if row is not None else None


def cache_age_seconds(conn, qid) -> Optional[int]:
    """Age in seconds of the cached answer for ``qid`` (now − its decided_at), or
    None if unknown. Used by the LIVE temporal class: a price/score may be reused
    with a "since you asked" comparison only if the prior is very recent; otherwise
    the turn runs fresh. Timestamps are epoch ints (``bursar_db.now``)."""
    row = conn.execute(
        "SELECT decided_at, created_at FROM queries WHERE id=?", (qid,)
    ).fetchone()
    if row is None:
        return None
    stamp = row["decided_at"] if row["decided_at"] is not None else row["created_at"]
    if stamp is None:
        return None
    try:
        return max(0, int(db.now()) - int(stamp))
    except (TypeError, ValueError):
        return None


def dedup_reply(cached: str, *, sim: float, saved: float = 0.0,
                orig_session: Optional[str] = None,
                same_session: bool = False, reasoning_tokens: int = 0) -> str:
    """Compose the in-chat message Bursar returns on a dedup hit.

    The point (per the product call): the user should feel *known* — told they
    already asked this, handed the real prior answer to build on, and given a
    one-click jump to where it lives. The jump uses a ``#bursar-session:<id>``
    fragment href (NOT a custom ``scheme://`` URL — Streamdown's link-hardening
    blocks unknown schemes; same-page fragments pass, the convention the media
    links use). The desktop markdown renderer turns the whole notice into a
    styled card with a Hermes-blue jump button. The stats copy + the session id
    ride in the query string so the wording lives here, not duplicated in TS.

    ``reasoning_tokens`` (>0 only for reasoning models) reframes the headline from
    a pure answer-cache into "you already paid to *think* about this": those
    thinking tokens are the bulk of a reasoning call's cost, and dedup skips
    re-spending them. Omitted cleanly when 0/unknown.
    """
    from urllib.parse import quote

    pct = round(max(0.0, min(1.0, sim)) * 100)
    bits = [f"~{pct}% match"]
    if saved and saved > 0:
        bits.append(f"saved ~${saved:.4f}")
    if reasoning_tokens and reasoning_tokens > 0:
        bits.append(f"skipped ~{reasoning_tokens:,} reasoning tokens you already paid to think")
    bits.append("served instantly from cache")
    stats = " · ".join(bits)
    # Only offer a jump when the original lives in a *different* conversation and
    # we actually captured its id — jumping to the current chat is pointless.
    params = [f"stats={quote(stats)}"]
    if orig_session and not same_session:
        params.append(f"session={quote(orig_session)}")
    notice = f"[↗ Open the earlier conversation](#bursar-dedup?{'&'.join(params)})"
    answer = (cached or "").strip() or "_(the earlier answer wasn't cached)_"
    return f"{notice}\n\nHere's your earlier answer:\n\n{answer}"


def original_turn_summary(conn, orig_id) -> Optional[dict]:
    """Summarize the ORIGINAL turn that produced ``orig_id`` — the rediscovery
    work a re-ask now skips.

    A "turn" is identified EXACTLY by ``turn_id`` (every LLM call of one user turn
    shares it). We sum the **real** billed tokens/cost from the LEDGER (not the
    predicted ``est_cost``) so the savings are measured, never invented — and pick
    the LAST serviced row's answer as the turn's final answer.

    Grouping by turn_id is what keeps the number honest: grouping by
    ``(session_id, prompt)`` would CONFLATE two separate turns that asked the same
    question in one session, doubling the reported cost. When ``turn_id`` is NULL
    (legacy rows, or the agentloop proof which has no turns) we fall back to the
    (session_id, prompt) grouping — those callers don't surface this number.

    Returns ``{calls, tokens, cost, final_answer, session_id, prompt}`` or
    ``None`` if the original can't be located."""
    row = conn.execute(
        "SELECT session_id, prompt, turn_id FROM queries WHERE id=?", (orig_id,)
    ).fetchone()
    if row is None:
        return None
    sess, prompt, turn_id = row["session_id"], row["prompt"], row["turn_id"]
    if turn_id:
        qrows = conn.execute(
            "SELECT id, result, decided_at FROM queries "
            "WHERE source='live' AND status='serviced' AND turn_id = ? "
            "ORDER BY decided_at ASC",
            (turn_id,),
        ).fetchall()
    else:
        # Legacy / agentloop fallback: no turn_id. ``session_id IS ?`` matches NULL.
        qrows = conn.execute(
            "SELECT id, result, decided_at FROM queries "
            "WHERE source='live' AND status='serviced' AND turn_id IS NULL "
            "AND session_id IS ? AND prompt=? ORDER BY decided_at ASC",
            (sess, prompt),
        ).fetchall()
    if not qrows:
        return None
    ids = [r["id"] for r in qrows]
    placeholders = ",".join("?" for _ in ids)
    led = conn.execute(
        f"SELECT COALESCE(SUM(tokens),0) AS toks, COALESCE(SUM(total),0) AS cost, "
        f"COUNT(*) AS n FROM ledger WHERE query_id IN ({placeholders})",
        tuple(ids),
    ).fetchone()
    # Final answer = the latest serviced row that actually carries a servable
    # answer (skip trailing placeholder/empty steps).
    final_answer = None
    for r in reversed(qrows):
        if is_servable_answer(r["result"]):
            final_answer = r["result"]
            break
    return {
        "calls": int(led["n"] or len(ids)),
        "tokens": int(led["toks"] or 0),
        "cost": round(float(led["cost"] or 0.0), 6),
        "final_answer": final_answer,
        "session_id": sess,
        "prompt": prompt,
        "turn_id": turn_id,
    }


def _humanize_age(seconds: Optional[int]) -> Optional[str]:
    """Render an age in seconds as a coarse human phrase ("about 3 days ago"), or
    None if unknown. Coarse on purpose — the agent surfaces it to the user."""
    if seconds is None:
        return None
    s = max(0, int(seconds))
    if s < 90:
        return "moments ago"
    m = s // 60
    if m < 90:
        return f"about {m} minute{'s' if m != 1 else ''} ago"
    h = s // 3600
    if h < 36:
        return f"about {h} hour{'s' if h != 1 else ''} ago"
    d = s // 86400
    return f"about {d} day{'s' if d != 1 else ''} ago"


# N5 — cap the prior answer injected as context. This is a SAFETY VALVE for a
# pathological prior (a runaway multi-10K-char answer), NOT a routine trimmer:
# truncating a normal answer would delete the very content that makes augment
# worth doing (the agent reuses the prior to skip the rediscovery loop), and the
# head/tail split can gut the middle of a structured answer.
#
# Sizing is DATA-DRIVEN (≈4 chars/token for English prose): measured against the
# live answer-length distribution, the largest real answer was 11,168 chars and
# p95 was 5,738. The default 24000 chars (~6K tokens) sits ~2x above that observed
# max, so a legitimate detailed answer passes through WHOLE; only a genuinely huge
# (pathological) prior is trimmed. ~6K tokens is <5% of the 100K–200K+ windows the
# models here run (deepseek-v4-pro, Claude), and the augment already replaces a
# multi-call rediscovery loop that costs far more than the injected prior — so the
# context-bloat risk this guards is a rare near-limit edge case. Hence a generous
# safety-valve ceiling, NOT an aggressive trimmer. Tunable via BURSAR_MAX_PRIOR_CHARS
# (set it LOW to force the cap for a demo/test). When it fires we keep the HEAD and
# TAIL (opening framing + conclusion) and drop the middle with an explicit marker.
_MAX_PRIOR_CHARS = int(os.environ.get("BURSAR_MAX_PRIOR_CHARS", "24000"))


def _cap_prior(prior_answer: Optional[str]) -> str:
    """Return the prior answer for injection, head+tail-truncated to
    ``_MAX_PRIOR_CHARS`` with a marker noting how much was omitted. Short answers
    pass through unchanged."""
    text = (prior_answer or "").strip()
    if not text:
        return "(none captured)"
    if _MAX_PRIOR_CHARS <= 0 or len(text) <= _MAX_PRIOR_CHARS:
        return text
    # Reserve room for the marker; split the budget ~60/40 head/tail so the
    # opening (definitions, framing) is favored over the trailing detail.
    head_n = max(1, int(_MAX_PRIOR_CHARS * 0.6))
    tail_n = max(1, _MAX_PRIOR_CHARS - head_n)
    omitted = len(text) - head_n - tail_n
    marker = f"\n\n[… {omitted:,} chars of the prior answer omitted to fit the context budget …]\n\n"
    return text[:head_n].rstrip() + marker + text[-tail_n:].lstrip()


def prior_cap_stats(prior_answer: Optional[str]) -> dict:
    """N5 observability: how large the prior answer was and how much actually got
    injected after :func:`_cap_prior`. Recomputes via the SAME path that builds the
    block, so the recorded figures can't drift from what was injected. Keys:
    ``prior_chars`` (raw), ``injected_chars`` (post-cap), ``capped`` (bool)."""
    raw = (prior_answer or "")
    injected = _cap_prior(prior_answer)
    return {
        "prior_chars": len(raw),
        "injected_chars": len(injected),
        "capped": bool(_MAX_PRIOR_CHARS > 0 and len(raw.strip()) > _MAX_PRIOR_CHARS),
    }


def dedup_augment_block(*, prior_answer: str, sim: float, turn: Optional[dict],
                        orig_session: Optional[str], same_session: bool,
                        freshness: Optional[list] = None,
                        compare: bool = False,
                        prior_age: Optional[int] = None,
                        temporal_class: Optional[str] = None) -> str:
    """The context block injected into a re-ask's opening LLM call.

    This is the heart of the redesign: on a re-ask Bursar does NOT substitute a
    canned answer and end the turn. It hands the agent the prior answer as
    *context* and lets the agent run ONE cheap call — skipping the expensive
    rediscovery loop (the dozen search/read calls the first ask paid for) while
    staying live and resumable. The agent leads with a brief "you asked this
    before", reuses the prior answer, verifies currency with a targeted re-read if
    warranted, states what changed (if anything), then continues the conversation
    normally with that context in hand.

    ``compare`` (T3) switches the framing for TIME-SENSITIVE classes (evolving /
    recent-live): the prior answer is presented as a HISTORICAL BASELINE, the agent
    is told to re-derive the CURRENT state fresh and produce an explicit "what
    changed since <when>" section — turning the re-ask into a historical-diff
    feature instead of a plain reuse. ``prior_age`` (seconds) dates the baseline;
    ``temporal_class`` is carried for legibility. Because the call still runs fresh,
    a comparison-augment is NEVER stale — the prior is context, not the answer.

    Everything here is instruction + measured facts; the agent writes the actual
    user-facing words, so the reply is natural and the turn stays open."""
    pct = round(max(0.0, min(1.0, sim)) * 100)
    when = _humanize_age(prior_age)
    if compare:
        lines = [
            "[Bursar — repeat-question, time-sensitive: historical comparison]",
            f"This question is a ~{pct}% match to one you answered"
            + (" earlier in this conversation" if same_session else " in an earlier conversation")
            + (f", {when}." if when else "."),
            "This topic can change over time, so the earlier answer is a BASELINE, "
            "not the current answer. Re-derive the CURRENT state this turn, then tell "
            "the user explicitly what has changed since then.",
        ]
    else:
        lines = [
            "[Bursar — repeat-question shortcut]",
            f"This question is a ~{pct}% match to one already answered"
            + (" in this conversation." if same_session else " in an earlier conversation."),
        ]
    if turn:
        saved = turn.get("cost") or 0.0
        toks = turn.get("tokens") or 0
        calls = turn.get("calls") or 0
        if calls:
            lines.append(
                f"Answering it the first time took {calls} model call(s) / "
                f"~{toks:,} tokens / ~${saved:.4f} of lookup work. Reuse that "
                "instead of redoing it."
            )
    lines.append("")
    lines.append("PRIOR ANSWER (reuse this — do not re-derive from scratch):")
    lines.append(_cap_prior(prior_answer))
    # Freshness verdict — Bursar re-hashed (out of band, 0 tokens) ONLY the files
    # it captured from the prior turn. That is the cache-the-work payoff for those
    # files, but it is NOT a guarantee about the whole answer: the prior turn may
    # have leaned on sources Bursar never tracked (read via shell/search/an API, or
    # the original answer was itself incomplete — H4 live repro: the verified set
    # was 5 unrelated files while the answer-relevant file was never captured). So
    # every claim below is scoped to the NAMED files; we never assert a global
    # "nothing changed".
    # Three states (H9 adds 'missing'): a gone path carries unchanged=False AND
    # missing=True, so exclude it from 'changed' (it needs re-discovery, not a
    # re-read of a dead path). A relocated file is unchanged=True with relocated_to.
    unchanged = [f for f in (freshness or []) if f.get("unchanged")]
    missing = [f for f in (freshness or []) if f.get("missing")]
    changed = [f for f in (freshness or [])
               if not f.get("unchanged") and not f.get("missing")]
    if freshness:
        lines.append("")
        lines.append("FRESHNESS — Bursar re-verified by content hash ONLY the files it "
                     "captured from the prior answer (this is the entire currency check):")
        for f in unchanged:
            if f.get("relocated_to"):
                lines.append(f"  • {f.get('name')} — UNCHANGED but MOVED to "
                             f"{f.get('relocated_to')} (same content, new path).")
            else:
                lines.append(f"  • {f.get('name')} — UNCHANGED since the prior answer.")
        for f in changed:
            lines.append(f"  • {f.get('name')} — CHANGED since the prior answer.")
        for f in missing:
            lines.append(f"  • {f.get('name')} — GONE (its path no longer exists).")
    lines.append("")
    lines.append("Respond to the user as follows:")
    if compare:
        lines.append(
            "1. Open with one short line that they asked a version of this before"
            + (f" ({when})" if when else "")
            + " and that you'll give the current state plus what's changed since then."
        )
    else:
        lines.append(
            "1. Open with one short line that they've asked this before and Bursar is "
            "reusing the earlier work (saving the lookup)."
        )
    if unchanged and not changed and not missing:
        names = ", ".join(f.get("name") for f in unchanged)
        lines.append(
            f"2. Reuse the prior answer to skip the rediscovery work. Bursar verified "
            f"ONLY these files unchanged: {names} — do NOT re-read THOSE (wasted tokens). "
            f"But that is the only thing confirmed: if your answer relies on any source "
            f"NOT in that list, check it before presenting the answer as current. Do NOT "
            f"restart the full search loop."
        )
        lines.append(
            f"3. End by stating specifically what Bursar confirmed unchanged ({names}). Do "
            f"NOT tell the user \"nothing has changed\" in general unless the answer depends "
            f"only on those files."
        )
    elif changed or missing:
        actions = []
        if changed:
            cnames = ", ".join(f.get("name") for f in changed)
            actions.append(f"{cnames} CHANGED — re-read ONLY those and fold in what's different")
        if missing:
            mnames = ", ".join(f.get("name") for f in missing)
            actions.append(f"{mnames} is GONE — re-discover it (search for it by name/content; "
                           f"do NOT re-read the old path, it no longer exists)")
        unote = (" The rest Bursar checked are unchanged ("
                 + ", ".join(f.get("name") for f in unchanged) + ")." if unchanged else "")
        lines.append(
            f"2. Reuse the prior answer to skip the rediscovery work, but: "
            + "; ".join(actions) + f".{unote} If your answer relies on any source Bursar did "
            f"NOT list, check it too. Do NOT restart the full search loop."
        )
        lines.append(
            "3. End by telling the user exactly what changed / was relocated / is gone "
            "since the earlier answer."
        )
    else:
        # No files captured for the prior answer (it predates work-capture, or was
        # answered without reading any file Bursar tracks). Bursar verified NOTHING
        # here — so do not let the agent claim a currency it cannot back up.
        lines.append(
            "2. Reuse the prior answer to skip the rediscovery work, but Bursar verified "
            "NO files for it — treat the prior answer as a starting point, not a confirmed-"
            "current one. Re-check the sources the answer actually depends on before "
            "presenting it as current. Do NOT restart the full search loop from scratch."
        )
        lines.append(
            "3. End by telling the user what you re-checked and whether it changed. Do NOT "
            "claim \"nothing has changed\" unless you actually re-verified it this turn."
        )
    if compare:
        lines.append(
            "3b. REQUIRED — this is a time-sensitive re-ask. Re-derive the CURRENT "
            "answer this turn (do not present the prior answer as current), then add a "
            "short \"What's changed since "
            + (when or "the earlier answer")
            + "\" section that explicitly contrasts the prior answer with the current "
            "state — call out new developments, reversals, or \"still holds\". If "
            "genuinely nothing changed, say so plainly. This historical comparison is "
            "the point of the reuse. If you CANNOT verify the current value this turn "
            "(no tool/source available to check it), say so explicitly — do NOT assert "
            "a change, or that it \"still holds\", that you could not confirm."
        )
    lines.append(
        "4. Stay ready to continue — keep that context in hand for the user's "
        "follow-ups."
    )
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Budgets
# --------------------------------------------------------------------------

def ensure_live_budget(conn, team: str, cap: Optional[float] = None) -> None:
    """Create a budget envelope for a live team if it doesn't exist yet, so the
    team's real spend shows on the Budgets page. Never lowers an existing cap."""
    if db.get_budget(conn, team) is not None:
        return
    db.upsert_budget(conn, team, LIVE_BUDGET_CAP if cap is None else cap,
                     period="live")


# --------------------------------------------------------------------------
# Record — write a fully-decided live query (serviced / deduped)
# --------------------------------------------------------------------------

def _insert_decided_query(conn, *, team, prompt, value, tier, rationale,
                          est_tokens, est_cost, fee, vpt, model, status,
                          dedup_of=None, result=None, source="live",
                          session_id=None, reasoning_tokens=0, turn_id=None,
                          temporal_class=None, reuse_mode=None,
                          tenant=None) -> str:
    """Insert a query row that is already decided (no pending phase). Both
    ``created_at`` and ``decided_at`` are stamped now — the call already
    happened. Tagged ``source='live'`` so the Trading Floor can split real
    traffic from the synthetic firehose; ``session_id`` is the conversation it
    belongs to (for dedup jump-links). ``reasoning_tokens`` is the thinking
    tokens the real call spent (0 if none/unknown), so a later dedup hit can
    credit the thinking the user already paid for. Returns the new query id."""
    qid = db.new_query_id()
    ts = db.now()
    # Stamp the row's isolation boundary from the running process (N1). Defaults
    # to 'local' (single tenant) so single-user reuse is unchanged; a per-org
    # gateway sets BURSAR_TENANT and every row it writes is scoped to that org.
    tenant = tenant if tenant is not None else tenant_id()
    # Embed with the active backend (hashing by default, semantic when opted in)
    # and tag the row with its embedding space so find_dup only compares like
    # with like. pack() is dimension-agnostic.
    emb = dedup.pack(embed.embed_vec(prompt))
    embed_model = embed.active_model()
    conn.execute(
        "INSERT INTO queries (id, team, tenant, source, session_id, turn_id, prompt, embedding, embed_model, value, tier, rationale, "
        "est_tokens, reasoning_tokens, est_cost, fee, vpt, chosen_model, status, dedup_of, temporal_class, reuse_mode, result, "
        "created_at, decided_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (qid, team, tenant, source, session_id, turn_id, prompt, emb, embed_model, value, tier, rationale, est_tokens,
         int(reasoning_tokens or 0), est_cost, fee, vpt, model, status, dedup_of, temporal_class, reuse_mode, result, ts, ts),
    )
    return qid


def record_serviced(conn, *, team, prompt, model, tokens, token_cost,
                    value, tier, rationale, naive_cost=None,
                    requested_model=None, source="live", cost_source=None,
                    stripe_event_id=None, result=None, shadow=None,
                    session_id=None, reasoning_tokens=0, turn_id=None,
                    temporal_class=None, reuse_mode=None) -> str:
    """Record a real serviced LLM call with its TRUE tokens + cost.

    ``token_cost`` is the real $ (computed by the caller via
    ``agent.usage_pricing``); ``tokens`` the real token count. We add the flat
    ``TRADING_FEE`` and write a serviced query + ledger row + budget increment +
    ``serviced`` event, mirroring the dispatcher so live rows reconcile on the
    same ledger. ``requested_model`` (when it differs from ``model``) records a
    down-route. ``reasoning_tokens`` (thinking tokens, 0 if none/unknown) is
    stored so a later dedup hit can credit the thinking already paid for.
    Returns the new query id.
    """
    fee = fee_for(token_cost)
    spend = round(float(token_cost) + fee, 6)
    worth = value_usd(value)
    vpt = round(worth / spend, 4) if spend > 0 else float("inf")
    result_text = result if result is not None else (
        f"[serviced via {model}] answer to: {prompt[:80]}"
    )
    ensure_live_budget(conn, team)
    with db.write_txn(conn):
        qid = _insert_decided_query(
            conn, team=team, prompt=prompt, value=value, tier=tier,
            rationale=rationale, est_tokens=tokens, est_cost=token_cost,
            fee=fee, vpt=vpt, model=model, status="serviced", result=result_text,
            source=source, session_id=session_id, reasoning_tokens=reasoning_tokens,
            turn_id=turn_id, temporal_class=temporal_class, reuse_mode=reuse_mode,
        )
        conn.execute(
            "INSERT INTO ledger (query_id, team, source, model, tokens, token_cost, fee, "
            "total, stripe_event_id, settled, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (qid, team, source, model, tokens, token_cost, fee, spend,
             stripe_event_id, 1 if stripe_event_id else 0, db.now()),
        )
        conn.execute(
            "UPDATE budgets SET spent = spent + ?, updated_at=? WHERE team=?",
            (spend, db.now(), team),
        )
        payload = {
            "team": team, "model": model, "value": value, "tier": tier,
            "est_cost": token_cost, "fee": fee, "total": spend, "vpt": vpt,
            "stripe_event_id": stripe_event_id, "source": source, "live": True,
        }
        if cost_source:
            payload["cost_source"] = cost_source
        if requested_model and requested_model != model:
            payload["requested_model"] = requested_model
            payload["down_routed"] = True
        if naive_cost is not None:
            payload["naive_cost"] = naive_cost
            payload["saved_routing"] = round(max(0.0, float(naive_cost) - float(token_cost)), 6)
        if shadow:
            # "Would have" actions Bursar computed but did NOT enforce (F5), or a
            # down-route it wanted but had no safe target for (F3). Legible on the
            # floor without affecting what was actually billed.
            payload["shadow"] = shadow
        db.emit_event(conn, "serviced", query_id=qid, payload=payload)
    return qid


def record_deduped(conn, *, team, prompt, orig_id, sim, saved,
                   cached_result=None, value=None, tier=None,
                   rationale=None, est_tokens=None, est_cost=None,
                   source="live", session_id=None,
                   temporal_class=None, reuse_mode="serve") -> str:
    """Record a real call served from cache (a near-duplicate). No inference ran,
    so there's no ledger charge — just a deduped query row + ``deduped`` event
    carrying the $ saved. Value/tier are scored if not supplied (cheap).

    ``est_tokens``/``est_cost`` are the WOULD-RUN economics (what this call would
    have cost had it actually run, right-sized) — stored on the query row, NOT
    the ledger. This mirrors the dispatcher's ``_mark_deduped`` so the dashboard
    can value the reuse: the floor's naive-baseline + dedup-saved read these off
    the row. Storing 0 here (the old behaviour) made the floor treat every reused
    call as if it had been free to begin with, silently under-reporting savings.
    Spend is still untouched — there is no ledger row for a deduped call."""
    if value is None or tier is None or rationale is None:
        vs = scoring.score(prompt)
        value = vs.value if value is None else value
        tier = vs.tier if tier is None else tier
        rationale = vs.rationale if rationale is None else rationale
    if est_tokens is None or est_cost is None:
        est = pricing.estimate(prompt, tier)
        est_tokens = est.tokens if est_tokens is None else est_tokens
        est_cost = est.est_cost if est_cost is None else est_cost
    with db.write_txn(conn):
        qid = _insert_decided_query(
            conn, team=team, prompt=prompt, value=value, tier=tier,
            rationale=rationale, est_tokens=est_tokens, est_cost=est_cost, fee=0.0,
            vpt=0.0, model=None, status="deduped", dedup_of=orig_id,
            result=cached_result, source=source, session_id=session_id,
            temporal_class=temporal_class, reuse_mode=reuse_mode,
        )
        db.emit_event(conn, "deduped", query_id=qid, payload={
            "team": team, "dedup_of": orig_id, "similarity": round(float(sim), 3),
            "saved": round(float(saved), 6), "source": source, "live": True,
        })
    return qid


# --------------------------------------------------------------------------
# Dedup-serve synthesis (F4)
# --------------------------------------------------------------------------
#
# When armed and a near-duplicate of a recently-serviced call is found WITH a
# cached answer, the gate returns that answer as a synthetic provider response
# WITHOUT making a real API call. Only safe when the active api_mode is
# OpenAI-compatible (the loop normalizes the response via the chat_completions
# transport, which reads ``response.choices[0].message.content`` + ``.usage``).
# For other api_modes (anthropic_messages/codex_responses/bedrock_converse) the
# shape differs, so we SHADOW instead (record the would-be saving, run for real).

# api_mode values that the chat_completions transport handles (nous = "").
_SYNTHESIZABLE_API_MODES = {"", "chat_completions"}


def is_synthesizable_api_mode(api_mode: Optional[str]) -> bool:
    return (api_mode or "").strip().lower() in _SYNTHESIZABLE_API_MODES


# H5 — freshness-intent / external-live signals. A question asking for something
# time-sensitive or live (a score, a price, "latest", "right now") cannot be
# safely served from a cached answer: there is no local file to hash, so the
# cache-the-work freshness check comes back empty and we'd risk replaying a stale
# answer as current. When any of these fire the gate SKIPS dedup-augment and runs
# the turn fresh. The bias is intentional and matches the H5 spec ("when in doubt
# about freshness, do NOT dedup-serve"): a false positive only costs a reuse we
# could have made; a false negative serves stale data. Word boundaries keep "now"
# out of "knowledge".
#
# DELIBERATELY EXCLUDED: price/pricing, score/scoring, status, live. The H5 spec
# lists "price/score/status" as freshness signals, but those words ARE Bursar's
# own domain vocabulary (live gate, live pricing, value scoring, query status) —
# including them would false-bypass legit dedupable questions about Bursar's own
# code ("explain the pricing model", "how does the live gate work"), losing demo
# hits. The genuinely time-sensitive cases ("latest price", "score right now")
# are still caught by the temporal markers below, so freshness coverage holds.
_FRESHNESS_INTENT = re.compile(
    r"\b("
    r"latest|newest|current(?:ly)?|now|today|tonight|yesterday|tomorrow|"
    r"as of|real[\s-]?time|up[\s-]?to[\s-]?date|"
    r"weather|temperature|trending|winning|"
    r"this\s+(?:week|month|morning|year)"
    r")\b",
    re.IGNORECASE,
)


def is_freshness_intent(prompt: Optional[str]) -> bool:
    """True if the question asks for time-sensitive / external-live data that a
    cached answer could render stale (H5). Conservative by design: when this fires
    Bursar declines to dedup-serve and runs the turn fresh.

    NOTE (T-series): this is the OLD binary freshness veto. It conflates EVOLVING
    knowledge ("the latest RAG strategies") with LIVE data ("the latest BTC
    price") — both trip the regex — so it wrongly blocked legitimate research
    re-asks from deduping (the in-app bug that motivated the temporal router).
    The router now uses :func:`classify_temporal` instead; this is kept only as a
    secondary signal inside the heuristic fallback and for back-compat callers."""
    return bool(prompt and _FRESHNESS_INTENT.search(prompt))


# --------------------------------------------------------------------------
# T1 — Temporal-class classifier
# --------------------------------------------------------------------------
#
# Freshness is NOT a uniform property of a query — it's a per-query TEMPORAL
# CLASS. Four classes, each with a reuse strategy the router (bursar_gate)
# applies:
#
#   timeless  — never changes ("how does vector RAG work", definitions, settled
#               math/history) → SERVE if the original was cheap / AUGMENT-plain
#               if expensive (the existing H12 path). No freshness risk.
#   evolving  — drifts over weeks–months ("the latest RAG strategies", "state of
#               batteries 2026", current best practices) → AUGMENT-COMPARISON:
#               inject the prior as a historical baseline, re-derive FRESH, surface
#               the diff. This is the unlock — augment always runs a fresh call, so
#               it is never stale, and the prior becomes "what changed since".
#   live      — changes in seconds–minutes (a price, a score, weather, "is the
#               server up", breaking news) → AUGMENT-COMPARISON only if a very
#               recent prior exists, else FRESH. NEVER serve a cached value as
#               current.
#   stateful  — depends on the asker's private/session state ("my calendar", "my
#               last commit") → FRESH; a cached answer is likely already invalid.
#
# The classifier is Hermes-native (primary = the auxiliary LLM client, the same
# brain bursar_model uses to value queries). When that isn't wired (standalone
# runs, no provider) it falls back to a regex + volatile-noun heuristic. The
# fail-safe direction is EVOLVING: an uncertain classification must never produce
# a stale SERVE, and augment-comparison (a fresh call) is always safe, so when in
# doubt we route to augment, never to serve-stale.

TEMPORAL_CLASSES = ("timeless", "evolving", "live", "stateful")

# Default when we genuinely cannot tell. EVOLVING → augment-comparison → fresh
# call → never stale. This is the safe direction (see module note above).
_TEMPORAL_DEFAULT = "evolving"

# Volatile-data nouns come in two tiers, because some are unambiguously external
# real-time data and some collide with Bursar's OWN domain vocabulary.
#
# TIER A — unambiguously real-time. Their mere presence marks a query LIVE: there
# is no Bursar-domain reading of "weather" or "the score of the game". These never
# appear in a question about Bursar's code.
_VOLATILE_HARD = re.compile(
    r"\b("
    r"weather|temperature|forecast|"
    r"standings?|odds|"
    r"news|headlines?|breaking|"
    r"traffic|outage|"
    r"stock|stocks|shares?|ticker|"
    r"exchange\s+rate|interest\s+rate|"
    r"election|polls?"
    r")\b",
    re.IGNORECASE,
)
# TIER B — domain-OVERLOADED. "price/pricing", "score/scoring", "status", "live"
# are Bursar's own vocabulary (live gate, value scoring, query status, live
# pricing). On their own they must NOT mark a query live, or every question ABOUT
# Bursar's code ("explain the pricing model", "how does the live gate work") would
# false-fire and lose its dedup. They mark LIVE only when ADJACENT to a freshness
# word ("current price", "score right now") — real-time intent, not the domain.
_VOLATILE_SOFT = re.compile(
    r"\b("
    r"price|prices|pricing|quote|"
    r"score|scores|"
    r"status|uptime|live"
    r")\b",
    re.IGNORECASE,
)

# First-person / session-state markers → STATEFUL. The cached answer is about a
# different point in the asker's private state, so it's likely invalid.
_STATEFUL_INTENT = re.compile(
    r"\b("
    r"my\s+(?:calendar|schedule|inbox|email|emails|messages|tasks?|todos?|"
    r"commits?|branch|repo|files?|account|balance|orders?|meetings?|notes?)|"
    r"do\s+i\s+have|what'?s\s+on\s+my|my\s+last|am\s+i\b|are\s+my\b"
    r")\b",
    re.IGNORECASE,
)

# N4 — conversational deixis. A follow-up that refers back to THIS conversation
# ("continue", "do the same for X", "what did we decide", "as we discussed") is
# stateful: its answer depends on the dialogue so far, not on a standalone
# question. Such a prompt can still semantically match a prior turn, so without
# this it could be SERVED a cached answer that ignores the conversation. Marking
# it stateful routes it to FRESH (never serve, never augment) — the safe
# direction: the only cost of a false positive is a missed reuse, while a false
# negative would replay a context-blind answer.
_STATEFUL_DEIXIS = re.compile(
    r"\b("
    r"continue\b|keep\s+going|carry\s+on|go\s+on\b|proceed\b|go\s+ahead|"
    r"do\s+(?:the\s+)?same|same\s+(?:thing\s+)?for\b|same\s+as\s+(?:before|above)|"
    r"what\s+did\s+we\s+(?:decide|say|choose|agree|conclude)|"
    r"as\s+(?:we\s+)?(?:discussed|mentioned|agreed|said|noted|established)|"
    r"as\s+above|like\s+(?:we|you)\s+said|"
    r"you\s+(?:just\s+)?(?:said|mentioned|suggested)|earlier\s+you\s+said|"
    r"the\s+(?:previous|last)\s+(?:one|answer|response|step|point)|"
    r"from\s+(?:before|earlier)|per\s+our\b|"
    r"next\s+step|finish\s+(?:it|that|the)"
    r")\b",
    re.IGNORECASE,
)


def is_stateful_intent(prompt: Optional[str]) -> bool:
    """True if the prompt depends on the asker's private/session state OR on this
    conversation's own history (N4 deixis). Such prompts must never be served a
    cached answer — they're routed to the 'stateful' temporal class (run fresh)."""
    if not prompt:
        return False
    return bool(_STATEFUL_INTENT.search(prompt) or _STATEFUL_DEIXIS.search(prompt))

# In-process cache: classification is deterministic per prompt text, and a re-ask
# (the only case the class matters for) has the SAME prompt as the original, so a
# tiny LRU-ish dict means the aux LLM is hit at most once per distinct question.
_TEMPORAL_CACHE: "dict[str, str]" = {}
_TEMPORAL_CACHE_MAX = 512

_TEMPORAL_SYSTEM = (
    "You are a temporal-class classifier for a query cache. Decide how quickly the "
    "ANSWER to a question goes out of date, so the cache knows whether a prior "
    "answer can be reused. Respond with ONLY a JSON object {\"class\": \"<value>\"} "
    "where <value> is exactly one of:\n"
    "- timeless: the answer never changes (how something works, definitions, "
    "settled math/history, explanations of code or concepts).\n"
    "- evolving: the answer drifts over weeks or months (the latest best practices, "
    "the current state of a field, recent — but not real-time — developments).\n"
    "- live: the answer is a single real-time datapoint that changes by the "
    "second or minute (a price, a score, the weather, a current system status, a "
    "breaking-news headline).\n"
    "- stateful: the answer depends on the asker's own private or session data "
    "(their calendar, their files, their last action), OR it is a continuation of "
    "THIS conversation rather than a standalone question — e.g. \"continue\", "
    "\"do the same for X\", \"what did we decide\", \"as we discussed\", \"finish "
    "that\". A continuation depends on the dialogue so far, so a cached answer to a "
    "similar-looking question would be wrong: classify it stateful.\n"
    "Classify by the nature of the answer, not by recency words. Phrases like "
    "\"right now\", \"currently\", \"as of 2026\", or \"in production today\" do "
    "NOT make a question live — an analysis, write-up, comparison, or "
    "best-practices question is EVOLVING even when it asks for the current state "
    "(e.g. \"the latest RAG strategies teams use right now\" or \"a technical "
    "write-up of the main approaches in production\" → evolving). Reserve LIVE for "
    "a question whose answer is a specific live value that would be wrong minutes "
    "later. Output only the JSON."
)


def _temporal_heuristic(prompt: str) -> str:
    """Deterministic fallback classifier (no LLM). Precedence: stateful > live >
    evolving > timeless. Biases AWAY from timeless when any time-signal is present,
    because a wrong 'timeless' is the only error that can serve stale data."""
    p = prompt or ""
    if is_stateful_intent(p):  # private/session state OR conversational deixis (N4)
        return "stateful"
    has_fresh = bool(_FRESHNESS_INTENT.search(p))
    # TIER A volatile noun → real-time on its own ("the weather", "the score").
    if _VOLATILE_HARD.search(p):
        return "live"
    # TIER B (domain-overloaded) noun → live ONLY when a freshness word co-occurs
    # ("current price", "score right now"). Alone it's a question about the domain,
    # not a request for live data — keep it dedupable.
    if has_fresh and _VOLATILE_SOFT.search(p):
        return "live"
    # A freshness word ALONE ("latest", "current", "now", "2026") with no volatile
    # noun → evolving knowledge, not live data. This is the exact case the old veto
    # got wrong: it vetoed dedup here instead of routing to augment-comparison.
    if has_fresh:
        return "evolving"
    return "timeless"


def _aux_classify_temporal(prompt: str) -> Optional[str]:
    """Classify via Hermes's auxiliary LLM client (the Hermes-native path, same
    client bursar_model uses to value queries). Returns a valid class string, or
    None if the aux client isn't wired / errors / returns garbage — the caller
    then falls back to the heuristic. Makes one cheap, deterministic call."""
    try:
        from agent.auxiliary_client import get_text_auxiliary_client  # type: ignore
    except Exception:
        return None  # not running inside Hermes (standalone / tests)
    try:
        client, model = get_text_auxiliary_client("bursar_temporal")
    except Exception:
        return None
    if client is None or not model:
        return None
    try:
        from agent.auxiliary_client import get_auxiliary_extra_body  # type: ignore
        extra = get_auxiliary_extra_body() or None
    except Exception:
        extra = None
    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": _TEMPORAL_SYSTEM},
                {"role": "user", "content": (prompt or "")[:2000]},
            ],
            temperature=0,
            max_tokens=32,
            extra_body=extra,
        )
        content = resp.choices[0].message.content or ""
    except Exception:
        return None
    return _parse_temporal_class(content)


def _parse_temporal_class(content: str) -> Optional[str]:
    """Pull a valid temporal class out of an aux-LLM response. Tries JSON first,
    then a bare-word scan. Returns None (→ heuristic/fail-safe) on no match."""
    import json

    text = (content or "").strip()
    # JSON object anywhere in the text.
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        try:
            obj = json.loads(text[start:end + 1])
            cls = str(obj.get("class", "")).strip().lower()
            if cls in TEMPORAL_CLASSES:
                return cls
        except Exception:
            pass
    # Bare-word fallback: the model answered with just the class.
    low = text.lower()
    for cls in TEMPORAL_CLASSES:
        if re.search(rf"\b{cls}\b", low):
            return cls
    return None


def classify_temporal(prompt: Optional[str]) -> str:
    """Return the temporal class of *prompt* — one of TEMPORAL_CLASSES.

    Primary: the Hermes auxiliary LLM (Hermes-native). Fallback: a regex +
    volatile-noun heuristic when the aux client is unavailable. Fail-safe: when
    even the heuristic can't decide (empty prompt), default to EVOLVING — augment
    runs a fresh call, so it is never stale. Cached per prompt text (the aux LLM is
    hit at most once per distinct question, and a re-ask reuses the original's
    class for free). Never raises."""
    key = prompt.strip() if isinstance(prompt, str) else ""
    if not key:
        return _TEMPORAL_DEFAULT
    cached = _TEMPORAL_CACHE.get(key)
    if cached is not None:
        return cached
    cls = None
    try:
        cls = _aux_classify_temporal(key)
    except Exception:
        cls = None
    if cls not in TEMPORAL_CLASSES:
        # Aux unavailable or unsure → deterministic heuristic (never raises).
        cls = _temporal_heuristic(key)
    if cls not in TEMPORAL_CLASSES:
        cls = _TEMPORAL_DEFAULT
    if len(_TEMPORAL_CACHE) >= _TEMPORAL_CACHE_MAX:
        _TEMPORAL_CACHE.clear()  # cheap bound; classes are cheap to recompute
    _TEMPORAL_CACHE[key] = cls
    return cls


def synthesize_chat_response(content: str, model: Optional[str] = None):
    """Build a minimal OpenAI ChatCompletion-shaped object the loop's
    chat_completions transport can normalize. Zero-token usage (no real call).

    Mirrors exactly the attributes ``ChatCompletionsTransport.normalize_response``
    touches: ``choices[0].message.{content,tool_calls}`` + ``choice.finish_reason``
    + ``response.usage.{prompt,completion,total}_tokens`` + ``response.model``.
    """
    from types import SimpleNamespace

    message = SimpleNamespace(
        content=content,
        tool_calls=None,
        role="assistant",
        reasoning=None,
        reasoning_content=None,
        reasoning_details=None,
        refusal=None,
    )
    choice = SimpleNamespace(message=message, finish_reason="stop", index=0)
    usage = SimpleNamespace(prompt_tokens=0, completion_tokens=0, total_tokens=0,
                            prompt_tokens_details=None)
    return SimpleNamespace(choices=[choice], usage=usage,
                           model=model or "bursar-dedup-cache")


# --------------------------------------------------------------------------
# Down-route target resolution (F3)
# --------------------------------------------------------------------------

def cheap_target(provider: str, requested_model: Optional[str] = None) -> Optional[str]:
    """Return a cheaper model for *provider*, or ``None`` if none is known.
    Never returns the same model that was requested. Whether the target is
    *safe to actually swap to* is a separate question — see ``downroute_target``."""
    override = os.environ.get("BURSAR_CHEAP_MODEL", "").strip()
    target = override or _CHEAP_BY_PROVIDER.get((provider or "").strip().lower())
    if not target:
        return None
    if requested_model and target == requested_model:
        return None
    return target


# Providers whose built-in cheap-model ids are confirmed (real catalog ids the
# provider definitely accepts). nous is intentionally absent: its portal model
# namespace must be confirmed by the user, so its default cheap id is treated as
# UNVERIFIED and down-route shadows until BURSAR_CHEAP_MODEL is set.
_TRUSTED_DOWNROUTE_PROVIDERS = {"anthropic", "openai", "openai-codex"}


def downroute_target(provider: str, requested_model: Optional[str] = None) -> tuple[Optional[str], bool, str]:
    """Plan a down-route. Returns ``(target, trusted, reason)``.

    ``trusted`` is True only when we're confident the provider will accept
    ``target`` — i.e. the user set ``BURSAR_CHEAP_MODEL`` (vouching for it), set
    ``BURSAR_NOUS_CHEAP_MODEL`` for nous, or the provider is one with confirmed
    built-in ids. Untrusted targets are SHADOWED (recorded as "would have", real
    call runs) so an unverified model id can never break a turn. This is the
    "validate-before-arm" safety rail for the model-swap path.
    """
    target = cheap_target(provider, requested_model)
    if not target:
        return None, False, "no cheaper target known for provider"
    p = (provider or "").strip().lower()
    if os.environ.get("BURSAR_CHEAP_MODEL", "").strip():
        return target, True, "user-set BURSAR_CHEAP_MODEL"
    if p == "nous" and os.environ.get("BURSAR_NOUS_CHEAP_MODEL", "").strip():
        return target, True, "user-set BURSAR_NOUS_CHEAP_MODEL"
    if p in _TRUSTED_DOWNROUTE_PROVIDERS:
        return target, True, "confirmed provider catalog id"
    return target, False, (
        f"unverified cheap id {target!r} for provider {p!r}; set "
        "BURSAR_CHEAP_MODEL (or BURSAR_NOUS_CHEAP_MODEL) to enable real down-route"
    )
