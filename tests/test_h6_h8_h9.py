"""Adversarial proof of the H6/H8/H9 freshness/work-cache features, driven through
the REAL shipping gate + engine modules (the exact files the running gateway loads)
against REAL files on disk and the REAL bursar_db schema. Same methodology the N1
gate-integration test uses: import bursar_gate/bursar_db/bursar_live unchanged, arm
with BURSAR_ENFORCE=1, point BURSAR_DB at a throwaway temp DB, and exercise the
actual middleware functions. The model is irrelevant to these features — it only
decides WHEN a tool runs; every freshness verdict and every word the user sees is
produced by this code.

WHY this is the whole proof (not a stub): H6/H8/H9 logic lives entirely in
  - gate._tool_gate / _paths_read / _paths_from_shell / _hash_file / _stat_file   (capture)
  - gate._freshness_for_turn / _find_relocated + db.work_for_turn/paths_with_hash  (verdict)
  - live.dedup_augment_block                                                       (rendered text)
all of which we call here exactly as the armed gateway does.

Attack plan (how each feature could be silently wrong):
  H6  capture only fires for read_file → a `cat`/grep/MCP read leaves NO row, so a
      later change is invisible (a false "unchanged"). Also: a no-file command could
      garbage-capture a non-path token.
  H8  the fast stat path could (a) still pay a full hash when nothing changed (no
      speedup), or worse (b) trust the stat so hard it MISSES a same-size edit, or
      (c) crash / mis-judge a pre-H8 row that has NULL mtime/size.
  H9  a renamed file could be reported "unchanged" without verifying the bytes
      (stale!), or reported with a misleading "re-read <dead path>", or the relocate
      lookup could trust a path whose bytes silently differ now.
"""
import os, sys, json, pathlib, tempfile, hashlib

ENGINE = pathlib.Path("/Users/michaelstang/Desktop/hermes-hackathon/hermes-agent/optional-skills/finance/bursar/scripts")
PLUGINS = pathlib.Path("/Users/michaelstang/Desktop/hermes-hackathon/hermes-agent/plugins")
sys.path.insert(0, str(ENGINE)); sys.path.insert(0, str(PLUGINS))

_dbf = tempfile.NamedTemporaryFile(suffix=".db", delete=False); _dbf.close()
os.environ["BURSAR_DB"] = _dbf.name
os.environ["BURSAR_ENFORCE"] = "1"           # arm: _tool_gate only captures when armed

import bursar_db as db
import bursar_live as live
import bursar_gate as gate
db.init_db()

# realpath: on macOS mkdtemp returns /var/... but the gate canonicalizes paths via
# realpath before storing (it stores /private/var/...), so our expected paths must be
# canonical too or exact-set assertions spuriously fail. (The canonicalization is a
# feature — it dedupes symlinked paths — not the thing under test.)
WORK = pathlib.Path(os.path.realpath(tempfile.mkdtemp(prefix="bursar_h6_h8_h9_")))

fails = []
def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f"   [{detail}]" if (detail and not cond) else ""))
    if not cond:
        fails.append(name)

def write(rel, content):
    p = WORK / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    return str(p)

def sha(path):
    return hashlib.sha256(pathlib.Path(path).read_bytes()).hexdigest()

def run_tool(tool_name, args, result, *, turn_id, session_id="S"):
    """Drive the REAL tool_execution middleware exactly as the gateway does:
    next_call(args) returns the tool's result, then capture runs."""
    return gate._tool_gate(tool_name=tool_name, args=args,
                           next_call=lambda _a: result,
                           turn_id=turn_id, session_id=session_id)

def rows_for(turn_id):
    with db.connect_closing() as c:
        return db.work_for_turn(c, turn_id)

def freshness(turn_id):
    with db.connect_closing() as c:
        return gate._freshness_for_turn(c, turn_id)

def captured_paths(turn_id):
    return {r["path"] for r in rows_for(turn_id)}


# ───────────────────────── H6 — broadened read capture ─────────────────────────
print("\n=== H6: capture beyond read_file (terminal / search_files / MCP) ===")

# H6.1 terminal `cat <abs>` — the headline blind spot.
f1 = write("h6/notes.txt", "alpha beta gamma\n")
run_tool("terminal", {"command": f"cat {f1}"}, "alpha beta gamma\n", turn_id="t-cat")
check("H6.1 terminal cat captures the file", f1 in captured_paths("t-cat"))
check("H6.1 stored hash == real file hash",
      any(r["path"] == f1 and r["content_hash"] == sha(f1) for r in rows_for("t-cat")))

# H6.2 head -n 5 (separate-value flag), tail, and a piped multi-file command.
f2 = write("h6/a.py", "x=1\n"); f3 = write("h6/b.txt", "data\n")
run_tool("terminal", {"command": f"head -n 5 {f2}"}, "x=1\n", turn_id="t-head")
check("H6.2a head -n 5 skips the flag-value, captures only the path", captured_paths("t-head") == {f2})
run_tool("terminal", {"command": f"cat {f2} | grep x; head -n5 {f3}"}, "x=1\n", turn_id="t-pipe")
check("H6.2b piped 'cat a | grep x; head -n5 b' captures both read targets",
      captured_paths("t-pipe") == {f2, f3})

# H6.3 search_files result shape (matches[].path + files[]).
f4 = write("h6/hit1.md", "needle\n"); f5 = write("h6/hit2.md", "needle\n")
run_tool("search_files", {"query": "needle"},
         {"matches": [{"path": f4}], "files": [f5]}, turn_id="t-search")
check("H6.3 search_files captures matches[].path and files[]", captured_paths("t-search") == {f4, f5})

# H6.4 MCP filesystem read_resource (file:// uri).
f6 = write("h6/mcp.json", '{"k":1}\n')
run_tool("mcp_filesystem_read_resource", {"uri": f"file://{f6}"}, '{"k":1}\n', turn_id="t-mcp")
check("H6.4 mcp_*_read_resource captures the file:// path", captured_paths("t-mcp") == {f6})

# H6.5 read_file still works (no regression on the original path).
f7 = write("h6/orig.txt", "orig\n")
run_tool("read_file", {"path": f7}, "orig\n", turn_id="t-read")
check("H6.5 read_file still captured (regression guard)", captured_paths("t-read") == {f7})

# H6.6 NEGATIVE: a command that reads no file, and a non-read command, capture nothing.
run_tool("terminal", {"command": "echo hello world"}, "hello world\n", turn_id="t-echo")
run_tool("terminal", {"command": "grep needle somewhere"}, "", turn_id="t-grep")
check("H6.6 'echo ...' captures nothing (no garbage token)", rows_for("t-echo") == [])
check("H6.6 leading non-read cmd 'grep ...' captures nothing", rows_for("t-grep") == [])

# H6.7 END-TO-END money shot: cat a file, MUTATE it, freshness must say CHANGED.
fx = write("h6/watched.conf", "limit = 100\n")
run_tool("terminal", {"command": f"cat {fx}"}, "limit = 100\n", turn_id="t-e2e")
before = freshness("t-e2e")
check("H6.7a captured-via-cat file is in the freshness set (old code: empty)",
      [f for f in before if f["path"] == fx and f["unchanged"]])
write("h6/watched.conf", "limit = 500\n")  # the change a re-ask must catch
after = freshness("t-e2e")
hit = [f for f in after if f["path"] == fx]
check("H6.7b after edit, the cat'd file reads CHANGED",
      hit and hit[0]["unchanged"] is False and not hit[0].get("missing"))
# and the user-visible block actually says CHANGED + tells the agent to re-read it
block = live.dedup_augment_block(prior_answer="P"*40, sim=0.9,
                                 turn={"calls": 9, "tokens": 8000, "cost": 0.05},
                                 orig_session="o", same_session=True, freshness=after)
check("H6.7c rendered block says the file CHANGED", "CHANGED since the prior answer" in block)

# H6.8 NEGATIVE: an UNCHANGED cat'd file must not read as CHANGED.
fu = write("h6/stable.txt", "constant\n")
run_tool("terminal", {"command": f"cat {fu}"}, "constant\n", turn_id="t-stable")
fu_state = [f for f in freshness("t-stable") if f["path"] == fu]
check("H6.8 untouched cat'd file stays UNCHANGED (no false positive)",
      fu_state and fu_state[0]["unchanged"] is True)


# ───────────────────────── H8 — fast stat-based freshness ──────────────────────
print("\n=== H8: stat (mtime+size) short-circuit + same-size change still caught ===")

# Count real sha256 reads during a freshness check by wrapping the shipping _hash_file.
_real_hash = gate._hash_file
class HashCounter:
    def __init__(self): self.n = 0
    def __enter__(self):
        self.n = 0
        def counting(path):
            self.n += 1
            return _real_hash(path)
        gate._hash_file = counting
        return self
    def __exit__(self, *a):
        gate._hash_file = _real_hash

# H8.1 untouched file: mtime+size match → unchanged with ZERO full hashes.
g1 = write("h8/big.txt", "L" * 50_000)
run_tool("read_file", {"path": g1}, "(read)", turn_id="t-h8-fast")
with HashCounter() as hc:
    st = [f for f in freshness("t-h8-fast") if f["path"] == g1]
check("H8.1a untouched file reads UNCHANGED", st and st[0]["unchanged"] is True)
check("H8.1b ZERO full sha256 reads on the unchanged path (stat short-circuit)",
      hc.n == 0, f"hash calls={hc.n}")

# H8.2 same-SIZE content edit with mtime advanced (a normal write moves mtime):
#      stat differs → hash fallback → CHANGED detected.
orig_rows = rows_for("t-h8-fast")
stored_mtime = orig_rows[0]["mtime"]
new_same_size = "X" * 50_000                       # identical length, different bytes
pathlib.Path(g1).write_text(new_same_size)
os.utime(g1, (stored_mtime + 10, stored_mtime + 10))  # deterministic mtime advance
with HashCounter() as hc:
    st = [f for f in freshness("t-h8-fast") if f["path"] == g1]
check("H8.2a same-size edit (mtime moved) reads CHANGED via hash fallback",
      st and st[0]["unchanged"] is False)
check("H8.2b the fallback actually paid a hash (>=1) on the stat-mismatch path",
      hc.n >= 1, f"hash calls={hc.n}")

# H8.3 any SIZE change is caught even if mtime is (pathologically) held equal.
g2 = write("h8/sz.txt", "12345")
run_tool("read_file", {"path": g2}, "(read)", turn_id="t-h8-size")
sm = rows_for("t-h8-size")[0]["mtime"]
pathlib.Path(g2).write_text("123456789")           # different size
os.utime(g2, (sm, sm))                              # force mtime equal → only size differs
st = [f for f in freshness("t-h8-size") if f["path"] == g2]
check("H8.3 a size change is caught even with mtime forced equal", st and st[0]["unchanged"] is False)

# H8.4 back-compat: a pre-H8 row (NULL mtime/size) must still judge via full hash.
g3 = write("h8/legacy.txt", "legacy-body\n")
with db.connect_closing() as c:
    db.record_work(c, turn_id="t-h8-legacy", session_id="S", tool_name="read_file",
                   path=g3, content_hash=sha(g3), mtime=None, size=None)
st = [f for f in freshness("t-h8-legacy") if f["path"] == g3]
check("H8.4a pre-H8 row (NULL mtime/size), unchanged → UNCHANGED via full hash",
      st and st[0]["unchanged"] is True)
pathlib.Path(g3).write_text("legacy-body-EDITED\n")
st = [f for f in freshness("t-h8-legacy") if f["path"] == g3]
check("H8.4b pre-H8 row, after edit → CHANGED via full hash", st and st[0]["unchanged"] is False)


# ───────────────────────── H9 — rename / relocate handling ─────────────────────
print("\n=== H9: rename → relocate-recognized or GONE/re-discover, never stale ===")

# H9.1 relocate recognized: same bytes recorded at P and Q; delete P; Q still holds
#      the content → freshness reports UNCHANGED but MOVED to Q (verified, not assumed).
c_reloc = "relocatable-unique-content-AAA\n"
p_old = write("h9/reloc/old.txt", c_reloc)
p_new = write("h9/reloc/new.txt", c_reloc)
run_tool("read_file", {"path": p_old}, "(read)", turn_id="t-h9-reloc")
run_tool("read_file", {"path": p_new}, "(read)", turn_id="t-h9-other")  # Q seen before
os.remove(p_old)
st = [f for f in freshness("t-h9-reloc") if os.path.basename(f["path"]) == "old.txt"]
check("H9.1a gone path with same bytes elsewhere → UNCHANGED (relocated)",
      st and st[0]["unchanged"] is True and st[0].get("relocated_to") == os.path.abspath(p_new))
block = live.dedup_augment_block(prior_answer="P"*40, sim=0.9, turn=None,
                                 orig_session="o", same_session=True, freshness=st)
check("H9.1b rendered block says UNCHANGED but MOVED", "UNCHANGED but MOVED to" in block)

# H9.2 pure rename to an UNSEEN path → GONE/missing (NEVER falsely unchanged) and the
#      instruction is re-discover, NOT 're-read <dead path>'.
c_gone = "pure-rename-unique-content-BBB\n"
p2 = write("h9/gone/feature_list.json", c_gone)
run_tool("read_file", {"path": p2}, "(read)", turn_id="t-h9-gone")
os.rename(p2, str(WORK / "h9/gone/renamed_elsewhere.json"))  # new name never recorded
st = [f for f in freshness("t-h9-gone") if os.path.basename(f["path"]) == "feature_list.json"]
check("H9.2a pure rename to unseen path → missing=True, unchanged=False (safe)",
      st and st[0].get("missing") is True and st[0]["unchanged"] is False)
block = live.dedup_augment_block(prior_answer="P"*40, sim=0.9, turn=None,
                                 orig_session="o", same_session=True, freshness=st)
check("H9.2b block says GONE", "GONE (its path no longer exists)" in block)
check("H9.2c block instructs RE-DISCOVER, not re-read the dead path",
      "re-discover it" in block and "do NOT re-read the old path" in block)
check("H9.2d block never tells the agent to re-read the dead absolute path",
      f"re-read {p2}" not in block and "re-read ONLY those" not in block)

# H9.3 verify-don't-assume: candidate path exists but its bytes now DIFFER → must NOT
#      be trusted as a relocation → reported missing (never stale).
c_v = "verify-unique-content-CCC\n"
pv_old = write("h9/verify/old.txt", c_v)
pv_new = write("h9/verify/copy.txt", c_v)
run_tool("read_file", {"path": pv_old}, "(read)", turn_id="t-h9-verify")
run_tool("read_file", {"path": pv_new}, "(read)", turn_id="t-h9-verify2")
os.remove(pv_old)
pathlib.Path(pv_new).write_text("verify-content-CHANGED-now\n")  # candidate bytes differ
st = [f for f in freshness("t-h9-verify") if os.path.basename(f["path"]) == "old.txt"]
check("H9.3 relocation candidate with changed bytes is rejected → missing (not stale)",
      st and st[0].get("missing") is True and not st[0].get("relocated_to"))

# H9.4 combined render: unchanged + changed + missing all surface distinctly.
mixed = [
    {"name": "stable.py", "path": "/x/stable.py", "unchanged": True, "missing": False},
    {"name": "edited.py", "path": "/x/edited.py", "unchanged": False, "missing": False},
    {"name": "gone.py", "path": "/x/gone.py", "unchanged": False, "missing": True},
]
block = live.dedup_augment_block(prior_answer="P"*40, sim=0.9, turn=None,
                                 orig_session="o", same_session=True, freshness=mixed)
check("H9.4 combined block lists UNCHANGED, CHANGED and GONE distinctly",
      "stable.py — UNCHANGED" in block and "edited.py — CHANGED" in block and "gone.py — GONE" in block)


# ───────────────────────── summary ─────────────────────────
print("\n" + ("ALL GREEN" if not fails else f"{len(fails)} FAILED: " + ", ".join(fails)))
sys.exit(1 if fails else 0)
