# Bursar — Ecosystem Feedback

*Field notes from building Bursar (an internal AI-compute exchange) on Hermes,
Stripe, and NVIDIA Nemotron. Every item here is something we actually hit while
building, not a hypothetical. Where a gap exists, we note both the workaround we
shipped and the primitive we wish existed — several of these gaps **are** the
reason Bursar needs to exist.*

Judged on usefulness, viability, presentation — this doc is the "we used your
stuff for real and here's what we learned" artifact.

---

## Stripe — usage-based billing as an internal compute-chargeback rail

We use Stripe Billing **meter events** (test mode) as the settlement layer: each
serviced query emits the trading fee + token cost, producing a finance-grade
per-team chargeback ledger.

**1. Budgets are post-hoc, not pre-execution.** Stripe tells you what you spent;
it has no native *hard pre-spend cap* that refuses the next unit of usage before
it's incurred. For agentic AI spend — where a runaway loop can burn thousands in
minutes — "alert after the money's gone" is the wrong shape. Bursar's whole
thesis is the **pre-execution hard gate**: a query is refused *before* inference
if the team's envelope is exhausted. *Wish: a Stripe "budget guard" primitive —
a cap that can deny a meter event / return a "would exceed budget" signal
synchronously.* (This gap is precisely Bursar's value prop.)

**2. Meter events are fire-and-forget; no synchronous settlement ack.** `MeterEvent.create`
returns before aggregation; there's no immediate "this was accepted and counted"
confirmation usable as a settlement receipt. We model it honestly: the ledger
row is written with `settled=1` when an event id comes back and `settled=0` on
the local-only fallback, but a true two-phase "reserve → confirm" would let us
gate spend against *confirmed* usage. *Wish: an optional synchronous meter
acknowledgement, or a reserve/commit pair for spend-control use cases.*

**3. No sub-cent / fractional unit.** Per-query costs are fractions of a cent.
To avoid float drift we meter **integer micro-dollars** (`round(usd * 1e6)`).
Works, but it means every downstream consumer has to know the unit scale.
*Wish: a documented fractional-unit pattern for high-volume, low-value-per-event
metering (which AI inference inherently is).*

**4. Live-key blast radius.** A pasted `sk_live_…` would meter a real account.
We built a hard guardrail that refuses any non-`sk_test_` key. *Wish: a
test-only restricted key class that is structurally incapable of touching live
data, so a fat-finger can't bill production.*

**Measured ceiling (our `realcheck.py stripe-ramp`, test mode, 80 events/step):**

| concurrency | events/s | rate-limit errors |
|---|---|---|
| 1 | 2.6 | 0 |
| 5 | 13.2 | 0 |
| 10 | 23.2 | 0 |
| 20 | 35.3 | 0 |
| 40 | 61.7 | 1 (429) |

Clean to ~35/s, first 429 at ~62/s. Our fallback caught the 429 and recorded
that query local-only (`settled=0`) with no crash — i.e. metering degrades
gracefully under throttle, which is the behavior you want for a billing rail.
Extrapolated: even at this ceiling, metering **every** query at 1M scale is
hours and creates ~500k events — which is why Bursar meters real settlement at
realistic volume and proves throughput locally.

**What worked well:** meter + customer setup is idempotent and fast; test mode
is faithful to prod; the Python SDK's billing surface is clean. Settlement
end-to-end (meter → ledger reconcile to the cent) came together in an afternoon.

---

## NVIDIA Nemotron — the valuation/routing brain

Nemotron rates each query's business value (0–100) → tier → rationale. We run it
through Hermes's auxiliary model client, pointed at the hosted NIM endpoint
(`integrate.api.nvidia.com/v1`), model family Nemotron 3 / Llama-Nemotron.

**1. Reasoning latency makes per-call valuation expensive.** `nemotron-3-ultra-550b`
is ~8–12 s per scoring call; `llama-3.3-nemotron-super-49b` ~1.6 s. For a control
plane that prices *every* query, a 10 s valuation to decide whether to run a
$0.04 query is itself the waste we're fighting. This directly motivated Bursar's
**tiered escalation**: heuristic (free) for the obvious, a fast Nemotron for the
ambiguous middle, Ultra only for genuine high-stakes. *Wish: a first-class small,
low-latency "router/valuator" Nemotron tuned for fast structured judgments — an
ideal local-DGX-Spark workload.*

**2. Reasoning models need an explicit "thinking off" for structured output.**
Out of the box the reasoning models emit chain-of-thought and, with a small
token budget, never reach the JSON (they get truncated mid-thought). The fix —
prepending `detailed thinking off` to the system prompt — is essential but not
obvious from the endpoint. *Wish: a documented, parameterized structured-output /
reasoning-toggle mode (e.g. a `response_format: json` that implies thinking-off)
surfaced in the OpenAI-compatible API.*

**3. Model discovery.** The OpenAI-compatible `/v1/models` lists 100+ ids; which
Nemotron is the "valuation" pick vs the safety/embedding/parse variants isn't
obvious without docs. *Wish: capability tags (chat / reasoning / embed / guard)
in the model list.*

**Measured ceiling (our `realcheck.py nemotron-ramp`, hosted NIM, fast model,
40 calls/step):**

| concurrency | ok | errors | calls/s |
|---|---|---|---|
| 4 | 38 | 2 | 0.37 |
| 8 | 34 | 6 | 0.49 |
| 16 | 20 | 20 | 0.60 |
| 32 | 5 | 35 | 0.17 |

The hosted endpoint throttles hard: sustained useful throughput peaks ~0.5/s,
and **concurrency backfires** — half the calls fail at 16-way, ~88% at 32-way,
and under sustained load even the "fast" 49B runs ~10 s/call (vs ~1.6 s
unloaded). Every failure fell back to the heuristic cleanly, so the engine
never stalled. Two takeaways: (1) this is why per-query Ultra valuation can't be
the bulk path — it *forces* a tiered/escalation design; (2) a production
AI-FinOps control plane needs either much higher inference rate limits or a
**dedicated low-latency valuator/router model** (a great local-DGX-Spark
workload — keep the heavy reasoning model for the high-stakes minority).

**What worked well:** the OpenAI-compatible surface meant zero custom transport;
`detailed thinking off` gives crisp, well-reasoned valuations; the rationale
quality at Ultra is genuinely good ("Payments security breach = financial/legal/
reputational catastrophe"). The model lineup spans exactly the latency/quality
tiers our escalation ladder needs.

---

## Hermes — the platform Bursar is a citizen of

Bursar ships as a Hermes **skill** (engine) + **dashboard plugin** (UI) and
scores via Hermes's **auxiliary model client**. Building native paid off. (We
also prototyped an autonomous **cron** + **`delegate_task`** worker loop, but
cut it: Bursar is a *pre-execution* shield that fail-opens on live traffic, so
the real work already runs in the chat turn — a separate executor was redundant.
The cron/`delegate_task` primitives themselves worked fine.)

**1. No lightweight "complete this prompt" helper is the documented path.**
The obvious entry, `agent.chat()`, is the full agent loop (session DB, toolsets,
stdout) — wrong for a utility JSON call. The right tool,
`agent.auxiliary_client.get_text_auxiliary_client()`, we found only by reading
how `kanban_specify` makes its model call. *Wish: `auxiliary_client` documented
as the supported "internal utility LLM call" API for skill/plugin authors.*

**2. Plugin SDK has no charts/table primitives.** The dashboard SDK exposes
shadcn primitives but no charts, so a data-heavy plugin must ship its own
build (we use evilcharts + a React-externalized Vite build). Reasonable, but
worth a note for plugin authors expecting batteries-included viz.

**What worked well:** plugin auto-discovery, the WAL-backed shared SQLite the
dispatcher writes and the dashboard tails, and the auxiliary client reusing the
operator's configured provider (set Nemotron as `model.default` and Bursar uses
it, with provider fallback inherited for free) — these made "be a good Hermes
citizen" the path of least resistance, which is exactly right.

---

*Maintained as we build — see `PROGRESS.md` for the running log and
`feature_list.json` for state.*
