# Bursar — Discord submission post

> Fill before posting: `<github repo>`, `<video>`, `<bursar site>`, the passing-test count, and (optionally) a measured before/after savings %.

---

**BURSAR — Price every token before it burns.**

🔗 Code: `<github repo>`  ·  🎬 Demo: `<video>`  ·  🟢 Live: `<bursar site>`

**TL;DR —** Bursar is a *pre-execution compute exchange* for Hermes. Every query is priced, deduped, ranked by value-per-token, and settled against a hard budget **before a single token is spent**. It's a Hermes plugin (no core fork) that drops a live "Trading Floor" tab into any stock install — and it governs your **real** agent traffic, not a slideshow.

**The problem**
Enterprise AI budgets went **$1.2M → $7M** in two years, token consumption is **up 13x**, and roughly **31% of production queries are near-duplicates** — teams pay frontier prices to answer the same question five different ways, on the "best model" for everything. Industry estimates put **50–90% of inference spend as pure waste**. And today's budget alerts fire *after* the money's gone. Nothing decides what's actually worth running.

**What Bursar does** — runs every query through a clearing pipeline before it executes:

- **Price** — real, sourced model rates (5-tier catalog, ~500× spread). No fantasy numbers.
- **Dedup** — semantic near-duplicates collapse into one inference; repeats return the cached answer for ~free. The 31% never gets paid for twice.
- **Rank** — the dispatcher fills the budget highest **value-per-token first** — a knapsack market, like a trading desk allocating scarce capital. Best queries clear first.
- **Gate** — hard per-team budget caps enforced **pre-execution**. A tranche can't be overspent — the overrun doesn't *happen*.
- **Route** — trivia → nano, high-stakes → frontier. Cheapest model that clears the value tier's bar, killing the "best model for everything" tax.
- **Settle** — every serviced query meters a **Stripe trading fee** — the chargeback rail *and* the throttle. A query whose worth can't clear its cost + fee is priced out. Waste starves itself.

**It governs real traffic, not slides.** A native Hermes gate plugin sits directly in the LLM execution path of your real chats, subagents, and tool calls — observing, pricing, deduping, and down-routing live. **Fail-open by construction:** if Bursar errors, the call goes through; it can never block your own work. Arm enforcement with a single shield toggle. Real turns light up the Trading Floor instantly over a WebSocket.

**Valuation by Nemotron.** NVIDIA's Nemotron 3 scores each query's business value (Super 49B for the ambiguous middle, Ultra 550B for high-stakes) — running on-prem, so prompts and spend data never leave your walls. Everything degrades gracefully: no key → heuristic scoring + local-only ledger; nothing ever crashes the agent.

**Built on live infra, no mocks:** Hermes Agent (plugin + gate hooks, zero core fork) · Nemotron 3 Ultra/Super valuation · Stripe metered billing (test mode, micro-dollar precision, reconciles to the cent) · local 256-d semantic dedup (MiniLM opt-in) · SQLite WAL shared state · React desktop Trading Floor. Demo and live traffic run off the same ledger.

**The demo (60s, end-to-end):** an enterprise firehose pours in → queries sort by value-per-token → near-dupes collapse → a low-value flood can't clear the fee and gets starved → high-value work clears → Stripe meters each settlement (real sandbox receipts) → flip the before/after toggle and watch the bill reset to what actually shipped.

**Why it matters:** at thousands of agents, LLM spend isn't a billing problem — it's a *market* problem. Everyone this weekend is building agents that **spend**. Bursar is the exchange that decides what's **worth** spending on, before the token burns.
