"""Integration test through the REAL gate _gate() enforcement path (armed), proving
the N1 security property end-to-end — not just at the find_dup layer.

Attack: a prior answer exists for tenant 'acme'. Tenant 'globex' asks the SAME
question through the armed gate.
  G1. Same tenant (acme) -> REUSE happens (served or augmented).
  G2. Cross tenant (globex), default flag OFF -> the gate runs the REAL call
      (next_call invoked) and does NOT inject acme's answer -> no leak.
  G3. Cross tenant (globex), BURSAR_CROSS_TENANT_AUGMENT=1 -> the gate AUGMENTS
      (real call runs AND prior injected as context) but NEVER serves across.
  G4. Fail-open: the gate never raises into the caller.
"""
import os, sys, tempfile, pathlib
ENGINE = pathlib.Path("/Users/michaelstang/Desktop/hermes-hackathon/hermes-agent/optional-skills/finance/bursar/scripts")
PLUGINS = pathlib.Path("/Users/michaelstang/Desktop/hermes-hackathon/hermes-agent/plugins")
sys.path.insert(0, str(ENGINE)); sys.path.insert(0, str(PLUGINS))

tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False); tmp.close()
os.environ["BURSAR_DB"] = tmp.name
os.environ["BURSAR_ENFORCE"] = "1"          # arm enforcement (env wins over DB flag)
os.environ.pop("BURSAR_TEAM", None)
os.environ.pop("BURSAR_CROSS_TENANT_AUGMENT", None)

import bursar_db as db
import bursar_live as live
import bursar_gate as gate
db.init_db()

Q = "What is the capital of France and its significance for European logistics?"
ANSWER = ("Paris is the capital of France; a real cached answer well over sixteen "
          "characters, suitable for dedup-serve in this integration test.")

fails = []
def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{detail}]" if (detail and not cond) else ""))
    if not cond: fails.append(name)

# Seed a prior serviced answer owned by tenant 'acme'.
os.environ["BURSAR_TENANT"] = "acme"
with db.connect_closing() as c:
    live.record_serviced(c, team="live:acmeS1", prompt=Q, model="x/model",
                         tokens=400, token_cost=0.01, value=55, tier="standard",
                         rationale="seed", result=ANSWER, session_id="acmeS1")

def drive(tenant, *, session_id, cross=False):
    """Run one opening call through the armed gate; report (next_call_called,
    injected_system, returned)."""
    os.environ["BURSAR_TENANT"] = tenant
    if cross: os.environ["BURSAR_CROSS_TENANT_AUGMENT"] = "1"
    else: os.environ.pop("BURSAR_CROSS_TENANT_AUGMENT", None)
    request = {"model": "x/model", "messages": [{"role": "user", "content": Q}]}
    state = {"called": False}
    def next_call(req):
        state["called"] = True
        return {"id": "real_call_response"}
    returned = gate._gate(request=request, next_call=next_call,
                          session_id=session_id, api_request_id=f"r_{tenant}_{session_id}",
                          provider="nous", model="x/model", api_mode="",
                          turn_id=f"turn_{tenant}_{session_id}")
    injected = any(isinstance(m, dict) and m.get("role") == "system"
                   and "repeat-question" in str(m.get("content", "")).lower()
                   or (isinstance(m, dict) and m.get("role") == "system"
                       and "bursar" in str(m.get("content", "")).lower())
                   for m in request["messages"])
    leaked = any(ANSWER[:40] in str(m.get("content", "")) for m in request["messages"])
    return state["called"], injected, leaked, returned

# G1 — same tenant: reuse must happen (served => next_call NOT called, OR augmented).
called, injected, leaked, ret = drive("acme", session_id="acmeS2")
served = (not called)
check("G1 same-tenant (acme) REUSES the prior (served or augmented)",
      served or injected, detail=f"called={called} injected={injected}")

# G2 — cross tenant, default flag OFF: real call runs, acme's answer NOT injected.
called, injected, leaked, ret = drive("globex", session_id="globexS1", cross=False)
check("G2 cross-tenant default: REAL call runs (next_call invoked)", called is True)
check("G2 cross-tenant default: prior NOT served (no synth, real response returned)",
      ret == {"id": "real_call_response"})
check("G2 cross-tenant default: acme's answer NOT injected into globex request",
      leaked is False, detail=f"leaked={leaked} injected={injected}")

# G3 — cross tenant, flag ON: augment (real call runs AND prior injected), never serve.
called, injected, leaked, ret = drive("globex", session_id="globexS2", cross=True)
check("G3 cross-tenant+flag: still a REAL call (augment, not serve)", called is True)
check("G3 cross-tenant+flag: prior IS injected as augment context", injected is True)

# G4 — fail-open: drive a malformed request, must not raise, must call next_call.
os.environ["BURSAR_TENANT"] = "acme"
state = {"called": False}
def nc2(req): state["called"] = True; return {"id": "fail_open_ok"}
try:
    gate._gate(request={"messages": None}, next_call=nc2, session_id="x",
               api_request_id="rx", provider="nous", model="m", api_mode="", turn_id="tx")
    raised = False
except Exception as e:
    raised = True; print("  raised:", e)
check("G4 fail-open: gate never raises into the caller", raised is False)
check("G4 fail-open: malformed request still ran the real call", state["called"] is True)

print("\n" + ("ALL PASS" if not fails else f"FAILURES: {fails}"))
sys.exit(1 if fails else 0)
