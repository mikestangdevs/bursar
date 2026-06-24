# Installing Bursar on Hermes

Bursar ships as a **drop-in Hermes dashboard plugin** — it adds a "Trading
Floor" tab to any stock Hermes. No Hermes core changes, no build step (the
frontend bundle is prebuilt and committed).

Hermes auto-discovers anything under `~/.hermes/plugins/` on dashboard start,
so "install" just means staging the plugin there. The installer does that.

## Three ways to install

**1. From a clone (works today)** — run from the repo root:
```bash
bash plugin/install.sh             # dashboard tab + engine
bash plugin/install.sh --with-gate # also stage the live FinOps gate
```

**2. Hosted one-liner** — the published path. Build the bundle and serve two
files from the site host (`bursar-hermes.com`): `get.sh` (at `/install.sh`) and
the tarball (at `/bursar-plugin.tar.gz`):
```bash
cp -r plugin bursar-plugin && tar -czf bursar-plugin.tar.gz bursar-plugin && rm -rf bursar-plugin
# upload bursar-plugin.tar.gz + get.sh, then anyone runs:
curl -fsSL https://bursar-hermes.com/install.sh | bash
```
`get.sh` downloads the tarball and runs `install.sh` (defaults the URL to
`https://bursar-hermes.com/bursar-plugin.tar.gz`, overridable via `BURSAR_TARBALL_URL`).

**3. Manual**
```bash
mkdir -p ~/.hermes/plugins/bursar
cp -r plugin/dashboard   ~/.hermes/plugins/bursar/dashboard
cp -r plugin/engine      ~/.hermes/plugins/bursar/engine
cp -r plugin/bursar_gate ~/.hermes/plugins/bursar_gate   # optional: live gate
```

## After installing

```bash
# optional: seed the floor with demo traffic (runs dry without a Stripe key)
python3 ~/.hermes/plugins/bursar/engine/firehose.py --once 400 --dup-rate 0.31 --burst --seed 3 --reset

# start the dashboard — the "Trading Floor" tab appears
python3 -m hermes_cli.main dashboard
```

## How it stays self-contained

The plugin finds its engine in this order: `BURSAR_SCRIPTS` env → co-located
`engine/` (what the installer stages) → repo-relative path (dev checkout). So a
copied-out plugin works with no env vars. The optional `bursar_gate` finds the
engine in its sibling `bursar/engine`.

## The live gate (opt-in)

`--with-gate` stages `~/.hermes/plugins/bursar_gate` but does **not** enable it.
To let Bursar observe your real Hermes LLM traffic, add it to `~/.hermes/config.yaml`:
```yaml
plugins:
  enabled:
    - bursar_gate
```
It loads OFF by default (observe-only) — enforcement requires `BURSAR_ENFORCE=1`,
and the gate is fail-open by construction, so it can never block your own work.
