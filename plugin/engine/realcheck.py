"""Bursar — real-integration checks against LIVE Stripe (test mode) + NVIDIA
Nemotron. Separate from bench.py (which is pure-local throughput): this proves
the real external pipeline works and measures where the real APIs cap out.

Subcommands:
  e2e            ingest a realistic mix, run ONE tick with Nemotron escalation
                 ON + real Stripe metering ON; verify reconciliation + report
                 the Nemotron tier mix.
  stripe-ramp    hammer real MeterEvent.create at rising concurrency; report
                 events/s and rate-limit (429) errors -> the Stripe ceiling.
  nemotron-ramp  fire real fast-Nemotron scoring calls at rising concurrency;
                 report calls/s and errors -> the Nemotron ceiling.

Numbers feed ECOSYSTEM-FEEDBACK.md. These spend real NVIDIA credits and create
real test-mode Stripe events — keep volumes modest (that's the point: the local
1M run is the throughput proof; these prove correctness + find the API limits).
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import os
import pathlib
import shutil
import time

import bursar_db as db
import bursar_dispatcher as disp
import bursar_stripe as bs
import bursar_model as bm


# --------------------------------------------------------------------------
def cmd_e2e(args):
    os.environ["BURSAR_USE_NEMOTRON"] = "1"  # real Nemotron escalation on
    os.environ.setdefault("BURSAR_DB", "/tmp/bursar_realtest/bursar.db")
    shutil.rmtree(pathlib.Path(os.environ["BURSAR_DB"]).parent, ignore_errors=True)
    bs.load_dotenv()

    from firehose import Firehose, FirehoseConfig, _seed_budgets
    conn = db.connect()
    _seed_budgets(conn)
    # Give teams generous budgets so we exercise servicing (Stripe) broadly.
    for t in ["research", "sales", "engineering", "support", "marketing"]:
        db.upsert_budget(conn, t, 50.0, period="realtest", reset_spent=True)

    fh = Firehose(conn, FirehoseConfig(dup_rate=0.25, seed=args.seed))
    fh.emit(args.n)

    meter = bs.make_meter(strict=True)  # real Stripe; raises if not a test key
    bm.reset_calls()
    print(f"=== Bursar REAL e2e: N={args.n}  (Nemotron escalation ON + live Stripe) ===")
    t0 = time.time()
    res = disp.run_tick(conn, meter=meter)
    dt = time.time() - t0

    rows = db.list_ledger(conn)
    settled = [r for r in rows if r["settled"] == 1 and r["stripe_event_id"]]
    ledger_total = round(sum(r["total"] for r in rows), 6)
    calls = bm.call_counts()

    print(f"\ntick: {res.as_dict()}")
    print(f"wall-clock: {dt:.1f}s  ({args.n/dt:.1f} queries/s incl. model calls)")
    print(f"\nNemotron tier mix (calls actually made):")
    if calls:
        for model, c in sorted(calls.items(), key=lambda x: -x[1]):
            print(f"  {c:4d}  {model}")
    else:
        print("  (none — all queries scored by heuristic / skipped)")
    print(f"  heuristic-only (escalation skipped): "
          f"{args.n - sum(calls.values())} of {args.n}")
    print(f"\nStripe settlement (LIVE test mode):")
    print(f"  ledger rows         : {len(rows)}")
    print(f"  settled (id+settled): {len(settled)}")
    print(f"  ledger total        : ${ledger_total}")
    print(f"  tick bill           : ${res.bill}")
    print(f"  reconciles to cent  : {abs(ledger_total - res.bill) < 1e-6}")
    print(f"  sample event id     : {settled[0]['stripe_event_id'] if settled else None}")
    ok = (len(settled) == len(rows) and abs(ledger_total - res.bill) < 1e-6)
    print(f"\nRESULT: {'PASS' if ok else 'FAIL'}")
    conn.close()
    return 0 if ok else 1


# --------------------------------------------------------------------------
def _ramp(label, make_task, total, concurrencies):
    print(f"=== {label} ceiling ramp (total≈{total}/step) ===")
    print(f"{'concurrency':>11} {'ok':>5} {'err':>4} {'wall(s)':>8} {'rate/s':>8}")
    for c in concurrencies:
        ok = err = 0
        t0 = time.time()
        with cf.ThreadPoolExecutor(max_workers=c) as ex:
            futs = [ex.submit(make_task, i) for i in range(total)]
            for f in cf.as_completed(futs):
                try:
                    f.result(); ok += 1
                except Exception:
                    err += 1
        dt = time.time() - t0
        print(f"{c:>11} {ok:>5} {err:>4} {dt:>8.2f} {ok/dt:>8.2f}")


def cmd_stripe_ramp(args):
    bs.load_dotenv()
    meter = bs.make_meter(strict=True)
    teams = ["research", "sales", "engineering", "support", "marketing"]
    def task(i):
        r = meter(teams[i % len(teams)], "nemotron-nano", 1000, 0.05)
        if not r:
            raise RuntimeError("meter returned None")
        return r
    _ramp("Stripe MeterEvent", task, args.total, [1, 5, 10, 20, 40])
    return 0


def cmd_nemotron_ramp(args):
    bs.load_dotenv()
    FAST = os.environ.get("BURSAR_NEMOTRON_MODEL_FAST",
                          "nvidia/llama-3.3-nemotron-super-49b-v1.5")
    prompts = [
        "reformat the onboarding notes for the platform team this quarter",
        "summarize this week's standup for the data pipeline",
        "compare last month's metrics for the checkout flow",
        "draft a status update about the SSO rollout",
    ]
    def task(i):
        return bm.score_value_with_model(prompts[i % len(prompts)] + f" (ref {i})", model=FAST)
    _ramp(f"Nemotron fast ({FAST.split('/')[-1]})", task, args.total, [4, 8, 16, 32])
    return 0


# --------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="Bursar real-integration checks")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("e2e"); p.add_argument("--n", type=int, default=80); p.add_argument("--seed", type=int, default=7)
    p = sub.add_parser("stripe-ramp"); p.add_argument("--total", type=int, default=80)
    p = sub.add_parser("nemotron-ramp"); p.add_argument("--total", type=int, default=40)
    args = ap.parse_args()
    return {
        "e2e": cmd_e2e,
        "stripe-ramp": cmd_stripe_ramp,
        "nemotron-ramp": cmd_nemotron_ramp,
    }[args.cmd](args)


if __name__ == "__main__":
    raise SystemExit(main())
