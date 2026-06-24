"""Read-only monitor for the IN-APP verification of N1-N6.

Run this after restarting the Hermes gateway and sending the test prompts. It reads
the LIVE bursar DB (~/.hermes/bursar/bursar.db) without writing, and reports the
N-series signals so we can confirm each fix is live.

  python3 monitor_inapp.py          # snapshot
  watch -n 2 python3 monitor_inapp.py   # live (optional)
"""
import os, sqlite3, pathlib, time

DB = pathlib.Path(os.environ.get("BURSAR_DB") or (pathlib.Path.home() / ".hermes/bursar/bursar.db"))
print(f"DB: {DB}  exists={DB.exists()}")
if not DB.exists():
    raise SystemExit("live DB not found")

# Read-only connection (never mutate the live DB from the monitor).
conn = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
conn.row_factory = sqlite3.Row

cols = {r["name"] for r in conn.execute("PRAGMA table_info(queries)")}
has_tenant = "tenant" in cols
print(f"tenant column present (N1 migration ran): {has_tenant}")
if not has_tenant:
    print("  -> gateway has NOT yet run the new engine. Restart with new code, then re-run.")

sel_tenant = "tenant," if has_tenant else "'(n/a)' AS tenant,"
rows = conn.execute(
    f"SELECT id, {sel_tenant} session_id, status, tier, temporal_class, reuse_mode, "
    f"substr(prompt,1,52) AS p FROM queries WHERE source='live' "
    f"ORDER BY created_at DESC LIMIT 15"
).fetchall()

print(f"\nLast {len(rows)} LIVE query rows (newest first):")
print(f"{'status':9} {'tenant':8} {'tclass':9} {'reuse':11} {'tier':9} prompt")
print("-"*100)
served = stateful = augmented = 0
for r in rows:
    print(f"{r['status']:9} {str(r['tenant'])[:8]:8} {str(r['temporal_class'] or '-'):9} "
          f"{str(r['reuse_mode'] or '-'):11} {str(r['tier'] or '-'):9} {r['p']}")
    if r["reuse_mode"] == "serve": served += 1
    if r["reuse_mode"] in ("comparison", "plain"): augmented += 1
    if r["temporal_class"] == "stateful": stateful += 1

ded = conn.execute(
    "SELECT created_at, payload FROM events WHERE kind='deduped' ORDER BY id DESC LIMIT 5"
).fetchall()
print(f"\nRecent dedup events: {len(ded)}")
for e in ded:
    print("  ", time.strftime('%H:%M:%S', time.localtime(e['created_at'])), e["payload"][:120])

# N5 — augment rows carry prior/injected char counts in the serviced event shadow.
import json as _json
print("\nN5 — augment cap stats (from recent serviced-event shadow):")
n5_rows = 0
for e in conn.execute("SELECT payload FROM events WHERE kind='serviced' ORDER BY id DESC LIMIT 25"):
    try:
        sh = (_json.loads(e["payload"]) or {}).get("shadow") or {}
    except Exception:
        sh = {}
    if sh.get("dedup_reused") and ("prior_chars" in sh):
        n5_rows += 1
        print(f"   reused_of={sh.get('reused_of')} prior_chars={sh.get('prior_chars')} "
              f"injected_chars={sh.get('injected_chars')} capped={sh.get('prior_capped')}")
if not n5_rows:
    print("   (no augment rows with cap stats yet — run the expensive re-ask to trigger augment)")

print("\n--- N-series in-app signals ---")
print(f"  N1  reuse rows tagged tenant     : {'YES' if has_tenant else 'NO (migrate first)'}")
print(f"  N2/N5 augment (deep re-ask) rows : {augmented}")
print(f"  reuse-SERVE rows                 : {served}")
print(f"  N4  stateful (continuation) rows : {stateful}  (a 'continue'/'what did we decide' turn should appear here, NOT served)")
print(f"  N6  retention                    : default OFF (no rows deleted); set BURSAR_RETENTION_DAYS to enable")
