"""Bursar — synthetic enterprise query firehose (feature A8).

Simulates an enterprise's AI traffic so the demo shows scale without a real
customer: a tunable mix of high/standard/low value queries across teams, with
a deliberate near-duplicate rate (the ~31% the pitch cites) so the dedup beat
lands. Two modes:

* ``--once N``    ingest N queries and exit (then run a dispatch tick).
* ``--stream``    ingest continuously at ``--rate`` queries/sec until Ctrl-C,
                  running a dispatch tick every ``--tick`` seconds. This is
                  what drives the live trading floor during the demo.

Tunables: ``--dup-rate`` (fraction that are near-duplicates of a recent
query), ``--value-mix`` (high,standard,low weights), ``--burst`` (occasional
floods of low-value traffic — the "rogue agent loop" texture).
"""

from __future__ import annotations

import argparse
import os
import random
import time
from dataclasses import dataclass

import numpy as np

import bursar_db as db
import bursar_dedup as dedup
import bursar_dispatcher as disp
import bursar_stripe as stripe_meter

TEAMS = ["research", "sales", "engineering", "support", "marketing"]

# Enterprise query generator. Prompts are composed from independent dimensions
# (action × subject × entity × qualifier) so the combinatorial space is in the
# millions — enough entropy that genuinely-distinct queries don't collide at
# 100k-500k scale, which keeps the measured dedup rate tracking the configured
# --dup-rate instead of ballooning. The *action* carries the value-tier signal
# (keywords the scorer keys off); the other dimensions add realistic variety.

ACTIONS_HIGH = [
    "find the root cause of the production outage in", "assess the security breach exposure in",
    "evaluate the customer churn risk for", "summarize the revenue impact of the incident in",
    "review the compliance audit risk in", "triage the production incident affecting",
    "assess the contract renewal risk for", "escalate the executive customer complaint about",
    "quantify the revenue at risk from the outage in", "investigate the data breach in",
    "prepare the board briefing on the incident in", "analyze the forecast risk for",
]
ACTIONS_STD = [
    "draft an email to the team about", "summarize this week's standup notes for",
    "compare last month's metrics for", "reformat the project plan for",
    "review the current status of", "prepare a short brief on",
    "list the open action items for", "outline next steps for",
    "write release notes for", "summarize customer feedback on",
]
ACTIONS_LOW = [
    "tell me a joke about", "write a poem about", "share some fun trivia about",
    "suggest a recipe involving", "chat casually about", "recommend a playlist for",
    "what's the weather like for", "share a fun fact about",
]

SUBJECTS = [
    "the checkout flow", "the billing system", "the data pipeline", "onboarding",
    "the mobile app", "search ranking", "the API gateway", "the analytics warehouse",
    "the recommendation engine", "the fraud detection system", "the SSO rollout",
    "the data lake", "the support queue", "the payments service", "the notification system",
    "the inventory sync", "the reporting dashboard", "the auth service", "the CDN config",
    "the email pipeline", "the loyalty program", "the partner portal", "the iOS client",
    "the Android client", "the webhook delivery", "the batch scheduler", "the feature store",
    "the model serving layer", "the rate limiter", "the audit log",
]
ENTITIES = [
    "ACME Corp", "Globex", "Initech", "Umbrella Inc", "Soylent Co", "Hooli",
    "Stark Industries", "Wonka Labs", "Cyberdyne", "Wayne Enterprises", "Pied Piper",
    "the EMEA region", "the APAC region", "the LATAM region", "the NA region",
    "the enterprise tier", "the SMB segment", "the strategic accounts", "the public sector team",
    "the healthcare vertical", "the fintech vertical", "the retail vertical", "the gaming vertical",
]
QUALIFIERS = [
    "this quarter", "for Q3", "last month", "year over year", "versus target",
    "for the board review", "by end of week", "in production", "for the audit",
    "ahead of renewal", "before the launch", "for the QBR", "this sprint",
    "for the exec sync", "in the EU region", "for the migration", "post-incident",
    "for the renewal cycle", "ahead of the deadline", "for the SLA review",
]


def _compose(actions: list[str]) -> str:
    a = random.choice(actions)
    s = random.choice(SUBJECTS)
    e = random.choice(ENTITIES)
    q = random.choice(QUALIFIERS)
    return f"{a} {s} at {e} {q}?"

# Paraphrase rewrites that manufacture near-duplicates by changing ONLY
# filler/word-order — the content words are preserved, so a re-ask scores
# >=0.90 cosine and reliably dedups, while genuinely different subjects do
# not. Swaps here are stopwords (dropped by the embedder) plus reordering.
_PARAPHRASE_PREFIXES = ["", "can you ", "please ", "quick question: ", "hey, ", "i need to know: "]
_STOPWORD_SWAPS = [
    ("find the", "can you find the"), ("assess the", "can you assess the"),
    ("summarize", "give me a summary of"), ("draft", "write"),
    ("review the", "take a look at the"), ("evaluate the", "assess the"),
]


def _paraphrase(text: str) -> str:
    """Re-ask the same question with different filler/order, content intact —
    so a re-ask scores >=0.90 cosine and reliably dedups."""
    out = text
    for a, b in _STOPWORD_SWAPS:
        if a in out and random.random() < 0.7:
            out = out.replace(a, b)
            break
    pre = random.choice(_PARAPHRASE_PREFIXES)
    out = (pre + out[0].lower() + out[1:]) if pre else out
    # Occasionally append a trailing filler clause (stopwords only).
    if random.random() < 0.4:
        out = out.rstrip("?.") + ", can you help with this?"
    return out


@dataclass
class FirehoseConfig:
    dup_rate: float = 0.31           # fraction that are near-duplicates
    w_high: float = 0.30
    w_standard: float = 0.40
    w_low: float = 0.30
    burst: bool = False
    seed: int | None = None


class Firehose:
    def __init__(self, conn, cfg: FirehoseConfig):
        self.conn = conn
        self.cfg = cfg
        # recent originals as (prompt, embedding) so new originals can be
        # reject-sampled for distinctness and dups can be manufactured.
        self._recent: list[tuple[str, "np.ndarray"]] = []
        if cfg.seed is not None:
            random.seed(cfg.seed)

    def _raw_new(self) -> str:
        r = random.random()
        total = self.cfg.w_high + self.cfg.w_standard + self.cfg.w_low
        h = self.cfg.w_high / total
        s = (self.cfg.w_high + self.cfg.w_standard) / total
        actions = ACTIONS_HIGH if r < h else ACTIONS_STD if r < s else ACTIONS_LOW
        return _compose(actions)

    def _is_distinct(self, vec: "np.ndarray") -> bool:
        if not self._recent:
            return True
        mat = np.stack([v for _, v in self._recent])
        return float(np.max(mat @ vec)) < dedup.DEFAULT_THRESHOLD

    def _new_prompt(self) -> tuple[str, "np.ndarray"]:
        """A genuinely-new query: reject-sample against recent originals so it
        is below the dedup threshold (otherwise the measured dedup rate would
        balloon past the configured dup-rate from accidental collisions)."""
        for _ in range(6):
            cand = self._raw_new()
            emb = dedup.embed_vec(cand)
            if self._is_distinct(emb):
                return cand, emb
        # Couldn't find a distinct phrasing; force distinctness with a unique
        # reference tag (two distinctive tokens dominate the vector).
        cand = self._raw_new() + f" (ref {random.randint(10000,99999)} {random.choice(ENTITIES)})"
        return cand, dedup.embed_vec(cand)

    def next_query(self) -> tuple[str, str]:
        """Return (team, prompt) for the next query, honoring the dup rate."""
        team = random.choice(TEAMS)
        if self._recent and random.random() < self.cfg.dup_rate:
            src, _ = random.choice(self._recent)
            return team, _paraphrase(src)
        prompt, emb = self._new_prompt()
        self._recent.append((prompt, emb))
        if len(self._recent) > 60:
            self._recent.pop(0)
        return team, prompt

    def emit(self, n: int) -> int:
        """Ingest ``n`` queries; occasionally inject a low-value burst."""
        count = 0
        for _ in range(n):
            team, prompt = self.next_query()
            disp.ingest(self.conn, prompt, team)
            count += 1
            if self.cfg.burst and random.random() < 0.05:
                # A rogue low-value flood from one team (e.g. a stuck agent loop).
                rogue = random.choice(TEAMS)
                for _ in range(random.randint(4, 8)):
                    disp.ingest(self.conn, _compose(ACTIONS_LOW), rogue)
                    count += 1
        return count


def _seed_budgets(conn):
    """Give each team a budget envelope if it has none yet."""
    existing = {b["team"] for b in db.list_budgets(conn)}
    caps = {"research": 12.0, "sales": 9.0, "engineering": 12.0, "support": 6.0, "marketing": 3.0}
    for team in TEAMS:
        if team not in existing:
            db.upsert_budget(conn, team, caps.get(team, 1.0), period="2026-sprint-12")


def main():
    ap = argparse.ArgumentParser(description="Bursar synthetic query firehose")
    ap.add_argument("--once", type=int, metavar="N", help="ingest N queries then run one tick and exit")
    ap.add_argument("--stream", action="store_true", help="ingest continuously until Ctrl-C")
    ap.add_argument("--rate", type=float, default=5.0, help="queries/sec in --stream mode")
    ap.add_argument("--tick", type=float, default=1.0, help="seconds between dispatch ticks in --stream mode")
    ap.add_argument("--dup-rate", type=float, default=0.31)
    ap.add_argument("--value-mix", default="0.30,0.40,0.30", help="high,standard,low weights")
    ap.add_argument("--burst", action="store_true", help="inject occasional low-value floods")
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--reset", action="store_true", help="wipe the DB before starting")
    ap.add_argument("--no-stripe", action="store_true",
                    help="never settle through Stripe even if a test key is set")
    ap.add_argument("--nemotron", action="store_true",
                    help="value queries with Nemotron (escalation gate). Off by "
                         "default so throughput/scale runs stay pure-heuristic.")
    args = ap.parse_args()

    # Throughput harness: heuristic-only unless --nemotron. Setting this before
    # any .env load wins (load_dotenv never overrides an existing env var), so a
    # BURSAR_USE_NEMOTRON=1 in .env can't silently choke a high-rate/scale run.
    if not args.nemotron:
        os.environ["BURSAR_USE_NEMOTRON"] = "0"

    if args.reset:
        import pathlib, shutil
        shutil.rmtree(db.db_path().parent, ignore_errors=True)

    wh, ws, wl = (float(x) for x in args.value_mix.split(","))
    cfg = FirehoseConfig(dup_rate=args.dup_rate, w_high=wh, w_standard=ws, w_low=wl,
                         burst=args.burst, seed=args.seed)
    conn = db.connect()
    _seed_budgets(conn)
    fh = Firehose(conn, cfg)

    # Settle serviced queries through Stripe (test mode) when a key is set,
    # unless explicitly disabled. make_meter() degrades to a no-op otherwise,
    # so this is always safe to pass into run_tick.
    meter = None if args.no_stripe else (
        stripe_meter.make_meter() if stripe_meter.is_active() else None)

    if args.once is not None:
        n = fh.emit(args.once)
        res = disp.run_tick(conn, meter=meter)
        print(f"ingested {n} queries; tick: {res.as_dict()}")
        conn.close()
        return

    if args.stream:
        interval = 1.0 / max(args.rate, 0.1)
        last_tick = time.time()
        print(f"streaming ~{args.rate}/s, tick every {args.tick}s (Ctrl-C to stop)")
        try:
            while True:
                fh.emit(1)
                now = time.time()
                if now - last_tick >= args.tick:
                    res = disp.run_tick(conn, meter=meter)
                    last_tick = now
                    print(f"  tick: serviced={res.serviced} deduped={res.deduped} "
                          f"rejected={res.rejected} queued={res.queued} bill=${res.bill:.4f}")
                time.sleep(interval)
        except KeyboardInterrupt:
            print("\nstopped.")
        finally:
            conn.close()
        return

    ap.print_help()


if __name__ == "__main__":
    main()
