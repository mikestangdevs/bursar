"""Bursar — pluggable embedding backend for dedup.

The default dedup embedder (``bursar_dedup``) is a zero-dependency hashing
n-gram vector: instant, no model, no download. It catches near-verbatim re-asks
and content-preserving paraphrases, but it is *lexical* — it misses semantic
rephrasings that swap words ("who has had hat tricks" vs "which players have had
hat tricks" scores ~0.43, below threshold). For the live chat gate, where a user
genuinely re-asks the same thing in different words, that is the recall we want.

This module makes the embedder a choice, defaulting to the hashing backend so
nothing changes unless ``BURSAR_EMBED_BACKEND`` opts in:

  * ``hashing`` (default) — bursar_dedup's zero-dep vector (256-dim).
  * ``st`` | ``semantic`` — a local sentence-transformers model
    (``BURSAR_EMBED_MODEL``, default ``all-MiniLM-L6-v2``, 384-dim). Loaded once
    per process; runs on CPU; no API key, no network after the one-time model
    fetch. On the worldcup test set it lifts the rephrasing pair 0.43 → 0.80
    while keeping different-topic and compound questions correctly below
    threshold.

Safety by construction:
  * The backend is resolved ONCE per process (lazy). If sentence-transformers
    isn't installed or the model can't load, it falls back to hashing for the
    whole process, so a missing dependency can never break dedup — it just
    reverts to the lexical embedder.
  * Every stored embedding is tagged with ``active_model()`` (e.g. ``hash-256``
    or ``st-all-MiniLM-L6-v2-384``). Callers persist that tag and compare ONLY
    embeddings sharing it, so 256-dim hashing vectors and 384-dim semantic
    vectors are never cosine'd against each other (which would be garbage / a
    shape error). Switching backends simply starts a fresh cache; old rows age
    out of the window.
  * Each backend carries its own dedup threshold (semantic distributions differ
    from lexical), overridable via env.
"""

from __future__ import annotations

import logging
import os
import threading

import numpy as np

import bursar_dedup as dedup

log = logging.getLogger(__name__)

# Resolved once per process: a tuple (kind, model_or_None, tag, dim, threshold).
# None until first use. Resolving once keeps active_model() stable and in sync
# with what embed_vec() actually produces (no per-call backend flip-flop).
_resolved: tuple | None = None
# Guards the one-time resolve so a background warm() (H1) and the first real
# dedup call can't both load the model — double-checked locking around _resolved.
_resolve_lock = threading.Lock()

_ST_ALIASES = {"st", "sentence-transformers", "sentence_transformers"}
# The numpy backend runs the SAME MiniLM weights as ``st`` (proven cosine
# 1.000000) but with no torch/transformers — only numpy + stdlib. It is the
# recommended semantic backend: identical quality, ~73x faster warm,
# dependency-free. ``semantic``/``minilm`` map here (not to ``st``) so the
# default smart path is the light one.
_NUMPY_ALIASES = {"numpy", "np", "npmini", "minilm", "semantic", "mini"}
_DEFAULT_ST_MODEL = "all-MiniLM-L6-v2"
# Tuned on the worldcup rephrasing set: MiniLM scores same-question rephrasings
# ~0.78-0.94, different-topic ~0.35, compound-vs-single ~0.70. 0.78 collapses
# rephrasings without serving a compound or different-topic query a stale answer.
_DEFAULT_ST_THRESHOLD = 0.78


def _load_st_model(name: str):
    """Load a sentence-transformers model OFFLINE-FIRST. The model is normally
    already cached; HF_HUB_OFFLINE skips the hub network checks that cost ~2s+ per
    load (measured: 9.8s -> 5.6s warm). If the offline load fails (first run / not
    cached) we retry ONLINE to allow the one-time download. The offline env is
    restored afterward so we never force the rest of the gateway process offline.
    Returns the model, or None if both attempts fail (caller falls back to hashing)."""
    offline_vars = ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE")
    prev = {k: os.environ.get(k) for k in offline_vars}
    try:
        for attempt in ("offline", "online"):
            if attempt == "offline":
                for k in offline_vars:
                    os.environ[k] = "1"
            else:
                for k in offline_vars:
                    os.environ.pop(k, None)
            try:
                from sentence_transformers import SentenceTransformer  # heavy; only when opted in
                return SentenceTransformer(name)
            except Exception as exc:
                if attempt == "offline":
                    log.debug("bursar_embed: offline model load failed (%s); retrying online", exc)
                else:
                    log.warning("bursar_embed: model load failed online too (%s)", exc)
        return None
    finally:
        for k, v in prev.items():  # restore — don't leak offline mode to the process
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _model_dim(model) -> int:
    """sentence-transformers renamed the accessor across versions; try both,
    then fall back to probing with a tiny encode."""
    for attr in ("get_embedding_dimension", "get_sentence_embedding_dimension"):
        fn = getattr(model, attr, None)
        if callable(fn):
            try:
                return int(fn())
            except Exception:
                pass
    return int(len(model.encode("probe", normalize_embeddings=True)))


def _resolve() -> tuple:
    global _resolved
    if _resolved is not None:
        return _resolved
    with _resolve_lock:
        if _resolved is not None:  # another thread resolved while we waited
            return _resolved
        # Module default stays "hashing" so the firehose/dashboard/standalone tools
        # keep the zero-dep backend. The LIVE chat gate opts INTO numpy for its own
        # process via os.environ.setdefault("BURSAR_EMBED_BACKEND","numpy") at
        # registration (bursar_gate/__init__.py ~line 914) — that's what gives real
        # traffic semantic recall (reworded re-asks match) without moving the
        # synthetic demo. See [[bursar-numpy-embed]].
        backend = os.environ.get("BURSAR_EMBED_BACKEND", "hashing").strip().lower()
        if backend in _NUMPY_ALIASES:
            try:
                import bursar_minilm as mini  # numpy + stdlib only; no heavy import
                if not mini.available():
                    raise RuntimeError("minilm artifacts unavailable")
                dim = mini.dim()
                thr = float(os.environ.get("BURSAR_SEMANTIC_THRESHOLD", _DEFAULT_ST_THRESHOLD))
                _resolved = ("numpy", mini, f"np-{_DEFAULT_ST_MODEL}-{dim}", dim, thr)
                log.info("bursar_embed: numpy MiniLM backend active (dim=%d, thr=%.2f)", dim, thr)
                return _resolved
            except Exception as exc:  # artifacts missing / load failed → safe fallback
                log.warning("bursar_embed: numpy backend unavailable (%s); falling back to hashing", exc)
        if backend in _ST_ALIASES:
            try:
                name = os.environ.get("BURSAR_EMBED_MODEL", _DEFAULT_ST_MODEL)
                model = _load_st_model(name)
                if model is None:
                    raise RuntimeError("sentence-transformers model load failed (offline+online)")
                dim = _model_dim(model)
                thr = float(os.environ.get("BURSAR_SEMANTIC_THRESHOLD", _DEFAULT_ST_THRESHOLD))
                _resolved = ("st", model, f"st-{name}-{dim}", dim, thr)
                log.info("bursar_embed: semantic backend active (%s, dim=%d, thr=%.2f)", name, dim, thr)
                return _resolved
            except Exception as exc:  # not installed / model load failed → safe fallback
                log.warning("bursar_embed: semantic backend unavailable (%s); falling back to hashing", exc)
        _resolved = ("hashing", None, f"hash-{dedup.EMBED_DIM}", dedup.EMBED_DIM, dedup.DEFAULT_THRESHOLD)
        return _resolved


def warm() -> str:
    """Eagerly resolve + load the backend (and run one tiny encode) so the first
    real dedup call doesn't pay the model-load latency mid-turn (H1). Returns the
    active tag. Safe from a background thread — resolution is idempotent."""
    tag = active_model()  # triggers _resolve(): downloads/loads the model if semantic
    try:
        embed_vec("warm")  # exercise the encode path once so it's hot
    except Exception:  # pragma: no cover - warming must never raise
        pass
    return tag


def active_model() -> str:
    """Tag identifying the embedding space of vectors produced right now. Persist
    it alongside each embedding and only compare embeddings sharing this tag."""
    return _resolve()[2]


def threshold() -> float:
    """The dedup cosine threshold for the active backend."""
    return _resolve()[4]


def embed_vec(text: str) -> "np.ndarray":
    """Embed ``text`` with the active backend, as an L2-normalized float32 vector.

    Falls back to the hashing vector if a semantic encode raises after the model
    loaded (rare). A fallback vector may then be mistagged, but the only
    consequence is a missed dedup (it won't match anything) — never a wrong
    serve, which is the safe failure direction."""
    kind, model, _tag, _dim, _thr = _resolve()
    if kind == "numpy":
        try:
            return model.embed_vec(text or "")
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("bursar_embed: numpy encode failed (%s); using hashing vector", exc)
            return dedup.embed_vec(text)
    if kind == "st":
        try:
            vec = model.encode(text or "", normalize_embeddings=True)
            return np.asarray(vec, dtype=np.float32)
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("bursar_embed: encode failed (%s); using hashing vector for this call", exc)
    return dedup.embed_vec(text)
