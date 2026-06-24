"""Bursar — semantic deduplication (feature A3).

~31% of production queries are near-duplicates (the pitch's headline waste).
Dedup collapses semantically near-identical queries into a single inference;
repeats return the cached result for ~free.

Embedding strategy
------------------
Default: a **zero-dependency hashing n-gram embedder** — character and word
n-grams hashed into a fixed-width L2-normalized vector. No model download, no
sklearn, runs instantly, and is more than enough to catch the deliberate
near-duplicates the firehose injects ("what's our churn rate" vs "tell me the
churn rate"). Cosine similarity over these vectors is the dedup signal.

A real sentence-embedding model can be swapped in behind ``BURSAR_EMBEDDER``
later without touching the dispatcher — the interface is just
``embed(text) -> list[float]`` and ``cosine(a, b) -> float``.
"""

from __future__ import annotations

import hashlib
import math
import re
from typing import Iterable, Optional

import numpy as np

EMBED_DIM = 256

_WORD_RE = re.compile(r"[a-z0-9]+")


def _tokens(text: str) -> list[str]:
    return _WORD_RE.findall((text or "").lower())


# Common interrogatives / filler that carry little topic signal — dropping
# them keeps "what is our churn rate" and "tell me the churn rate" close.
_STOPWORDS = {
    "the", "a", "an", "is", "are", "was", "were", "be", "of", "to", "for",
    "in", "on", "our", "my", "we", "i", "you", "me", "us", "do", "does",
    "did", "can", "could", "would", "should", "please", "tell", "show",
    "give", "what", "whats", "how", "which", "list", "get", "and", "or",
}


def _content_tokens(text: str) -> list[str]:
    return [t for t in _tokens(text) if t not in _STOPWORDS]


def _features(text: str) -> Iterable[tuple[str, float]]:
    """Yield (feature, weight) pairs. Content unigrams carry the bulk of the
    signal (shared topic words = same question), word bigrams add order
    sensitivity, character 4-grams give morphology/typo robustness at low
    weight. Stopwords are dropped so phrasing differences don't dominate."""
    content = _content_tokens(text)
    for t in content:
        yield f"w:{t}", 2.0
    for a, b in zip(content, content[1:]):
        yield f"b:{a}_{b}", 1.0
    joined = " ".join(content)
    for i in range(len(joined) - 3):
        yield f"c:{joined[i:i + 4]}", 0.3


def _hash_bucket(feature: str) -> tuple[int, float]:
    """Map a feature to (dimension, sign) via a stable hash. The sign bit
    decorrelates collisions (the signed-hashing trick)."""
    digest = hashlib.md5(feature.encode("utf-8")).digest()
    idx = int.from_bytes(digest[:4], "big") % EMBED_DIM
    sign = 1.0 if digest[4] & 1 else -1.0
    return idx, sign


def embed_vec(text: str) -> "np.ndarray":
    """Return an L2-normalized weighted hashing n-gram vector as float32.

    This is the hot-path embedder used by the dispatcher and firehose. The
    list-returning :func:`embed` wraps it for callers that want plain floats.
    """
    vec = np.zeros(EMBED_DIM, dtype=np.float32)
    for feat, weight in _features(text):
        idx, sign = _hash_bucket(feat)
        vec[idx] += sign * weight
    norm = float(np.linalg.norm(vec))
    if norm > 0:
        vec /= norm
    return vec


def embed(text: str) -> list[float]:
    """Return an L2-normalized weighted hashing n-gram vector for ``text``."""
    return embed_vec(text).tolist()


def pack(vec: "np.ndarray") -> bytes:
    """Serialize an embedding to compact float32 bytes for DB storage."""
    return np.asarray(vec, dtype=np.float32).tobytes()


def unpack(blob: bytes) -> "np.ndarray":
    """Inverse of :func:`pack` — instant vs JSON parsing at scale."""
    return np.frombuffer(blob, dtype=np.float32)


def best_match_vec(
    qvec: "np.ndarray",
    window_mat: Optional["np.ndarray"],
    ids: list[str],
    *,
    threshold: float = None,  # type: ignore[assignment]
) -> Optional[tuple[str, float]]:
    """Vectorized nearest-neighbor over a window matrix.

    ``window_mat`` is an (W x D) array of L2-normalized row embeddings and
    ``ids`` the parallel query ids. Because rows and ``qvec`` are normalized,
    ``window_mat @ qvec`` is the cosine vector — one BLAS matvec instead of a
    Python loop. Returns (id, similarity) if the best clears ``threshold``.
    """
    if threshold is None:
        threshold = DEFAULT_THRESHOLD
    if window_mat is None or window_mat.shape[0] == 0:
        return None
    sims = window_mat @ qvec
    j = int(np.argmax(sims))
    best = float(sims[j])
    if best >= threshold:
        return ids[j], best
    return None


def cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity of two equal-length vectors (already normalized →
    this is just the dot product, but we stay total for unnormalized input)."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


# Cosine at/above which two queries are treated as the *same question asked
# again*. Tuned empirically for the weighted hashing embedder: near-identical
# rephrasings (same content words, different filler/order) score 0.90-1.0;
# same-template-but-different-subject queries (genuinely different questions,
# e.g. "outage in checkout" vs "outage in billing") score ~0.80-0.84;
# unrelated queries ~0. 0.85 is deliberately conservative — it collapses
# re-asks, not merely-similar questions, so the exchange never returns a
# stale answer to a genuinely different query.
DEFAULT_THRESHOLD = 0.85


def best_match(
    vec: list[float],
    window: list[tuple[str, list[float]]],
    *,
    threshold: float = DEFAULT_THRESHOLD,
) -> Optional[tuple[str, float]]:
    """Return (query_id, similarity) of the closest prior query in the
    rolling ``window`` if it clears ``threshold``, else None.

    ``window`` is a list of (query_id, embedding) for recent serviced
    queries — the dispatcher maintains it.
    """
    best_id: Optional[str] = None
    best_sim = 0.0
    for qid, wvec in window:
        sim = cosine(vec, wvec)
        if sim > best_sim:
            best_sim = sim
            best_id = qid
    if best_id is not None and best_sim >= threshold:
        return best_id, best_sim
    return None


if __name__ == "__main__":
    pairs = [
        ("what is our customer churn rate?", "tell me the churn rate for our customers"),
        ("what is our customer churn rate?", "how many open support tickets are there?"),
        ("summarize the Q3 board deck", "summarize the Q3 board deck"),
    ]
    for x, y in pairs:
        print(f"{cosine(embed(x), embed(y)):.3f}  | {x!r}  ~  {y!r}")
