"""N1 migration safety: an EXISTING pre-tenant DB must gain the tenant column,
backfill old rows to 'local', and keep working (the user's live ~/.hermes DB path).
"""
import os, sys, tempfile, sqlite3, pathlib
ENGINE = pathlib.Path("/Users/michaelstang/Desktop/hermes-hackathon/hermes-agent/optional-skills/finance/bursar/scripts")
sys.path.insert(0, str(ENGINE))

tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False); tmp.close()
os.environ["BURSAR_DB"] = tmp.name

# 1) Hand-build an OLD-shape queries table (NO tenant column) + an old live row.
raw = sqlite3.connect(tmp.name)
raw.execute("""CREATE TABLE queries (
  id TEXT PRIMARY KEY, team TEXT NOT NULL, source TEXT NOT NULL DEFAULT 'demo',
  session_id TEXT, turn_id TEXT, prompt TEXT NOT NULL, embedding TEXT, embed_model TEXT,
  value REAL, tier TEXT, rationale TEXT, est_tokens INTEGER, reasoning_tokens INTEGER,
  est_cost REAL, fee REAL, vpt REAL, chosen_model TEXT, status TEXT NOT NULL DEFAULT 'pending',
  dedup_of TEXT, temporal_class TEXT, reuse_mode TEXT, result TEXT,
  created_at INTEGER NOT NULL, decided_at INTEGER)""")
raw.execute("INSERT INTO queries (id, team, source, prompt, status, created_at) "
            "VALUES ('old1','live:old','live','legacy question',' pending',0)")
raw.commit()
cols_before = {r[1] for r in raw.execute("PRAGMA table_info(queries)")}
raw.close()
assert "tenant" not in cols_before, "precondition: old DB has no tenant column"

# 2) connect() runs _migrate -> should ADD COLUMN tenant DEFAULT 'local'.
import bursar_db as db
with db.connect_closing() as conn:
    cols_after = {r["name"] for r in conn.execute("PRAGMA table_info(queries)")}
    backfilled = conn.execute("SELECT tenant FROM queries WHERE id='old1'").fetchone()["tenant"]

ok = []
ok.append(("migration added tenant column", "tenant" in cols_after))
ok.append(("legacy row backfilled to 'local'", backfilled == "local"))

# 3) Idempotent: a second connect/migrate must not error or duplicate.
try:
    with db.connect_closing() as conn:
        conn.execute("SELECT tenant FROM queries").fetchone()
    ok.append(("second migrate is idempotent (no error)", True))
except Exception as e:
    ok.append((f"second migrate raised: {e}", False))

for name, cond in ok:
    print(("PASS " if cond else "FAIL ") + name)
fails = [n for n, c in ok if not c]
print("\n" + ("ALL PASS" if not fails else f"FAILURES: {fails}"))
sys.exit(1 if fails else 0)
