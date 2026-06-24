"""Adversarial test wave for N1 (tenant isolation) + N2 (_TURN_Q TTL eviction).

Attack plan
-----------
N1 — tenant leakage:
  A1. Cross-tenant SERVE leak: tenant B asks tenant A's exact question. Default
      gate scoping (find_dup(tenant=B)) MUST return None — B can never be served
      or shown A's answer text.
  A2. Same-tenant reuse preserved: A re-asks across a *different session* but the
      SAME tenant -> find_dup(tenant=A) MUST still match (the flagship single-user
      "you asked this in another conversation" reuse must survive the fix).
  A3. Cross-tenant AUGMENT is opt-in only: find_dup(tenant=None) (the cross flag
      ON) DOES return A's row for B, but query_tenant proves it's cross-tenant, so
      the gate's same_tenant gate would force AUGMENT, never SERVE.
  A4. Legacy/default rows: a row written with BURSAR_TENANT unset lands in 'local';
      a default gate (also 'local') still reuses it -> single-user unaffected.

N2 — _TURN_Q eviction:
  B1. Active long loop survives a flood that blows the cap: a loop that touches its
      turn_id every round must NEVER be mis-seen as first_call again (the exact
      "cut off an agent loop" failure the old count-256 cap could cause).
  B2. The pinned question survives mid-turn prompt drift (turn-opening guard).
  B3. Idle reclaim: a turn silent past the TTL is reclaimed (acceptable: it's
      abandoned); a turn still within the TTL is NOT.
  B4. Backstop boundary: a pure flood of *untouched* turns past the cap DOES evict
      the oldest (documents that the cap is still a real ceiling).
"""
import os, sys, tempfile, time, pathlib

REPO = pathlib.Path("/Users/michaelstang/Desktop/hermes-hackathon/hermes-agent")
ENGINE = REPO / "optional-skills" / "finance" / "bursar" / "scripts"
PLUGINS = REPO / "plugins"
sys.path.insert(0, str(ENGINE))
sys.path.insert(0, str(PLUGINS))

# Isolated DB BEFORE importing the engine (connect() reads BURSAR_DB).
_tmpdb = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmpdb.close()
os.environ["BURSAR_DB"] = _tmpdb.name

import bursar_db as db
import bursar_live as live

db.init_db()

ANSWER = "The capital of France is Paris, a real servable cached answer over 16 chars."
Q = "What is the capital of France and why does it matter for logistics planning?"

failures = []
def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{detail}]" if detail and not cond else ""))
    if not cond:
        failures.append(name)

def seed(tenant, session, prompt=Q, answer=ANSWER):
    os.environ["BURSAR_TENANT"] = tenant
    with db.connect_closing() as conn:
        qid = live.record_serviced(
            conn, team=f"live:{session}", prompt=prompt, model="x/model",
            tokens=500, token_cost=0.01, value=60, tier="standard",
            rationale="t", result=answer, session_id=session,
        )
    return qid

# ---------------------------------------------------------------- N1
print("\n=== N1 — tenant isolation ===")
qa = seed("acme", "sessA")          # tenant acme writes the answer

# A1: tenant globex asks the SAME question -> default scoping must NOT match acme.
with db.connect_closing() as conn:
    dup_cross_blocked = live.find_dup(conn, Q, tenant="globex")
check("A1 cross-tenant SERVE blocked (find_dup tenant=globex -> None)",
      dup_cross_blocked is None, detail=str(dup_cross_blocked))

# A2: same tenant, DIFFERENT session -> reuse must still work.
with db.connect_closing() as conn:
    dup_same = live.find_dup(conn, Q, tenant="acme")
check("A2 same-tenant cross-session reuse preserved",
      dup_same is not None and live.is_servable_answer(dup_same[2]),
      detail=str(dup_same))

# A3: cross flag ON (tenant=None) matches across tenants, but it's provably cross.
with db.connect_closing() as conn:
    dup_crossmode = live.find_dup(conn, Q, tenant=None)
    cross_is_crosstenant = (dup_crossmode is not None
                            and live.query_tenant(conn, dup_crossmode[0]) == "acme")
check("A3 cross-tenant AUGMENT match found only with flag (tenant=None)",
      dup_crossmode is not None)
check("A3 query_tenant proves the match is cross-tenant (acme != globex)",
      cross_is_crosstenant, detail=str(dup_crossmode))

# A4: default/legacy 'local' tenant reuse.
del os.environ["BURSAR_TENANT"]           # unset -> tenant_id() == 'local'
check("A4 tenant_id() defaults to 'local' when unset", live.tenant_id() == "local")
seed("local", "sessL", prompt="Explain the CAP theorem in distributed systems clearly.",
     answer="CAP theorem: consistency, availability, partition-tolerance; pick two under partition.")
with db.connect_closing() as conn:
    dup_local = live.find_dup(conn, "Explain the CAP theorem in distributed systems clearly.",
                              tenant=live.tenant_id())
check("A4 default-tenant ('local') reuse works (single-user unaffected)",
      dup_local is not None and live.is_servable_answer(dup_local[2]))

# A5 (boundary): the matched row's tenant column is actually stored, not NULL.
with db.connect_closing() as conn:
    t = live.query_tenant(conn, qa)
check("A5 tenant column stored on the row (== 'acme')", t == "acme", detail=str(t))

# ---------------------------------------------------------------- N2
print("\n=== N2 — _TURN_Q last-access TTL eviction ===")
import bursar_gate as gate

def reset_turnq(ttl=3600.0, cap=20000):
    gate._TURN_Q.clear()
    gate._TURN_Q_TTL = ttl
    gate._TURN_Q_MAX = cap

# B1 + B2: active long loop survives a flood that far exceeds the cap.
reset_turnq(ttl=3600.0, cap=100)        # tiny cap so the flood is cheap
q0, first0 = gate._turn_question("loopT", "the real user question")
check("B1 first call of the loop is first_call=True", first0 is True)
loop_ok = True
pin_ok = True
ROUNDS, OTHERS_PER_ROUND = 50, 30       # 1500 other turns >> cap 100
for r in range(ROUNDS):
    for j in range(OTHERS_PER_ROUND):
        gate._turn_question(f"other_{r}_{j}", "noise prompt")
    # the loop's own internal call: drifted prompt, same turn_id
    q, first = gate._turn_question("loopT", f"[System: tool result chunk {r}]")
    if first:        # mis-seen as opening MID-LOOP -> would derail the loop
        loop_ok = False
    if q != "the real user question":
        pin_ok = False
check("B1 active loop NEVER mis-seen as first across 1500-turn flood (>15x cap)", loop_ok)
check("B2 pinned question survives mid-turn prompt drift", pin_ok)

# B3: idle reclaim vs fresh retention.
reset_turnq(ttl=0.05, cap=20000)
gate._turn_question("idle", "q-idle")
gate._turn_question("fresh", "q-fresh")
_, fresh_again = gate._turn_question("fresh", "drift")     # immediate -> within TTL
check("B3 turn still within TTL is NOT reclaimed (first stays False)", fresh_again is False)
time.sleep(0.12)                                            # exceed the 0.05 TTL
_, idle_again = gate._turn_question("idle", "q-idle-2")     # silent past TTL -> reclaimed
check("B3 idle turn past TTL IS reclaimed (seen as first again)", idle_again is True)

# B5: THE discriminating case — a SILENT loop (waiting on a slow tool) survives a
# flood that exceeds the OLD count-256 cap but stays under the new backstop and
# within the TTL. Under the old policy loopT is untouched, drifts to the front,
# and the next "full" insert drops it -> mid-loop misfire. Under the new policy
# eviction is by last-access AGE, so 1500 fresh others (<<20000 cap) evict nobody.
reset_turnq(ttl=3600.0, cap=20000)          # real defaults
gate._turn_question("silentLoop", "the real question")     # one call, then "tool wait"
for i in range(1500):                       # 1500 >> old cap 256, << new cap 20000
    gate._turn_question(f"concurrent_{i}", "other session traffic")
_, silent_again = gate._turn_question("silentLoop", "[System: tool finally returned]")
check("B5 SILENT loop survives a 1500-turn flood (old cap-256 would have evicted it)",
      silent_again is False)

# B4: backstop boundary — pure flood of UNTOUCHED turns past cap evicts oldest.
reset_turnq(ttl=3600.0, cap=100)
gate._turn_question("victim", "q-victim")          # inserted, never touched again
for i in range(300):                               # 3x cap, none touch 'victim'
    gate._turn_question(f"flood_{i}", "x")
_, victim_again = gate._turn_question("victim", "q-victim-2")
check("B4 backstop cap still evicts an untouched oldest turn under pure flood",
      victim_again is True)

# ---------------------------------------------------------------- summary
print("\n" + ("ALL PASS" if not failures else f"FAILURES: {failures}"))
sys.exit(1 if failures else 0)
