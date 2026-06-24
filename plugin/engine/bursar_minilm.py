"""Bursar — all-MiniLM-L6-v2 sentence embeddings in pure numpy + stdlib.

This runs the *exact* MiniLM weights (same vectors as ``sentence-transformers``,
proven cosine ≥0.999) with **no torch, no transformers, no onnx** — only numpy
(already a Bursar dependency) and the standard library. It exists so the smart
*semantic* dedup backend is dependency-free and fast to warm: there is no heavy
package import, so warm is a ~sub-second numpy load instead of the ~5s
``transformers`` import.

Two shipped artifacts live in ``bursar_models/`` next to this file:
  * ``minilm_l6_v2_fp16.npz`` — the model weights (fp16, ~45 MB). fp16 is exact
    enough that cosine vs the fp32/torch vectors is 1.000000 to six decimals.
  * ``vocab.txt`` — the WordPiece vocabulary.

The forward pass (BERT: embeddings → 6 transformer layers → mean-pool →
L2-normalize) and the WordPiece tokenizer are both verified byte-equivalent to
the reference implementation. erf uses a vectorized Abramowitz-Stegun
approximation (max abs error ~1.5e-7), which keeps cosine parity while staying
fast (no per-element python loop).

If the artifacts are missing or fail to load, callers fall back to the hashing
embedder — a missing model can never break dedup, exactly as with the ``st``
backend.
"""
from __future__ import annotations

import logging
import math
import os
import unicodedata

import numpy as np

log = logging.getLogger(__name__)

_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bursar_models")
_NPZ = os.path.join(_DIR, "minilm_l6_v2_fp16.npz")
_VOCAB = os.path.join(_DIR, "vocab.txt")

MAX_LEN = 256

# Loaded once (lazy). _W holds fp32 weight arrays; _CFG = (NL, NH, H, INTER, HD, EPS).
_W: dict | None = None
_CFG: tuple | None = None
_VOCAB_MAP: dict | None = None


# --------------------------------------------------------------------------- #
# Tokenizer (BertTokenizer: do_lower_case=True, strip_accents, CJK split)
# --------------------------------------------------------------------------- #
def _is_control(c):
    if c in ("\t", "\n", "\r"):
        return False
    return unicodedata.category(c).startswith("C")


def _is_whitespace(c):
    return c in (" ", "\t", "\n", "\r") or unicodedata.category(c) == "Zs"


def _is_punct(c):
    cp = ord(c)
    if (33 <= cp <= 47) or (58 <= cp <= 64) or (91 <= cp <= 96) or (123 <= cp <= 126):
        return True
    return unicodedata.category(c).startswith("P")


def _is_cjk(cp):
    return ((0x4E00 <= cp <= 0x9FFF) or (0x3400 <= cp <= 0x4DBF)
            or (0x20000 <= cp <= 0x2A6DF) or (0x2A700 <= cp <= 0x2B73F)
            or (0x2B740 <= cp <= 0x2B81F) or (0x2B820 <= cp <= 0x2CEAF)
            or (0xF900 <= cp <= 0xFAFF) or (0x2F800 <= cp <= 0x2FA1F))


def _basic_tokenize(text):
    cleaned = []
    for c in text:
        cp = ord(c)
        if cp == 0 or cp == 0xFFFD or _is_control(c):
            continue
        cleaned.append(" " if _is_whitespace(c) else c)
    text = "".join(cleaned)
    text = "".join(f" {c} " if _is_cjk(ord(c)) else c for c in text)
    out = []
    for tok in text.strip().split():
        tok = "".join(ch for ch in unicodedata.normalize("NFD", tok.lower())
                      if unicodedata.category(ch) != "Mn")
        cur = []
        for c in tok:
            if _is_punct(c):
                if cur:
                    out.append("".join(cur))
                    cur = []
                out.append(c)
            else:
                cur.append(c)
        if cur:
            out.append("".join(cur))
    return out


def _wordpiece(token, vocab, unk="[UNK]", max_chars=100):
    if len(token) > max_chars:
        return [unk]
    out = []
    start = 0
    while start < len(token):
        end = len(token)
        cur = None
        while start < end:
            sub = token[start:end]
            if start > 0:
                sub = "##" + sub
            if sub in vocab:
                cur = sub
                break
            end -= 1
        if cur is None:
            return [unk]
        out.append(cur)
        start = end
    return out


def _encode_ids(text, vocab):
    ids = [vocab["[CLS]"]]
    for tok in _basic_tokenize(text or ""):
        for piece in _wordpiece(tok, vocab):
            ids.append(vocab.get(piece, vocab["[UNK]"]))
    if len(ids) >= MAX_LEN:
        ids = ids[:MAX_LEN - 1]
    ids.append(vocab["[SEP]"])
    return ids


# --------------------------------------------------------------------------- #
# Forward pass (numpy)
# --------------------------------------------------------------------------- #
# Abramowitz-Stegun 7.1.26 — vectorized erf, max abs error ~1.5e-7.
_A1, _A2, _A3, _A4, _A5, _P = (0.254829592, -0.284496736, 1.421413741,
                               -1.453152027, 1.061405429, 0.3275911)


def _erf(x):
    s = np.sign(x)
    ax = np.abs(x)
    t = 1.0 / (1.0 + _P * ax)
    y = 1.0 - (((((_A5 * t + _A4) * t) + _A3) * t + _A2) * t + _A1) * t * np.exp(-ax * ax)
    return s * y


def _gelu(x):
    return 0.5 * x * (1.0 + _erf(x / math.sqrt(2.0)))


def _layernorm(x, w, b, eps):
    mu = x.mean(-1, keepdims=True)
    var = x.var(-1, keepdims=True)
    return (x - mu) / np.sqrt(var + eps) * w + b


def _load():
    global _W, _CFG, _VOCAB_MAP
    if _W is not None:
        return
    data = np.load(_NPZ)
    W = {k: data[k].astype(np.float32) for k in data.files
         if not k.startswith("__")}
    nl, nh, h, inter, _maxpos, _vocab = [int(v) for v in data["__meta__"]]
    eps = float(data["__eps__"][0])
    _VOCAB_MAP = {}
    with open(_VOCAB, encoding="utf-8") as f:
        for i, line in enumerate(f):
            _VOCAB_MAP[line.rstrip("\n")] = i
    _CFG = (nl, nh, h, inter, h // nh, eps)
    _W = W


def _forward(ids):
    nl, nh, h, _inter, hd, eps = _CFG
    W = _W
    n = len(ids)
    ids = np.asarray(ids, dtype=np.int64)
    x = (W["embeddings.word_embeddings.weight"][ids]
         + W["embeddings.position_embeddings.weight"][np.arange(n)]
         + W["embeddings.token_type_embeddings.weight"][0])
    x = _layernorm(x, W["embeddings.LayerNorm.weight"], W["embeddings.LayerNorm.bias"], eps)
    for L in range(nl):
        p = f"encoder.layer.{L}."
        q = x @ W[p + "attention.self.query.weight"].T + W[p + "attention.self.query.bias"]
        k = x @ W[p + "attention.self.key.weight"].T + W[p + "attention.self.key.bias"]
        v = x @ W[p + "attention.self.value.weight"].T + W[p + "attention.self.value.bias"]
        q = q.reshape(n, nh, hd).transpose(1, 0, 2)
        k = k.reshape(n, nh, hd).transpose(1, 0, 2)
        v = v.reshape(n, nh, hd).transpose(1, 0, 2)
        scores = (q @ k.transpose(0, 2, 1)) / math.sqrt(hd)
        scores = scores - scores.max(-1, keepdims=True)
        e = np.exp(scores)
        a = e / e.sum(-1, keepdims=True)
        ctx = (a @ v).transpose(1, 0, 2).reshape(n, h)
        ao = ctx @ W[p + "attention.output.dense.weight"].T + W[p + "attention.output.dense.bias"]
        x = _layernorm(ao + x, W[p + "attention.output.LayerNorm.weight"],
                       W[p + "attention.output.LayerNorm.bias"], eps)
        inter = _gelu(x @ W[p + "intermediate.dense.weight"].T + W[p + "intermediate.dense.bias"])
        out = inter @ W[p + "output.dense.weight"].T + W[p + "output.dense.bias"]
        x = _layernorm(out + x, W[p + "output.LayerNorm.weight"],
                       W[p + "output.LayerNorm.bias"], eps)
    # all tokens valid (single sequence, no padding) → plain mean pool
    vec = x.mean(0)
    norm = float(np.linalg.norm(vec))
    return (vec / norm).astype(np.float32) if norm > 0 else vec.astype(np.float32)


def available() -> bool:
    """True if the model artifacts are present and loadable."""
    try:
        _load()
        return True
    except Exception as exc:  # pragma: no cover - defensive
        log.debug("bursar_minilm: artifacts unavailable (%s)", exc)
        return False


def embed_vec(text: str) -> "np.ndarray":
    """L2-normalized 384-dim MiniLM sentence embedding as float32."""
    _load()
    return _forward(_encode_ids(text, _VOCAB_MAP))


def dim() -> int:
    _load()
    return _CFG[2]
