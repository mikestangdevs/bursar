# Bursar — Hermes desktop integration (overlay)

This is the **full, integrated experience** shown in the demo video: the Bursar
pages rendered inline in the Hermes desktop app, plus two touches woven into
Hermes' own chrome —

- a **shield toggle in the title bar** (emerald when Bursar is enforcing on your
  live LLM traffic, click to arm/disarm), and
- an **in-chat dedup card**: when Bursar serves an answer from cache instead of
  re-running inference, the assistant message renders a styled "served from a
  prior answer" card with a jump-to-original-session button.

These two touches are **not** something a Hermes plugin can do — plugins mount a
dashboard tab, they can't reach the title bar or the chat markdown renderer. So
this surface is shipped as a small **source overlay** onto the Hermes desktop
app, not as a drop-in. (The drop-in `plugin/` gives you a working Trading Floor
*tab* on any stock Hermes — see the repo root README for that path.)

## What's here

- `hermes-desktop-integration.patch` — the diff against 11 core desktop files
  (title bar, chat markdown renderer, sidebar, command palette, routes,
  app-shell, icons, styles, types, package.json). ~600 lines, all additive.
- `app-bursar/` — the self-contained Bursar surface: the four pages (Trading
  Floor, Ledger, Budgets, Routing) and their components/hooks. This is new
  source, not a patch.

## Applying it

Pinned base: **`NousResearch/hermes-agent` @ `c6c8abbadb`**. The patch is small
and additive, so it applies cleanly across nearby commits; if a hunk rejects,
the conflicts are trivial (import lists / a route entry / a styles block).

```bash
# from a hermes-agent checkout
git checkout c6c8abbadb            # or your current main; see note above

# 1) drop in the Bursar surface
cp -r /path/to/this/desktop-ui/app-bursar apps/desktop/src/app/bursar

# 2) apply the chrome integration
git apply /path/to/this/desktop-ui/hermes-desktop-integration.patch

# 3) build + run the desktop app
cd apps/desktop && npm install && npm run build
```

The backend is **100% reused**: the desktop app spawns `hermes_cli.main
dashboard` as its local backend, which mounts Bursar's plugin API (install the
`plugin/` engine + dashboard the usual way, or run from a checkout). Engine,
gate, dashboard, and the desktop UI all read the same WAL-backed SQLite at
`~/.hermes/bursar/bursar.db`, so nothing drifts.

> Note: the title-bar shield only appears when a Bursar engine answers the
> bridge, so a vanilla Hermes build (no Bursar) shows nothing — the overlay is
> inert without the engine installed.
