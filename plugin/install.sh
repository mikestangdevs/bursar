#!/usr/bin/env bash
#
# Bursar — installer for the Hermes plugin (the "Trading Floor" dashboard tab).
#
# Drops a self-contained Bursar plugin into ~/.hermes/plugins/ so any stock
# Hermes install picks it up on the next dashboard start. No Hermes core
# changes, no build step (the frontend bundle ships prebuilt).
#
# Usage:
#   bash install.sh                 # install the dashboard tab + engine
#   bash install.sh --with-gate     # also stage the live FinOps gate (opt-in)
#   HERMES_HOME=/path bash install.sh   # install into a non-default Hermes home
#
set -euo pipefail

HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
DEST="$HERMES_HOME/plugins"
WITH_GATE=0
for arg in "$@"; do
  case "$arg" in
    --with-gate) WITH_GATE=1 ;;
    -h|--help) grep '^#' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "Unknown option: $arg" >&2; exit 2 ;;
  esac
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# --- Locate sources: repo mode (run from the clone) or bundle mode (tarball) ---
if [ -d "$SCRIPT_DIR/dashboard" ] && [ -d "$SCRIPT_DIR/engine" ]; then
  # Bundle mode: install.sh sits next to dashboard/ and engine/.
  DASHBOARD_SRC="$SCRIPT_DIR/dashboard"
  ENGINE_SRC="$SCRIPT_DIR/engine"
  GATE_SRC="$SCRIPT_DIR/bursar_gate"
elif [ -d "$SCRIPT_DIR/dashboard" ] && [ -d "$SCRIPT_DIR/../../optional-skills/finance/bursar/scripts" ]; then
  # Repo mode: this file lives at <repo>/plugins/bursar/install.sh.
  DASHBOARD_SRC="$SCRIPT_DIR/dashboard"
  ENGINE_SRC="$SCRIPT_DIR/../../optional-skills/finance/bursar/scripts"
  GATE_SRC="$SCRIPT_DIR/../bursar_gate"
else
  echo "✗ Could not find Bursar sources next to this script." >&2
  echo "  Run it from the repo (plugin/install.sh) or an extracted bundle." >&2
  exit 1
fi

if [ ! -f "$DASHBOARD_SRC/dist/index.js" ]; then
  echo "✗ Prebuilt frontend missing at $DASHBOARD_SRC/dist/index.js" >&2
  echo "  Build it first: cd dashboard/frontend && npm install && npm run build" >&2
  exit 1
fi

echo "▸ Installing Bursar into $DEST/bursar"
mkdir -p "$DEST/bursar"

# rsync keeps the copy clean (drops caches); fall back to cp if absent.
copy() {
  if command -v rsync >/dev/null 2>&1; then
    rsync -a --delete \
      --exclude '__pycache__' --exclude '*.pyc' --exclude 'node_modules' \
      "$1/" "$2/"
  else
    rm -rf "$2"; mkdir -p "$2"; cp -R "$1/." "$2/"
  fi
}

copy "$DASHBOARD_SRC" "$DEST/bursar/dashboard"
copy "$ENGINE_SRC"    "$DEST/bursar/engine"
echo "  ✓ dashboard tab + engine staged (frontend bundle is prebuilt)"

if [ "$WITH_GATE" = "1" ]; then
  if [ -d "$GATE_SRC" ]; then
    copy "$GATE_SRC" "$DEST/bursar_gate"
    # Strip the repo-relative engine; the gate falls back to ../bursar/engine.
    rm -rf "$DEST/bursar_gate/engine" "$DEST/bursar_gate/__pycache__"
    echo "  ✓ live gate staged at $DEST/bursar_gate (NOT yet enabled)"
    GATE_STAGED=1
  else
    echo "  ! --with-gate requested but gate source not found at $GATE_SRC" >&2
    GATE_STAGED=0
  fi
else
  GATE_STAGED=0
fi

cat <<EOF

✓ Bursar installed.

Next:
  1. Seed the floor with demo traffic (optional, runs dry without Stripe):
       python3 "$DEST/bursar/engine/firehose.py" --once 400 --dup-rate 0.31 --burst --seed 3 --reset

  2. Start (or restart) the Hermes dashboard:
       python3 -m hermes_cli.main dashboard
     Open it and look for the "Trading Floor" tab.
EOF

if [ "$GATE_STAGED" = "1" ]; then
  cat <<EOF

  3. (Live governance — opt-in) To let Bursar observe your REAL Hermes traffic,
     enable the agent plugin in $HERMES_HOME/config.yaml:
       plugins:
         enabled:
           - bursar_gate
     It loads OFF by default — observe-only until you set BURSAR_ENFORCE=1.
EOF
fi

echo
