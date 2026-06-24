"""Bursar live FinOps gate — a native Hermes *agent* plugin.

This is the F-series: it points Bursar at the user's REAL Hermes traffic (every
chat turn, every subagent, every tool-internal LLM call) instead of only the
synthetic firehose. It runs **in the agent process** (where the LLM calls are
actually made), unlike the Bursar *dashboard* plugin which only reads the DB.

Two integration points, both native (zero core fork):

  * ``post_api_request`` hook  — ALWAYS-ON observability. After every real call
    we record the TRUE tokens/cost into ``bursar_db`` (the same DB the desktop
    Trading Floor reads over the E4 ``/events`` WebSocket), so real traffic
    lights up the floor live, attributed to a ``live:*`` team.

  * ``llm_execution`` middleware — ENFORCEMENT, and only when explicitly armed
    (``BURSAR_ENFORCE=1``). Disarmed it is a strict pass-through. Armed it may
    dedup-serve from cache or down-route trivial calls — both still return an
    answer, so Bursar never hard-refuses the user. Budget-stop / reject-worthless
    are computed but SHADOW-only (shown as "would have", never enforced).

Safety rails (non-negotiable):
  1. ``BURSAR_ENFORCE`` defaults OFF → observe + record only.
  2. Kill switch → unset the env var (or disable the plugin) = stock Hermes.
  3. Fail-open → every gate/record path is wrapped; a Bursar error is invisible
     to the chat. The middleware chain itself also falls through to the real
     call if the callback raises before ``next_call`` (hermes_cli/middleware.py).

F0 (this commit): scaffold + register + strict no-op disarmed. The recorder and
the enforcement branches are wired as safe stubs and filled in by F2/F3/F4.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import threading
import time
from pathlib import Path

log = logging.getLogger("bursar_gate")

# Per-call hand-off from the middleware (which sees the request/prompt) to the
# post_api_request hook (which sees the true usage). Keyed by api_request_id —
# a stable unique id passed to BOTH sites (conversation_loop.py). Bounded so a
# crashed/never-fired hook can't leak memory in a long-lived agent process.
_PENDING: "dict[str, dict]" = {}
_PENDING_LOCK = threading.Lock()
_PENDING_MAX = 256


def _stash(key: str, value: dict) -> None:
    if not key:
        return
    with _PENDING_LOCK:
        if len(_PENDING) >= _PENDING_MAX:
            # Drop the oldest ~half — these are observability breadcrumbs, not
            # correctness-critical; losing a few never affects a chat.
            for k in list(_PENDING)[: _PENDING_MAX // 2]:
                _PENDING.pop(k, None)
        _PENDING[key] = value


def _unstash(key: str):
    if not key:
        return None
    with _PENDING_LOCK:
        return _PENDING.pop(key, None)


# A turn's canonical question, keyed by turn_id (FIRST call of the turn wins).
# Why: ``prompt_from_request`` reads the LAST message, but Hermes overwrites that
# mid-turn with injected tool-result / system-retry USER-role messages (e.g. a
# read_file that timed out becomes "[System: Your previous tool call ...]"). That
# made the turn's final answer get recorded under a drifted key instead of the
# user's actual question — so a later re-ask matched only the early narration
# fragments and the agent redid the whole search. Pinning the question to turn_id
# on first sighting keeps EVERY call of the turn (including the one that finally
# holds the answer) filed under the real question, and makes "is this the opening
# call?" robust against those injected pseudo-user messages.
# tid -> (canonical_question, last_access_monotonic). Eviction is by LAST ACCESS
# age (N2), NOT a count cap and NOT first-seen age. Rationale: the count-256 cap
# could evict a long loop's turn_id mid-flight under concurrency (>256 simultaneous
# turns) → a later internal call mis-seen as first_call → dedup fires MID-LOOP and
# derails it. A first-seen TTL would be just as wrong: a turn legitimately running
# longer than the TTL would evict itself. Last-access TTL is the correct policy —
# every call of a turn touches its entry (refreshing last_access), so an ACTIVE
# turn never ages out no matter how long it runs; only a turn that has been SILENT
# for the whole TTL is reclaimed. _TURN_Q_TTL is therefore the max plausible GAP
# between two calls of one turn (default 1h — no agent loop pauses that long between
# internal calls), not the max turn duration. _TURN_Q_MAX is a memory backstop only.
_TURN_Q: "dict[str, tuple[str, float]]" = {}
_TURN_Q_LOCK = threading.Lock()
_TURN_Q_TTL = float(os.environ.get("BURSAR_TURN_TTL_S", "3600"))  # silence-between-calls reclaim window
_TURN_Q_MAX = int(os.environ.get("BURSAR_TURN_Q_MAX", "20000"))   # backstop ceiling, not the eviction mechanism


_WARNED_NO_TURN_ID = False


def _warn_no_turn_id_once() -> None:
    """Emit a single warning the first time an llm_execution call arrives with no
    turn_id (N3). Bursar still works — it uses the hardened last-message fallback —
    but turn_id grouping is the robust path, so a sustained absence is worth a flag.
    Guarded so it can never spam a long-running agent process."""
    global _WARNED_NO_TURN_ID
    if not _WARNED_NO_TURN_ID:
        _WARNED_NO_TURN_ID = True
        log.warning("bursar_gate: llm_execution call had no turn_id — using the "
                    "last-message turn-opening fallback (hardened, but weaker than "
                    "turn_id grouping). If this persists, a Hermes path may have "
                    "stopped threading turn_id.")


def _turn_question(turn_id: str, prompt: str) -> "tuple[str, bool]":
    """Return ``(canonical_question, is_first_call)`` for *turn_id*.

    First call of a turn pins its prompt as the question; later calls reuse it
    (ignoring the drifted last-message prompt). With no turn_id we can't group,
    so fall back to the call's own prompt and let the caller decide openness."""
    tid = (turn_id or "").strip()
    if not tid or not prompt:
        return prompt, False
    now = time.monotonic()
    with _TURN_Q_LOCK:
        # Reclaim turns silent past the TTL. Entries are kept in ascending
        # last-access order (move-to-end on every touch below), so the oldest
        # sit at the front — pop from the front and stop at the first still-fresh
        # entry. O(number actually expired), so this stays cheap per call.
        while _TURN_Q:
            k = next(iter(_TURN_Q))
            if now - _TURN_Q[k][1] > _TURN_Q_TTL:
                _TURN_Q.pop(k, None)
            else:
                break
        first = tid not in _TURN_Q
        if first:
            # Backstop only — TTL above should keep us far below this. If a flood
            # of concurrent turns still blows the ceiling, drop the oldest half
            # (front = least-recently-accessed) so active turns survive.
            if len(_TURN_Q) >= _TURN_Q_MAX:
                for k in list(_TURN_Q)[: _TURN_Q_MAX // 2]:
                    _TURN_Q.pop(k, None)
            _TURN_Q[tid] = (prompt, now)
        else:
            q = _TURN_Q.pop(tid)[0]      # touch: move to MRU end, refresh last_access
            _TURN_Q[tid] = (q, now)
        return _TURN_Q[tid][0], first


# ---------------------------------------------------------------------------
# Engine import — mirror the dashboard plugin: put the Bursar skill's scripts
# dir on sys.path so we can import the shared engine modules by name.
#
#   <repo>/plugins/bursar_gate/__init__.py        <- this file
#   <repo>/optional-skills/finance/bursar/scripts/ <- the engine
#
# parents[2] of this file is <repo> (bursar_gate -> plugins -> repo).
# ``BURSAR_SCRIPTS`` overrides the location (matches the dashboard plugin).
# ---------------------------------------------------------------------------

def _engine_scripts_dir() -> Path:
    env = os.environ.get("BURSAR_SCRIPTS")
    if env:
        return Path(env).expanduser()
    here = Path(__file__).resolve()
    # Self-contained install: the engine ships either co-located in this gate
    # plugin (./engine) or in the sibling dashboard plugin
    # (~/.hermes/plugins/bursar/engine) — one engine copy serves both.
    for cand in (here.parent / "engine", here.parents[1] / "bursar" / "engine"):
        if cand.is_dir():
            return cand
    repo = here.parents[2]
    return repo / "optional-skills" / "finance" / "bursar" / "scripts"


def _ensure_engine_on_path() -> bool:
    """Insert the engine scripts dir on sys.path. Returns True if it exists.

    Never raises — an import problem must not break plugin load (and therefore
    must not affect the agent), so callers treat a False here as "disarmed".
    """
    try:
        scripts = _engine_scripts_dir()
        if scripts.is_dir():
            sp = str(scripts)
            if sp not in sys.path:
                sys.path.insert(0, sp)
            return True
        log.warning("bursar_gate: engine scripts dir not found at %s", scripts)
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("bursar_gate: could not locate engine scripts (%s)", exc)
    return False


# ---------------------------------------------------------------------------
# Runtime config helpers
# ---------------------------------------------------------------------------

_TRUTHY = {"1", "true", "yes", "on"}
_FALSEY = {"0", "false", "no", "off"}


def _truthy(raw) -> bool:
    """True only when *raw* is an explicitly truthy string (else False). Used for
    default-OFF feature flags like cross-tenant augment — anything unset/blank/
    unrecognized stays off."""
    return (raw or "").strip().lower() in _TRUTHY


def _armed_db() -> bool:
    """The DB control flag (``control['enforce']``) — the desktop titlebar
    toggle. Cross-process: the dashboard writes it, we read it. Fail-safe to
    DISARMED on any error so a DB hiccup can never silently arm enforcement.
    """
    try:
        import bursar_db as db
        with db.connect_closing() as conn:
            val = db.get_control(conn, "enforce", "0")
        return (val or "").strip().lower() in _TRUTHY
    except Exception:
        return False


def _armed() -> bool:
    """True only when enforcement is explicitly turned on.

    Read live (per call) so the kill switch is instantaneous. Precedence:

      1. ``BURSAR_ENFORCE`` env var, when explicitly set (to a truthy OR falsey
         value), WINS — a terminal operator's launch flag is authoritative and
         cannot be overridden from the UI.
      2. Otherwise (env unset/blank, the desktop default) honor the DB control
         flag the titlebar toggle writes.

    Either way the read is live per call, so unsetting the env / flipping the
    toggle returns Hermes to stock behavior on the very next call.
    """
    raw = os.environ.get("BURSAR_ENFORCE")
    if raw is not None and raw.strip() != "":
        return raw.strip().lower() in _TRUTHY
    return _armed_db()


def _team_for(session_id: str) -> str:
    """Attribution: explicit BURSAR_TEAM, else ``live:<last6 of session>``.

    Subagents get their own session_id, so they naturally surface as their own
    "team" on the floor.
    """
    explicit = os.environ.get("BURSAR_TEAM", "").strip()
    if explicit:
        return explicit
    sid = (session_id or "").strip()
    return f"live:{sid[-6:]}" if sid else "live:unknown"


# ---------------------------------------------------------------------------
# Hook + middleware callbacks
# ---------------------------------------------------------------------------

def _text_of(content):
    """Flatten an assistant message's ``content`` to plain text.

    Handles the three shapes a provider can hand back: a plain string, a list
    of content parts (dicts with ``text``/``content`` or bare strings or objects
    with a ``.text`` attr), or None. Returns a stripped string or None."""
    if isinstance(content, str):
        return content.strip() or None
    if isinstance(content, list):
        parts = []
        for p in content:
            if isinstance(p, str):
                parts.append(p)
            elif isinstance(p, dict):
                parts.append(p.get("text") or p.get("content") or "")
            else:
                parts.append(getattr(p, "text", "") or "")
        joined = "".join(parts).strip()
        return joined or None
    return None


def _inject_context(request, block: str) -> bool:
    """Insert a system message carrying *block* just before the last user message.

    This is how dedup-AUGMENT works: instead of replacing the LLM response with a
    canned answer (substitute-and-stop), we add the prior answer + skip-the-lookup
    instructions to the request and let the real call run, cheaply, with the agent
    staying live. Placed right before the user's prompt so it reads as background
    the model uses to answer. Returns True if injected."""
    msgs = request.get("messages")
    if not isinstance(msgs, list) or not msgs:
        return False
    idx = len(msgs)
    for i in range(len(msgs) - 1, -1, -1):
        m = msgs[i]
        if isinstance(m, dict) and m.get("role") == "user":
            idx = i
            break
    msgs.insert(idx, {"role": "system", "content": block})
    return True


def _extract_answer(assistant_message):
    """The assistant's real, user-facing answer text, to cache for dedup-serve.

    ONLY the visible ``content`` (string or list-of-parts) — deliberately NOT
    ``reasoning``/``reasoning_content``. A reasoning+tool agent emits several API
    calls per turn; the planning steps have empty ``content`` (just thinking + a
    tool call), and caching that reasoning made dedup replay a half-finished
    thought instead of the answer. Empty content → None, so that step records a
    placeholder and is never dedup-served (the servable-answer guard drops it)."""
    if assistant_message is None:
        return None
    return _text_of(getattr(assistant_message, "content", None))


def _record(*, usage=None, model=None, response_model=None, provider=None,
            base_url=None, session_id="", api_request_id="",
            api_call_count=0, assistant_message=None, **_) -> None:
    """``post_api_request`` hook — record the TRUE tokens/cost of a real call.

    Always-on observability (independent of BURSAR_ENFORCE). Reconciles with the
    prompt/quote the middleware stashed for this ``api_request_id``, computes the
    real cost via ``bursar_live.true_cost`` (native-first, market-rate fallback),
    and writes a serviced query + ledger + event into ``bursar_db`` so the
    desktop Trading Floor lights up live. Fully wrapped — a recorder error can
    never surface to the chat.
    """
    try:
        pending = _unstash(api_request_id)
        if pending is None:
            # No stashed prompt (middleware didn't run, or a retry path) — we
            # can't score/dedup without it, so skip rather than guess.
            return
        if pending.get("served_dedup"):
            # The gate already served this from cache and recorded a deduped
            # row; there's no real call to bill.
            return
        if not isinstance(usage, dict) or not usage:
            return

        import bursar_db as db
        import bursar_live as live

        prompt = pending["prompt"]
        team = pending["team"]
        quote = pending["quote"]
        requested_model = pending.get("requested_model")
        # The model that ACTUALLY served the call. The hook's `model` kwarg is
        # the agent's configured model, NOT our per-call down-routed override —
        # so prefer the gate's recorded target, then the provider's echoed
        # response_model, then the kwarg.
        used_model = pending.get("actual_model") or response_model or model or requested_model

        cost, tokens, cost_src = live.true_cost(
            used_model, usage, provider=provider, base_url=base_url,
            fallback_quote=quote,
        )
        if tokens <= 0:
            return  # nothing actually ran (or no usage reported)

        # Reasoning/thinking tokens (reasoning models only) — the bulk of a
        # reasoning call's cost. Stored so a later dedup hit can credit the
        # thinking already paid for. 0 when the provider reports none.
        reasoning_tokens = live.reasoning_tokens_from_usage(usage)

        # Cache the REAL answer so a later near-duplicate can be dedup-served
        # (F4). Without this the cached result is a placeholder and dedup-serve
        # would return non-answers.
        answer = _extract_answer(assistant_message)

        # Merge any "would have" markers the gate left (down-route had no safe
        # target; dedup couldn't synthesize this provider's shape).
        shadow = {}
        for k in ("shadow_downroute", "shadow_dedup"):
            if pending.get(k):
                shadow.update(pending[k])

        # Dedup-augment: this real (cheap) call reused a prior answer and skipped
        # the original rediscovery loop. Record the MEASURED savings (real ledger
        # tokens/cost of that earlier turn) on the row so the floor can show it.
        aug = pending.get("dedup_augmented")
        # T4 — persist the temporal class + reuse outcome on the row so the floor
        # can badge it. An augment row carries its class + mode ('comparison' for
        # evolving/recent-live, 'plain' for timeless); a turn that was classified
        # but ran fresh (live-stale / stateful / non-synth decline) carries the
        # class only (no reuse_mode), so "ran fresh — live" is legible too.
        temporal_class = None
        reuse_mode = None
        if aug:
            shadow["dedup_reused"] = True
            shadow["reused_of"] = aug.get("orig_id")
            shadow["reused_calls"] = aug.get("skipped_calls", 0)
            shadow["reused_tokens"] = aug.get("skipped_tokens", 0)
            shadow["reused_saved"] = aug.get("skipped_cost", 0.0)
            # N5 observability: prior size + how much was injected after the cap.
            shadow["prior_chars"] = aug.get("prior_chars")
            shadow["injected_chars"] = aug.get("injected_chars")
            shadow["prior_capped"] = aug.get("capped")
            temporal_class = aug.get("temporal_class")
            reuse_mode = aug.get("mode")
        elif pending.get("shadow_dedup"):
            temporal_class = pending["shadow_dedup"].get("temporal_class")

        spend = round(cost + quote.fee, 6)

        # S2 — settle this real call through Stripe (test-mode usage meter), the
        # same rail the firehose uses. Done BEFORE opening the DB connection so a
        # ~network round-trip never holds a write txn open. Fully fail-open: the
        # meter returns None on any trouble → the ledger row records settled=0.
        stripe_event_id = None
        meter = _get_meter()
        if meter is not None:
            # defer=True: the be_ identifier is generated client-side and returned
            # instantly (this row records settled=1 now); the MeterEvent.create
            # round-trip fires on a daemon thread so it adds ~0ms to the turn.
            stripe_event_id = meter(team, used_model, tokens, spend, defer=True)

        with db.connect_closing() as conn:
            # F5 — the two actions the user opted OUT of enforcing: compute what
            # Bursar WOULD do and surface it as "would have", without blocking.
            # reject-worthless: value below cost+fee (the value gate).
            if quote.net <= 0:
                shadow["would_reject"] = True
                shadow["reject_saving"] = spend
            # hard budget-stop: this call pushes the team past its cap. Read the
            # budget BEFORE record_serviced increments it.
            bud = db.get_budget(conn, team)
            if bud is not None and (bud["spent"] + spend) > bud["cap"] + 1e-9:
                shadow["would_budget_stop"] = True
                shadow["budget_stop_saving"] = spend
                shadow["team_cap"] = bud["cap"]

            live.record_serviced(
                conn, team=team, prompt=prompt, model=used_model,
                tokens=tokens, token_cost=cost,
                value=quote.value, tier=quote.tier, rationale=quote.rationale,
                naive_cost=quote.naive_cost,
                requested_model=requested_model, cost_source=cost_src,
                stripe_event_id=stripe_event_id,
                shadow=shadow or None, result=answer, session_id=session_id or None,
                reasoning_tokens=reasoning_tokens, turn_id=pending.get("turn_id"),
                temporal_class=temporal_class, reuse_mode=reuse_mode,
            )
            if aug:
                # Surface the reuse on the live feed: a real (cheap) call that
                # skipped the earlier turn's measured rediscovery work.
                db.emit_event(conn, "dedup_augment", payload={
                    "team": team, "orig_id": aug.get("orig_id"),
                    "similarity": aug.get("similarity"),
                    "reused_calls": aug.get("skipped_calls", 0),
                    "reused_tokens": aug.get("skipped_tokens", 0),
                    "reused_saved": aug.get("skipped_cost", 0.0),
                    "this_tokens": tokens, "this_cost": spend,
                    "temporal_class": aug.get("temporal_class"),
                    "mode": aug.get("mode"),
                    "cache_age_s": aug.get("cache_age_s"),
                })
                conn.commit()
        if log.isEnabledFor(logging.DEBUG):
            log.debug("bursar_gate recorded: team=%s model=%s tokens=%s cost=$%.5f (%s)",
                      team, used_model, tokens, cost, cost_src)
    except Exception as exc:  # pragma: no cover - defensive, must stay silent
        log.debug("bursar_gate: _record swallowed error: %s", exc)


# H12 — augment-and-continue runs a REAL (cheap) call, so it only pays off when
# the original turn was a genuine multi-call rediscovery loop whose skipped cost
# beats this call's cost. A cheap/single-call original has no loop to skip, so
# augmenting is net-negative (proven live: a trivial re-ask cost more to "reuse"
# than it saved). Below this call-count we substitute (pure cache-serve, $0 new
# spend) instead; env-overridable.
_AUGMENT_MIN_CALLS = int(os.environ.get("BURSAR_AUGMENT_MIN_CALLS", "2"))


def _gate(*, request, next_call, session_id="", api_request_id="",
          provider="", model="", api_mode="", **ctx):
    """``llm_execution`` middleware — capture + (when armed) enforce.

    Contract (hermes_cli/middleware.py): return ``next_call(request)`` to allow
    (optionally mutating ``request`` first to down-route), or return a synthetic
    response WITHOUT calling ``next_call`` to serve from cache.

    ALWAYS (armed or not) it captures the prompt + Bursar quote into ``_PENDING``
    so the post-call hook can record the real cost — that's how observability
    works even disarmed. Enforcement branches (dedup-serve F4, down-route F3)
    only run when ``BURSAR_ENFORCE`` is set; F2 is capture-only.

    Fail-open: if anything here raises BEFORE ``next_call``, the middleware chain
    runs the real call anyway, so a Bursar bug can never break a turn.
    """
    # --- capture for observability (best-effort; never blocks the call) ---
    pending = None
    quote = None
    turn_opening = False
    try:
        import bursar_live as live

        raw_prompt = live.prompt_from_request(request)
        # Pin the prompt to the turn's FIRST-seen question so a mid-turn injected
        # message (timed-out tool result, system retry) can't drift the dedup /
        # recording key off the user's real question. ``first_call`` is the
        # robust "this starts the turn" signal (turn_id-based), immune to those
        # injected pseudo-user messages that fool a last-message check.
        turn_id = ctx.get("turn_id") or ""
        prompt, first_call = _turn_question(turn_id, raw_prompt)
        if not turn_id:
            # N3 — degraded path: no turn_id, so "is this the turn opening?" falls
            # back to the last-message heuristic (now hardened against injected
            # pseudo-user messages, but still weaker than turn_id grouping). Warn
            # ONCE so an operator notices if a Hermes path stops threading turn_id.
            _warn_no_turn_id_once()
        turn_opening = first_call if turn_id else live.is_turn_opening_request(request)
        if prompt:
            team = _team_for(session_id)
            quote = live.quote(prompt, team)
            requested_model = request.get("model") or model
            pending = {
                "prompt": prompt, "team": team, "quote": quote,
                "requested_model": requested_model,
                "served_dedup": False, "turn_id": turn_id or None,
            }
            _stash(api_request_id, pending)
    except Exception as exc:
        log.debug("bursar_gate: capture skipped: %s", exc)

    if not _armed():
        return next_call(request)

    # --- enforcement (armed only) ---------------------------------------------
    # All wrapped so a bug here falls through to the real call (fail-open) —
    # Bursar never breaks a turn.
    if quote is not None and pending is not None:
        # F4: dedup-AUGMENT. On a genuine re-ask, hand the agent the prior answer
        # as CONTEXT (+ skip-the-lookup instructions) and let the real call run
        # once, cheaply — instead of substituting a canned answer and ending the
        # turn. The agent confirms it's a repeat, gives the full current answer,
        # notes any change, and stays live/resumable; the saving is the dozen
        # rediscovery calls the first ask paid for and this one skips.
        #
        # Gated on is_turn_opening_request: ONLY the call that starts the user's
        # turn. The agent re-sends the same trailing user prompt on every internal
        # loop call, so without this dedup would fire mid-turn on the first ask
        # (the bogus "you asked this before" bug) and corrupt the loop's context.
        try:
            import bursar_live as live  # already imported above on the happy path
            import bursar_db as db

            prompt = pending["prompt"]
            # turn_opening computed in capture (turn_id-based, drift-immune).
            #
            # T2 — TEMPORAL-CLASS ROUTER (replaces the old binary H5 freshness
            # veto). The veto conflated EVOLVING knowledge ("the latest RAG
            # strategies") with LIVE data ("the latest BTC price") — both tripped
            # one regex — and so declined a large fraction of legitimate research
            # re-asks. Instead we classify the re-ask's TEMPORAL CLASS and route:
            #   timeless → serve cheap / augment-plain expensive (the H12 path)
            #   evolving → augment-COMPARISON (fresh call + "what changed" diff)
            #   live     → augment-COMPARISON (fresh call; prior is a dated baseline)
            #   stateful → run fresh
            # Key safety property: only SERVE (return a cached answer with no fresh
            # call) can be stale, and we serve ONLY for timeless. Every other reuse
            # is augment — a fresh call with the prior injected as context — so it
            # is never stale. We classify ONLY when a reusable match exists, so the
            # aux-LLM classify cost is paid on real re-asks, not every turn.
            #
            # NOTE (in-app fix 2026-06-22): live and evolving now BOTH route to
            # augment-comparison. The earlier "live + prior older than TTL → run
            # plain fresh" branch was over-conservative: comparison ALWAYS runs a
            # fresh call with the prior shown as an explicitly-dated baseline ("as
            # of <when> …, current state re-derived"), so it is never stale at any
            # age — plain-fresh costs the same and adds nothing. Dropping it also
            # makes the router robust to the classifier confusing evolving↔live
            # (the exact miss we hit: a RAG research re-ask was tagged 'live' off
            # "right now / in production", and a 220s-old prior fell past the TTL).
            # Both classes now behave identically; the class is just a badge label.
            if turn_opening:
                # N1 — tenant isolation. By default scope the dedup window to THIS
                # process's tenant, so a match can never come from another org and
                # team B can't be served (or shown) team A's answer. Cross-tenant
                # AUGMENT is opt-in (BURSAR_CROSS_TENANT_AUGMENT); even then we never
                # SERVE across tenants — see the same_tenant gate on the serve branch.
                live_tenant = live.tenant_id()
                cross_tenant_augment = _truthy(os.environ.get("BURSAR_CROSS_TENANT_AUGMENT"))
                find_tenant = None if cross_tenant_augment else live_tenant
                with db.connect_closing() as conn:
                    dup = live.find_dup(conn, prompt, tenant=find_tenant)
                    # Only reuse a REAL cached answer — never a placeholder, a
                    # reasoning fragment, or an interrupted turn.
                    if dup is not None and live.is_servable_answer(dup[2]):
                        orig_id, sim, cached = dup
                        tclass = live.classify_temporal(prompt)
                        api_synth = live.is_synthesizable_api_mode(api_mode)
                        turn = live.original_turn_summary(conn, orig_id)
                        orig_session, _op, _ = live.query_ref(conn, orig_id)
                        same_session = bool(orig_session) and orig_session == session_id
                        # N1 — is the matched answer from THIS tenant? When cross
                        # is off, find_dup already filtered to live_tenant, so this
                        # is always True. When cross is on (opt-in), a cross-tenant
                        # match is allowed to AUGMENT but must never SERVE — a None
                        # tenant (unreadable row) compares unequal, so it fails closed.
                        same_tenant = live.query_tenant(conn, orig_id) == live_tenant
                        # Prefer the original turn's FINAL answer over the matched
                        # row (which may be a mid-loop fragment).
                        prior = (turn or {}).get("final_answer") or cached
                        age = live.cache_age_seconds(conn, orig_id)

                        if not api_synth:
                            # Provider shape we can't synthesize/inject into — never
                            # mutate an unsupported request; record a shadow only.
                            pending["shadow_dedup"] = {
                                "would_dedup_augment": True,
                                "similarity": round(sim, 3),
                                "temporal_class": tclass,
                                "reason": f"api_mode {api_mode!r} not synthesizable",
                            }
                        elif tclass == "stateful":
                            pending["shadow_dedup"] = {
                                "would_dedup_augment": True,
                                "similarity": round(sim, 3),
                                "temporal_class": tclass,
                                "reason": "stateful — ran fresh (prior reflects different session state)",
                            }
                        else:
                            # Reuse path: timeless | evolving | live.
                            # compare=True for the time-sensitive classes (evolving,
                            # live): inject the prior as an explicitly-dated baseline
                            # and require a "what changed since <when>" diff — the
                            # call runs fresh, so it is never stale regardless of the
                            # prior's age. timeless → plain reuse.
                            compare = tclass in ("evolving", "live")

                            # H12 — strategy by ORIGINAL cost. SERVE (substitute,
                            # $0 new spend, ends the turn with the cached answer) is
                            # only safe for a TIMELESS class with a cheap original;
                            # a time-sensitive class must run fresh even when cheap,
                            # so it never returns stale data.
                            calls = int((turn or {}).get("calls", 0) or 0)
                            reuse_saved = float((turn or {}).get("cost", 0.0) or 0.0)
                            augment_cost = float(quote.est_cost or 0.0) + float(quote.fee or 0.0)
                            augment_worth_it = (calls >= _AUGMENT_MIN_CALLS
                                                and reuse_saved > augment_cost)
                            # same_tenant required: SERVE substitutes another
                            # turn's cached answer verbatim, so it must never hand
                            # one tenant another tenant's text. A cross-tenant match
                            # (only possible under the opt-in flag) falls through to
                            # AUGMENT, which runs a fresh call.
                            if tclass == "timeless" and not augment_worth_it and same_tenant:
                                content = live.dedup_reply(
                                    prior, sim=sim, saved=reuse_saved,
                                    orig_session=orig_session, same_session=same_session,
                                    reasoning_tokens=live.original_reasoning_tokens(conn, orig_id),
                                )
                                live.record_deduped(
                                    conn, team=pending["team"], prompt=prompt,
                                    orig_id=orig_id, sim=sim, saved=reuse_saved,
                                    cached_result=prior, session_id=session_id or None,
                                    temporal_class=tclass, reuse_mode="serve",
                                )
                                pending["served_dedup"] = True
                                log.debug(
                                    "bursar_gate dedup-SERVE (timeless cheap original: "
                                    "calls=%s saved=$%.4f vs augment $%.4f): %s",
                                    calls, reuse_saved, augment_cost, orig_id,
                                )
                                return live.synthesize_chat_response(content, model)

                            # AUGMENT — one fresh call with the prior injected as
                            # context (skip the rediscovery loop), and for the
                            # time-sensitive classes a required "what changed" diff.
                            # Cache-the-work: re-hash the files that turn read (0 LLM
                            # tokens) so the block can say "unchanged — don't re-read"
                            # with proof instead of paying to re-read.
                            freshness = _freshness_for_turn(conn, (turn or {}).get("turn_id"))
                            block = live.dedup_augment_block(
                                prior_answer=prior, sim=sim, turn=turn,
                                orig_session=orig_session, same_session=same_session,
                                freshness=freshness, compare=compare,
                                prior_age=age, temporal_class=tclass,
                            )
                            if _inject_context(request, block):
                                pending["dedup_augmented"] = {
                                    "orig_id": orig_id, "similarity": round(sim, 3),
                                    "temporal_class": tclass,
                                    "cross_tenant": not same_tenant,
                                    # N5 observability: how big the prior was and how
                                    # much actually got injected after the cap.
                                    **live.prior_cap_stats(prior),
                                    "mode": "comparison" if compare else "plain",
                                    "cache_age_s": age,
                                    "skipped_calls": (turn or {}).get("calls", 0),
                                    "skipped_tokens": (turn or {}).get("tokens", 0),
                                    "skipped_cost": (turn or {}).get("cost", 0.0),
                                    "files_checked": len(freshness),
                                    "files_unchanged": sum(1 for f in freshness if f["unchanged"]),
                                }
                                log.debug(
                                    "bursar_gate dedup-augment[%s/%s]: sim=%.3f of %s "
                                    "(reuse %s calls / ~%s tok)", tclass,
                                    "comparison" if compare else "plain", sim, orig_id,
                                    (turn or {}).get("calls", 0), (turn or {}).get("tokens", 0),
                                )
        except Exception as exc:
            log.debug("bursar_gate: dedup-augment skipped (fail-open): %s", exc)

        # F3: down-route trivial-tier calls to a known-safe cheaper same-provider
        # model.
        try:
            import bursar_live as live

            requested_model = pending["requested_model"]
            if quote.tier == "trivial":
                target, trusted, reason = live.downroute_target(provider, requested_model)
                if target and trusted:
                    # Swap the model in place; the recorder reads actual_model so
                    # cost is attributed to what actually served the call.
                    request["model"] = target
                    pending["actual_model"] = target
                    log.debug("bursar_gate down-route: %s -> %s (trivial)", requested_model, target)
                else:
                    # Untrusted/no target → SHADOW (would-have), run as-is. This
                    # is the validate-before-arm rail: an unverified model id can
                    # never break a turn. F5 surfaces the would-be saving.
                    pending["shadow_downroute"] = {
                        "would_down_route": True,
                        "target": target,
                        "reason": reason,
                        "tier": quote.tier,
                    }
        except Exception as exc:
            log.debug("bursar_gate: down-route skipped (fail-open): %s", exc)

    return next_call(request)


# ---------------------------------------------------------------------------
# Cache-the-work — capture which files a turn READ (tool_execution middleware)
# ---------------------------------------------------------------------------

# Shell read commands whose target file the answer may depend on (H6 — the
# `terminal` blind spot: an agent that `cat`s a file leaves no read_file row).
_SHELL_READ_CMDS = {"cat", "head", "tail", "less", "more", "bat", "nl", "xxd", "od"}
# Bound how many paths a single search/command contributes to one turn's work set
# (a broad grep can hit dozens; freshness over-tracking is safe but bloats rows).
_MAX_PATHS_PER_TOOL = 25


def _stat_file(path: str):
    """``(abspath, mtime, size)`` via a cheap stat, or ``(None, None, None)``.
    The H8 fast pre-check: a stat is ~free; a full hash is O(size)."""
    try:
        abspath = str(Path(path).expanduser().resolve())
        st = os.stat(abspath)
        return abspath, st.st_mtime, st.st_size
    except Exception:
        return None, None, None


def _hash_file(path: str):
    """``(abspath, sha256, mtime, size)`` for *path*, or all-``None`` if unreadable.

    Reads the file's bytes directly (Bursar is in the agent process, same machine)
    — the out-of-band freshness check that costs 0 LLM tokens, vs. the agent paying
    full tokens to re-read + re-process the file. Returns mtime+size too so the
    capture path stores them for the H8 stat pre-check."""
    try:
        import hashlib
        abspath = str(Path(path).expanduser().resolve())
        st = os.stat(abspath)
        h = hashlib.sha256()
        with open(abspath, "rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                h.update(chunk)
        return abspath, h.hexdigest(), st.st_mtime, st.st_size
    except Exception:
        return None, None, None, None


def _paths_from_shell(command: str):
    """Files a shell command read, e.g. 'cat a.py | grep x; head -n5 b.txt' →
    ['a.py','b.txt']. Best-effort: split on shell separators, take args of a
    leading read command, skip flags/redirections."""
    # Flags that take a SEPARATE value (head -n 5, tail -c 100, bat -r 1:9) — skip
    # the value so it isn't mistaken for a path. Joined forms (-n5) are caught by
    # the startswith('-') flag check.
    value_flags = {"-n", "-c", "--lines", "--bytes", "-r", "--line-range"}
    paths = []
    for seg in re.split(r"[|;&\n]+", command or ""):
        toks = seg.strip().split()
        if not toks or toks[0] not in _SHELL_READ_CMDS:
            continue
        skip_next = False
        for t in toks[1:]:
            if skip_next:
                skip_next = False
                continue
            if t in value_flags:
                skip_next = True
                continue
            if t.startswith("-"):                 # other flag
                continue
            if t in (">", ">>", "<", "2>", "|"):  # redirection / pipe boundary
                break
            paths.append(t)
    return paths


def _paths_from_search(result):
    """File paths a search_files result surfaced (matches[].path, files[])."""
    data = result
    if isinstance(data, (bytes, str)):
        try:
            data = json.loads(data)
        except Exception:
            return []
    if not isinstance(data, dict):
        return []
    paths = [m["path"] for m in (data.get("matches") or [])
             if isinstance(m, dict) and isinstance(m.get("path"), str)]
    paths += [f for f in (data.get("files") or []) if isinstance(f, str)]
    return paths


def _path_from_uri(uri):
    """Local filesystem path behind an MCP read_resource uri, if any."""
    if not isinstance(uri, str) or not uri.strip():
        return None
    u = uri.strip()
    if u.startswith("file://"):
        return u[len("file://"):]
    if u.startswith("/") or u.startswith("~"):
        return u
    return None


def _paths_read(tool_name: str, args, result):
    """Best-effort ``[(tool_label, path)]`` a tool call read (H6). Fail-open: any
    surprise → ``[]``. De-duped and bounded by ``_MAX_PATHS_PER_TOOL``."""
    name = tool_name or ""
    a = args if isinstance(args, dict) else {}
    raw = []
    try:
        if name == "read_file":
            p = a.get("path")
            if isinstance(p, str) and p.strip():
                raw.append(("read_file", p))
        elif name == "terminal":
            raw += [("terminal", p) for p in _paths_from_shell(a.get("command") or "")]
        elif name == "search_files":
            raw += [("search_files", p) for p in _paths_from_search(result)]
        elif name.startswith("mcp_") and name.endswith("_read_resource"):
            p = _path_from_uri(a.get("uri"))
            if p:
                raw.append(("mcp_read", p))
    except Exception:
        return []
    seen, out = set(), []
    for label, p in raw:
        if p in seen:
            continue
        seen.add(p)
        out.append((label, p))
        if len(out) >= _MAX_PATHS_PER_TOOL:
            break
    return out


def _find_relocated(conn, content_hash, *, exclude=None):
    """H9: a tracked path is gone — is the SAME content under a new path now? Walk
    the paths we've ever seen with this exact hash; return the first that currently
    exists AND still hashes to it (verified, never assumed). None otherwise."""
    if not content_hash:
        return None
    try:
        import bursar_db as db
        for cand in db.paths_with_hash(conn, content_hash):
            if cand == exclude:
                continue
            abspath, digest, _m, _s = _hash_file(cand)
            if abspath and digest == content_hash:
                return abspath
    except Exception:
        pass
    return None


def _freshness_for_turn(conn, turn_id):
    """For each file the original *turn_id* read, decide its state NOW at 0 LLM
    tokens. Returns ``[{name, path, unchanged, missing, relocated_to?}]``.

    H8: a cheap stat (mtime+size) short-circuits the common unchanged case; only a
    stat mismatch (or a pre-H8 row without them) pays a full sha256. H9: a path
    that's gone is reported ``missing`` (so the agent re-discovers it instead of
    chasing a dead path), and we try to find the same bytes under a new path
    (rename/move) before giving up. A gone/unreadable file is never asserted
    'unchanged' — the safe direction."""
    out = []
    if not turn_id:
        return out
    try:
        import os as _os
        import bursar_db as db
        for w in db.work_for_turn(conn, turn_id):
            path = w.get("path")
            if not path:
                continue
            name = _os.path.basename(path)
            old_hash = w.get("content_hash")
            abspath, mtime, size = _stat_file(path)
            if abspath is None:
                # Path gone (H9): same bytes elsewhere (rename/move)? else missing.
                moved = _find_relocated(conn, old_hash, exclude=path)
                if moved:
                    out.append({"name": name, "path": path, "unchanged": True,
                                "missing": False, "relocated_to": moved})
                else:
                    out.append({"name": name, "path": path, "unchanged": False,
                                "missing": True})
                continue
            # H8 fast path: stat matches what we stored → unchanged, no hash read.
            if (w.get("mtime") is not None and w.get("size") is not None
                    and float(w["mtime"]) == float(mtime) and int(w["size"]) == int(size)):
                out.append({"name": name, "path": path, "unchanged": True, "missing": False})
                continue
            # stat differs (or a pre-H8 row) → full hash decides.
            _abs, digest, _m, _s = _hash_file(path)
            out.append({"name": name, "path": path, "missing": False,
                        "unchanged": bool(digest and digest == old_hash)})
    except Exception as exc:
        log.debug("bursar_gate: freshness check skipped (fail-open): %s", exc)
    return out


# Throttled work_cache prune (H7): every Nth capture, cap rows to ~DEDUP_WINDOW
# matchable turns' worth so a long-lived gateway can't accrete forever.
_CAPTURE_COUNT = 0
_PRUNE_EVERY = 200


def _maybe_prune(conn) -> None:
    global _CAPTURE_COUNT
    _CAPTURE_COUNT += 1
    if _CAPTURE_COUNT % _PRUNE_EVERY:
        return
    try:
        import bursar_db as db
        from bursar_dispatcher import DEDUP_WINDOW
        # Cap = DEDUP_WINDOW matchable turns × a generous ~20 tracked files/turn;
        # override with BURSAR_WORK_CACHE_MAX. Freshness for turns past the dedup
        # window is dead weight (they can never be matched), so dropping it is free.
        cap = int(os.environ.get("BURSAR_WORK_CACHE_MAX") or DEDUP_WINDOW * 20)
        n = db.prune_work_cache(conn, max_rows=cap)
        if n:
            log.debug("bursar_gate: pruned %s work_cache rows (cap=%s)", n, cap)
    except Exception as exc:
        log.debug("bursar_gate: work_cache prune skipped: %s", exc)


def _tool_gate(*, tool_name="", args=None, next_call, turn_id="", session_id="", **ctx):
    """``tool_execution`` middleware — capture-only (never blocks a tool).

    Runs the tool, then (when armed) records which file(s) a read touched + their
    content hash/mtime/size, keyed by turn_id, so a later re-ask can verify
    freshness without re-reading (H6 covers read_file/terminal/search_files/MCP).
    Fail-open: any error here still returns the real result.
    """
    result = next_call(args)
    try:
        if _armed() and turn_id:
            files = _paths_read(tool_name, args, result)
            if files:
                import bursar_db as db
                with db.connect_closing() as conn:
                    for label, path in files:
                        abspath, digest, mtime, size = _hash_file(path)
                        if abspath and digest:
                            db.record_work(conn, turn_id=turn_id,
                                           session_id=session_id or None,
                                           tool_name=label, path=abspath,
                                           content_hash=digest, mtime=mtime, size=size)
                    _maybe_prune(conn)
    except Exception as exc:
        log.debug("bursar_gate: tool capture skipped (fail-open): %s", exc)
    return result


# ---------------------------------------------------------------------------
# Plugin entry point
# ---------------------------------------------------------------------------

def register(ctx) -> None:
    """Wire up Bursar's live observability + enforcement.

    Called once, in the agent process, when the plugin is enabled via
    ``plugins.enabled`` in ``~/.hermes/config.yaml``.
    """
    _ensure_engine_on_path()
    # H1 — semantic dedup, scoped to THIS process (the agent gateway). Opting in
    # here (not in ~/.hermes/.env) keeps the dashboard/firehose process on the
    # default hashing backend, so the calibrated demo numbers stay stable while
    # the live chat gate gets semantic recall (reworded re-asks hit). setdefault
    # so an explicit operator override — including 'hashing' or 'st' — still wins.
    # 'numpy' = the pure-numpy MiniLM backend: SAME vectors as 'st' (cosine
    # 1.000000) but no torch/transformers — ~73x faster warm, dependency-free.
    os.environ.setdefault("BURSAR_EMBED_BACKEND", "numpy")
    _warm_embed_async()
    # S2 — build the Stripe meter off-thread so the first live turn doesn't pay
    # make_meter()'s one-time setup; fail-open if no test key is configured.
    _warm_meter_async()
    # Always-on observability.
    ctx.register_hook("post_api_request", _record)
    # Enforcement gate — present always, but a no-op unless BURSAR_ENFORCE=1.
    ctx.register_middleware("llm_execution", _gate)
    # Cache-the-work capture — records file reads per turn (armed only).
    ctx.register_middleware("tool_execution", _tool_gate)
    log.info(
        "bursar_gate registered (enforcement %s, embed=%s)",
        "ARMED" if _armed() else "disarmed",
        os.environ.get("BURSAR_EMBED_BACKEND", "hashing"),
    )


def _warm_embed_async() -> None:
    """Load the dedup embedder off the registration thread so the ~10s first
    model load (semantic backend) doesn't land mid-chat on the user's first turn.
    Fail-open: any warm error just defers to a normal lazy load later."""
    def _run() -> None:
        try:
            import bursar_embed as embed
            log.info("bursar_gate: dedup embedder warmed (%s)", embed.warm())
        except Exception as exc:  # pragma: no cover - warming must never break startup
            log.debug("bursar_gate: embed warm skipped (%s)", exc)
    try:
        threading.Thread(target=_run, name="bursar-embed-warm", daemon=True).start()
    except Exception as exc:  # pragma: no cover
        log.debug("bursar_gate: could not start warm thread (%s)", exc)


# ---------------------------------------------------------------------------
# Stripe settlement (S2) — meter the LIVE agent path, not just the firehose
# ---------------------------------------------------------------------------
#
# The synthetic firehose already settles each serviced query through Stripe
# (run_tick(meter=…)); the live agent path (this plugin's _record) never did, so
# real chat a judge watches recorded settled=0. We build the SAME meter the
# firehose/dashboard use — bursar_stripe.make_meter(), guarded by is_active() —
# once per process, and pass its event id into record_serviced so the live
# ledger row lands settled=1 with a real test-mode MeterEvent id.
#
# Fail-open by construction: make_meter() degrades to a no-op (no key / refused
# live key / SDK missing / setup error), the meter callable swallows its own
# errors and returns None (→ row records settled=0), and the whole _record body
# is already wrapped — so Stripe can NEVER surface into the chat. Metering is
# observability, so it is ALWAYS-ON (independent of BURSAR_ENFORCE), like the
# rest of _record. It only ever fires for a REAL call: _record returns early on
# the served_dedup (pure cache-serve) branch, and the dedup-augment branch DOES
# run a real cheap call, so metering it is correct.
_METER = None          # cached meter callable (or None when Stripe is unavailable)
_METER_BUILT = False   # has the one-time build run yet?
_METER_LOCK = threading.Lock()


def _get_meter():
    """Lazily build the Stripe meter ONCE per process and cache it. Returns a
    callable ``meter(team, model, tokens, total) -> event_id | None`` when a test
    key is configured, else ``None``. Never raises — a build failure caches
    ``None`` so we don't retry the (network) setup on every call."""
    global _METER, _METER_BUILT
    if _METER_BUILT:
        return _METER
    with _METER_LOCK:
        if _METER_BUILT:  # another thread built it while we waited
            return _METER
        try:
            import bursar_stripe as stripe
            _METER = stripe.make_meter() if stripe.is_active() else None
        except Exception as exc:  # pragma: no cover - defensive, must stay silent
            log.debug("bursar_gate: Stripe meter unavailable (%s); recording local-only", exc)
            _METER = None
        _METER_BUILT = True
    return _METER


def _warm_meter_async() -> None:
    """Build the Stripe meter off the registration thread so the first live turn
    doesn't pay make_meter()'s one-time Stripe setup (meter retrieve/create)
    mid-chat. Fail-open: any error just defers to a normal lazy build later."""
    def _run() -> None:
        try:
            m = _get_meter()
            log.info("bursar_gate: Stripe meter %s", "active (test mode)" if m else "inactive (local-only)")
        except Exception as exc:  # pragma: no cover - warming must never break startup
            log.debug("bursar_gate: meter warm skipped (%s)", exc)
    try:
        threading.Thread(target=_run, name="bursar-meter-warm", daemon=True).start()
    except Exception as exc:  # pragma: no cover
        log.debug("bursar_gate: could not start meter warm thread (%s)", exc)
