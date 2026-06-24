"""Bursar — Nemotron valuation glue (feature A4).

The "valuation brain." `bursar_scoring._nemotron()` calls
:func:`score_value_with_model` to have **Nemotron 3 Ultra** rate a query's
business value; on *any* failure it returns ``None`` and the caller falls
back to the transparent heuristic, so a slow or unreachable endpoint can
never sink the demo.

**Primary path is Hermes-native.** This is a Nous-judged entry, so the
valuation brain runs through Hermes's OWN auxiliary LLM client
(``agent.auxiliary_client.get_text_auxiliary_client`` — the same helper
``kanban_specify`` uses). It uses whatever provider/model the operator set in
``~/.hermes/config.yaml`` (make Nemotron 3 Ultra the ``model.default`` and it
scores every query) and inherits Hermes's provider-fallback chain. No parallel
config, no duplicate auth — Bursar is a Hermes citizen.

**Fallback path** is a zero-dependency OpenAI-compatible Chat Completions
client (``urllib``). It's used when an explicit endpoint is pinned via
``BURSAR_NEMOTRON_BASE_URL`` (a local DGX Spark, a mock, a specific NIM) or
when Hermes's auxiliary client isn't importable (standalone runs/tests). That
one shape still covers hosted NIM, a local DGX, Nous, or OpenRouter.

Config resolution (first match wins):

1. **Our own env / skill-root ``.env``** — an explicit override:
       BURSAR_NEMOTRON_BASE_URL   e.g. http://localhost:8000/v1 (local DGX)
       BURSAR_NEMOTRON_API_KEY    (aliases: NVIDIA_API_KEY, NEMOTRON_API_KEY)
       BURSAR_NEMOTRON_API_MODEL  the wire model id
2. **Hermes config** — the provider/base_url/key/default-model that Hermes
   already resolves from ``~/.hermes/config.yaml`` (the plan's "set Nemotron
   as ``model.default``"). Read via ``hermes_cli.auth``; reused so the demo
   needs no duplicate setup. Guarded — if ``hermes_cli`` isn't importable or
   resolution fails, we fall through.
3. **Default** — ``https://integrate.api.nvidia.com/v1`` (hosted NIM), no key.

    BURSAR_NEMOTRON_TIMEOUT    seconds, default 30 (a 550B reasoner is slow;
                               fails closed to the heuristic on timeout)

The model is asked for strict JSON ``{"value": int 0-100, "rationale": str}``;
we extract it robustly (a model may wrap it in prose or a ```json fence).
"""

from __future__ import annotations

import json
import os
import re
import ssl
import urllib.error
import urllib.request
from typing import Optional


def _ssl_context():
    """Prefer certifi's CA bundle (macOS system Python often ships without a
    usable one, which trips the hosted-NIM HTTPS call). Falls back to the
    default context."""
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        try:
            return ssl.create_default_context()
        except Exception:
            return None

DEFAULT_BASE_URL = "https://integrate.api.nvidia.com/v1"
DEFAULT_TIMEOUT = 30.0
DEFAULT_MAX_TOKENS = 512

# Leading line: the Llama-/Nemotron reasoning toggle. "detailed thinking off"
# gives a direct answer instead of chain-of-thought — without it a reasoning
# model burns the token budget thinking out loud and never reaches the JSON
# (and is far slower). Harmless on non-Nemotron models (just text).
_SYSTEM = (
    "detailed thinking off\n"
    "You are Bursar's valuation engine for an internal AI compute exchange. "
    "Rate the BUSINESS VALUE of running the user's query on a 0-100 scale, "
    "where 0 = worthless/trivial (jokes, chit-chat, idle trivia) and "
    "100 = mission-critical (revenue at risk, production incidents, security "
    "breaches, legal/compliance, board/executive decisions). Judge expected "
    "business payoff, not how interesting the question is. "
    "Respond with STRICT JSON only, no prose, no code fence: "
    '{"value": <integer 0-100>, "rationale": "<=12 words"}.'
)


_ENV_LOADED = False


def _ensure_env() -> None:
    """Load the skill-root .env once (zero-dep; exported vars win). Reuses
    bursar_stripe's loader so there's a single .env convention."""
    global _ENV_LOADED
    if _ENV_LOADED:
        return
    _ENV_LOADED = True
    try:
        import bursar_stripe
        bursar_stripe.load_dotenv()
    except Exception:
        pass


def _env_key() -> str:
    _ensure_env()
    for k in ("BURSAR_NEMOTRON_API_KEY", "NVIDIA_API_KEY", "NEMOTRON_API_KEY"):
        v = (os.environ.get(k) or "").strip()
        if v:
            return v
    return ""


def _hermes_credentials() -> dict:
    """Best-effort read of the provider/base_url/key/default-model that Hermes
    resolves from ~/.hermes/config.yaml. Returns {} if hermes_cli isn't
    importable or anything fails — caller falls through to the default."""
    try:
        from hermes_cli import auth  # type: ignore
    except Exception:
        return {}
    try:
        provider = auth.get_active_provider()
        if not provider:
            return {}
        creds = auth.resolve_api_key_provider_credentials(provider) or {}
        out = {"base_url": (creds.get("base_url") or "").rstrip("/"),
               "api_key": creds.get("api_key") or ""}
        try:
            from hermes_cli import config as hconfig  # type: ignore
            model = (hconfig.load_config_readonly() or {}).get("model") or ""
            # config model ids look like "provider/model"; the wire id is the
            # part after the last slash for OpenAI-compatible endpoints.
            if model:
                out["model"] = model.split("/")[-1]
        except Exception:
            pass
        return out
    except Exception:
        return {}


def _runtime() -> dict:
    """Resolve {base_url, api_key, model} with the documented precedence:
    our env override → Hermes config → hosted-NIM default."""
    env_url = (os.environ.get("BURSAR_NEMOTRON_BASE_URL") or "").rstrip("/")
    env_key = _env_key()
    env_model = (os.environ.get("BURSAR_NEMOTRON_API_MODEL") or "").strip()
    # Only pay the Hermes lookup if an override is missing.
    h = {} if (env_url and env_key and env_model) else _hermes_credentials()
    return {
        "base_url": env_url or h.get("base_url") or DEFAULT_BASE_URL,
        "api_key": env_key or h.get("api_key") or "",
        "model": env_model or h.get("model") or "",
    }


def _timeout() -> float:
    try:
        return float(os.environ.get("BURSAR_NEMOTRON_TIMEOUT", DEFAULT_TIMEOUT))
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT


_JSON_RE = re.compile(r"\{[^{}]*\}", re.DOTALL)


def _extract_json(text: str) -> dict:
    """Pull the first JSON object out of a completion, tolerating a ```json
    fence or surrounding prose. Raises ValueError if none parses."""
    text = (text or "").strip()
    try:
        return json.loads(text)
    except Exception:
        pass
    for m in _JSON_RE.finditer(text):
        try:
            return json.loads(m.group(0))
        except Exception:
            continue
    raise ValueError(f"no JSON object in model output: {text[:160]!r}")


def _messages(prompt: str) -> list:
    return [
        {"role": "system", "content": _SYSTEM},
        {"role": "user", "content": (prompt or "")[:2000]},
    ]


def _parse(content: str) -> dict:
    obj = _extract_json(content)
    value = float(obj.get("value"))
    rationale = str(obj.get("rationale") or "nemotron rating")
    return {"value": value, "rationale": rationale}


# --- primary path: Hermes-native auxiliary model client -------------------
# This is the point of a Nous-judged entry: Bursar's valuation brain runs
# through Hermes's OWN auxiliary LLM client (agent.auxiliary_client), so it
# uses whatever provider/model the operator configured in ~/.hermes/config.yaml
# (set Nemotron 3 Ultra as model.default and it scores every query), and
# inherits Hermes's provider fallback. The same helper kanban_specify uses.

_AUX_CACHE: tuple = ()  # (client, model) once resolved; () = not tried, None-tuple = unavailable


class _AuxUnavailable(Exception):
    """Hermes auxiliary client isn't importable or has no provider configured."""


def _aux_client():
    """Return (client, model) from Hermes's auxiliary client, or raise
    _AuxUnavailable. Cached. Building the client makes no network call."""
    global _AUX_CACHE
    if _AUX_CACHE:
        if _AUX_CACHE[0] is None:
            raise _AuxUnavailable("no auxiliary client configured")
        return _AUX_CACHE
    try:
        from agent.auxiliary_client import get_text_auxiliary_client  # type: ignore
    except Exception as e:  # not running inside Hermes (e.g. standalone tests)
        _AUX_CACHE = (None, None)
        raise _AuxUnavailable(f"auxiliary client import failed: {e!r}")
    try:
        client, model = get_text_auxiliary_client("bursar_valuation")
    except Exception as e:
        _AUX_CACHE = (None, None)
        raise _AuxUnavailable(f"get_text_auxiliary_client failed: {e!r}")
    if client is None or not model:
        _AUX_CACHE = (None, None)
        raise _AuxUnavailable("no auxiliary client configured")
    _AUX_CACHE = (client, model)
    return client, model


def _aux_score(prompt: str) -> dict:
    client, model = _aux_client()  # raises _AuxUnavailable if not wired
    try:
        from agent.auxiliary_client import get_auxiliary_extra_body  # type: ignore
        extra = get_auxiliary_extra_body() or None
    except Exception:
        extra = None
    resp = client.chat.completions.create(
        model=model,
        messages=_messages(prompt),
        temperature=0,
        max_tokens=DEFAULT_MAX_TOKENS,
        timeout=_timeout(),
        extra_body=extra,
    )
    return _parse(resp.choices[0].message.content or "")


# --- fallback path: zero-dep OpenAI-compatible client ---------------------
# Used when an explicit endpoint is set (local DGX Spark, a mock) or when
# Hermes's auxiliary client isn't available (standalone runs).

def _http_score(prompt: str, model: str) -> dict:
    rt = _runtime()
    wire_model = rt["model"] or model
    body = json.dumps({
        "model": wire_model,
        "messages": _messages(prompt),
        "temperature": 0,
        "max_tokens": DEFAULT_MAX_TOKENS,
        "stream": False,
    }).encode("utf-8")
    req = urllib.request.Request(
        f"{rt['base_url']}/chat/completions", data=body, method="POST",
        headers={"Content-Type": "application/json"},
    )
    if rt["api_key"]:
        req.add_header("Authorization", f"Bearer {rt['api_key']}")
    with urllib.request.urlopen(req, timeout=_timeout(), context=_ssl_context()) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    return _parse(payload["choices"][0]["message"]["content"])


def score_value_with_model(prompt: str, *, model: str = "nemotron-ultra") -> dict:
    """Rate ``prompt`` → ``{"value": float, "rationale": str}``. Raises on any
    failure so the caller (``bursar_scoring._nemotron``) falls back to the
    heuristic.

    An explicit ``BURSAR_NEMOTRON_BASE_URL`` wins (local DGX / mock / pinned
    endpoint). Otherwise the **Hermes auxiliary client** is used — the whole
    point of a Hermes-native entry — falling back to the direct client only if
    Hermes isn't available."""
    _ensure_env()
    if os.environ.get("BURSAR_NEMOTRON_BASE_URL"):
        out = _http_score(prompt, model)
    else:
        try:
            out = _aux_score(prompt)
        except _AuxUnavailable:
            out = _http_score(prompt, model)
    _CALLS[model] = _CALLS.get(model, 0) + 1  # telemetry for the real-test harness
    return out


# Lightweight call telemetry (which tier model actually got called). Not used
# by the engine path — read by realcheck.py to report tier mix.
_CALLS: dict = {}


def call_counts() -> dict:
    return dict(_CALLS)


def reset_calls() -> None:
    _CALLS.clear()


def is_configured() -> bool:
    """True iff a value backend looks wired — the Hermes auxiliary client, an
    explicit endpoint, or an API key (ours or Hermes config). Cheap pre-check
    so the scorer skips a doomed round-trip; the real test is the live call,
    which fails closed to the heuristic."""
    _ensure_env()
    if os.environ.get("BURSAR_NEMOTRON_BASE_URL") or _env_key():
        return True
    try:
        _aux_client()
        return True
    except _AuxUnavailable:
        pass
    h = _hermes_credentials()
    return bool(h.get("api_key") or h.get("base_url"))


if __name__ == "__main__":
    # Live probe: requires BURSAR_NEMOTRON_BASE_URL (+ key for hosted NIM).
    import sys
    # Load skill-root .env so a key/endpoint placed there is picked up.
    try:
        import bursar_stripe
        bursar_stripe.load_dotenv()
    except Exception:
        pass
    q = sys.argv[1] if len(sys.argv) > 1 else \
        "Production outage is dropping customer checkout — find the root cause."
    rt = _runtime()
    print(f"base_url={rt['base_url']}  key={'set' if rt['api_key'] else 'none'}  "
          f"model={rt['model'] or '(arg default)'}")
    try:
        print("result:", score_value_with_model(q))
    except Exception as e:
        print(f"call failed ({e!r}) — bursar_scoring would fall back to heuristic.")
        raise SystemExit(1)
