# Bursar: the internal compute exchange

**Bursar turns your organization's AI spend into a live market.** Every query is
priced, deduplicated, ranked by value-per-token, gated against a hard per-team
budget *before* inference, and settled through Stripe — so waste prices itself
out instead of showing up on next month's bill.

Built on **Hermes** (Nous Research), with **NVIDIA Nemotron** as the valuation
brain and **Stripe** as the settlement rail.

## Demo
https://github.com/user-attachments/assets/081c26c3-613e-4751-80e5-1406a2a44ade

---

## The problem

Enterprise AI bills are exploding, and a large fraction of the spend is pure
waste: the same question asked five different ways, low-value queries consuming
premium models, and no hard cap that stops a runaway agent loop *before* the
money is gone. Budgets today are post-hoc — Stripe (and every other tool) tells
you what you already spent. Bursar's thesis is the opposite: a **pre-execution
market** that allocates compute by value and refuses waste at the door.

## How it works

```
query ──▶ price ──▶ value-score ──▶ dedup ──▶ budget gate ──▶ route ──▶ settle
         (catalog)   (Nemotron)    (semantic)  (hard cap)    (vpt)    (Stripe)
```

- **Price** every query against a real, sourced model-cost catalog.
- **Value-score** it with **NVIDIA Nemotron** (tiered: free heuristic for the
  obvious, a fast Nemotron for the ambiguous middle, the big reasoning model only
  for genuine high-stakes — because valuing a $0.04 query with a 10s reasoning
  call is itself the waste we're fighting).
- **Deduplicate** semantically — near-identical questions collapse to one
  inference; the rest are served from the prior answer for ~free. Embeddings run
  in **pure NumPy** (a self-contained MiniLM, no PyTorch) so dedup is dependency-free.
- **Gate** against a hard per-team budget — *before* inference. The live gate is
  **fail-open by construction**: it observes, prices, dedups, and down-routes real
  Hermes traffic but can never block your own turn.
- **Route** by value-per-token — high-value work sails through, low-value work
  that can't clear its fee gets starved.
- **Settle** through **Stripe** usage-based metering — a finance-grade,
  per-team chargeback ledger, reconciled to the cent.

## Three ways to see it

| Surface | What it is | Where |
|---|---|---|
| **Trading Floor (desktop)** | The full, integrated experience in the video: live floor + the title-bar enforcement shield + in-chat dedup cards. | [`desktop-ui/`](desktop-ui/) (source overlay on the Hermes desktop app) |
| **Dashboard plugin** | A drop-in Trading Floor *tab* on any stock Hermes — no core changes, no build step. | [`plugin/`](plugin/) |
| **Live FinOps gate** | Opt-in. Watches your real Hermes LLM traffic and prices/dedups/down-routes it (fail-open). | [`plugin/bursar_gate/`](plugin/) |

> **Honest note on the two UIs:** the *drop-in plugin* mounts a working Trading
> Floor **tab**. The title-bar shield and the in-chat dedup card can't be done by
> a plugin (they live in core desktop files), so the **fully integrated**
> experience shown in the video is a small source overlay — see
> [`desktop-ui/README.md`](desktop-ui/README.md). Both share one backend and one
> SQLite store, so they never drift.

## Quick start (drop-in plugin)

Bursar installs onto a stock Hermes — Hermes auto-discovers anything under
`~/.hermes/plugins/`.

```bash
# from this repo
bash plugin/install.sh              # Trading Floor tab + engine
bash plugin/install.sh --with-gate  # also stage the live FinOps gate (opt-in)

# seed the floor with synthetic enterprise traffic (runs dry without a Stripe key)
python3 ~/.hermes/plugins/bursar/engine/firehose.py --once 400 --dup-rate 0.31 --burst --seed 3 --reset

# start the dashboard — the "Trading Floor" tab appears
python3 -m hermes_cli.main dashboard
```

Full installer details (hosted one-liner, manual path, the opt-in gate config)
are in [`plugin/INSTALL.md`](plugin/INSTALL.md).

### Configuration (all optional, all fail-open)

| Env var | Purpose |
|---|---|
| `STRIPE_SECRET_KEY` | **Test-mode only** (`sk_test_…`). A live key is *refused*; without one, the ledger records locally (`settled=0`). |
| `BURSAR_NEMOTRON_API_KEY` (or `NVIDIA_API_KEY`) | Nemotron valuation. Falls back to the free heuristic if absent. |
| `BURSAR_ENFORCE=1` | Arms the live gate's enforcement (default: observe-only). |

No keys are bundled. Bring your own; everything degrades gracefully without them.

## What's proven

- **Core loop** — price · Nemotron-score · semantic dedup · value-per-token route
  · hard budget gate · Stripe settle — end to end.
- **Live gate** intercepts real Hermes chat traffic, fail-open.
- **Scale & multi-tenant hardening** (tenant isolation, time-based turn
  eviction, injection-resistant turn detection, retention) — see
  [`docs/SCALE-HARDENING.md`](docs/SCALE-HARDENING.md).
- **Temporal-class router** — re-asks of time-sensitive questions are
  *augmented and re-run fresh against a dated baseline*, never served stale —
  see [`docs/TEMPORAL_ROUTER_SPEC.md`](docs/TEMPORAL_ROUTER_SPEC.md).
- Adversarial test suites in [`tests/`](tests/) (each spins up its own temp DB;
  they never touch the live `~/.hermes/bursar/bursar.db`).

> **On the numbers:** the **firehose** is a *synthetic* enterprise-traffic
> generator — it demonstrates how a rapidly-prompting org's traffic gets
> priced/deduped/gated/routed at volume. The **live gate** meters *real* spend on
> your own traffic. The repo never fabricates a measured number; where a figure
> is synthetic (the demo firehose) or industry context (market-size claims),
> it's labeled as such.

## Sponsor integrations

- **Hermes (Nous Research)** — Bursar is a native citizen: a dashboard plugin +
  engine + an opt-in gate, scoring via Hermes' auxiliary model client, tailing
  the same WAL-backed SQLite the dashboard reads. No core fork for the plugin
  path. Field notes in [`docs/ECOSYSTEM-FEEDBACK.md`](docs/ECOSYSTEM-FEEDBACK.md).
- **NVIDIA Nemotron** — the valuation/routing brain; the latency/throughput
  characteristics we measured are *why* the design is tiered (a great
  local-DGX-Spark workload). See the ecosystem doc.
- **Stripe** — usage-based metering as an internal chargeback rail, test mode,
  reconciled to the cent. Gaps we hit (no native pre-spend cap, fire-and-forget
  meters, sub-cent units) are documented — and the pre-spend cap gap *is* Bursar's
  value prop.

## Repo layout

```
plugin/        drop-in Hermes plugin: dashboard tab + engine + opt-in gate
desktop-ui/    source overlay for the fully-integrated desktop experience
site/          marketing site (static; Hermes-replica aesthetic)
docs/          ecosystem feedback + scale-hardening + temporal-router spec
tests/         adversarial / integration suites (standalone, temp-DB)
```

## Privacy

Bursar is built to run **entirely local** — on a DGX Spark or any box you
control. Your prompts and spend data never have to leave your walls; the
embedding model is local, the ledger is local SQLite, and Stripe sees only
metered usage events (test mode here).

