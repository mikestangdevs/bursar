"""Adversarial test wave for N3 (turn_id fallback hardening), N4 (deixis stateful),
N5 (prior-answer cap), N6 (retention purge).

Attack plan
-----------
N3: an injected "[System: ...]" / "[Tool error: ...]" pseudo-user message as the
    LAST message must NOT be seen as a turn opening (else dedup could fire mid-loop
    on the fallback path); a real user prompt still IS opening; a prompt that merely
    MENTIONS "[system]" mid-text is still opening.
N4: every conversational-continuation phrase classifies STATEFUL (never served); a
    normal timeless question does not.
N5: a long prior answer is truncated to the cap with head+tail preserved and an
    omitted-chars marker; a short prior is untouched; empty -> "(none captured)";
    the augment block actually injects the capped form.
N6: purge_expired deletes live rows (and their ledger+events) older than the window,
    keeps fresh ones, leaves demo rows alone, and is a no-op when disabled.
"""
import os, sys, tempfile, pathlib
ENGINE = pathlib.Path("/Users/michaelstang/Desktop/hermes-hackathon/hermes-agent/optional-skills/finance/bursar/scripts")
PLUGINS = pathlib.Path("/Users/michaelstang/Desktop/hermes-hackathon/hermes-agent/plugins")
sys.path.insert(0, str(ENGINE)); sys.path.insert(0, str(PLUGINS))

tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False); tmp.close()
os.environ["BURSAR_DB"] = tmp.name

import bursar_db as db
import bursar_live as live
db.init_db()

fails = []
def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  [{detail}]" if (detail and not cond) else ""))
    if not cond: fails.append(name)

def req(last_text, role="user", as_list=False):
    content = [{"type": "text", "text": last_text}] if as_list else last_text
    return {"messages": [{"role": "user", "content": "earlier turn"},
                         {"role": "assistant", "content": "ok"},
                         {"role": role, "content": content}]}

# ---------------------------------------------------------------- N3
print("\n=== N3 — turn_id-absent fallback hardening ===")
check("N3 injected '[System: ...]' last msg is NOT a turn opening",
      live.is_turn_opening_request(req("[System: Your previous tool call timed out]")) is False)
check("N3 '[Tool error: ...]' is NOT a turn opening",
      live.is_turn_opening_request(req("[Tool error: connection refused]")) is False)
check("N3 '(system) ...' paren form is NOT a turn opening",
      live.is_turn_opening_request(req("(system) retry the request")) is False)
check("N3 injected pseudo-user in LIST content form is caught",
      live.is_turn_opening_request(req("[System: resuming]", as_list=True)) is False)
check("N3 a genuine user prompt IS a turn opening",
      live.is_turn_opening_request(req("What is the capital of France?")) is True)
check("N3 a prompt that merely MENTIONS [system] mid-text is still opening",
      live.is_turn_opening_request(req("Explain the [system] design doc to me")) is True)

# ---------------------------------------------------------------- N4
print("\n=== N4 — conversational deixis -> stateful ===")
DEIXIS = ["continue", "keep going", "do the same for the other module",
          "what did we decide about caching", "as we discussed earlier",
          "finish that refactor", "use the previous answer", "from before, redo it",
          "go on", "as above"]
for ph in DEIXIS:
    cls = live._temporal_heuristic(ph)
    check(f"N4 deixis '{ph[:32]}' -> stateful", cls == "stateful", detail=cls)
# negatives: standalone timeless questions must NOT be flagged stateful
for ph in ["how does vector RAG work", "explain the CAP theorem",
           "what is the time complexity of quicksort"]:
    check(f"N4 timeless '{ph[:28]}' NOT stateful",
          live.is_stateful_intent(ph) is False and live._temporal_heuristic(ph) != "stateful")

# ---------------------------------------------------------------- N5
print("\n=== N5 — prior-answer cap ===")
cap = live._MAX_PRIOR_CHARS
head_marker = "HEADSTART_" + "x"*20
tail_marker = "y"*20 + "_TAILEND"
long_prior = head_marker + ("M"*(cap*3)) + tail_marker   # ~3x the cap
capped = live._cap_prior(long_prior)
check("N5 long prior is shortened below ~cap+marker",
      len(capped) <= cap + 120, detail=f"len={len(capped)} cap={cap}")
check("N5 head preserved", capped.startswith(head_marker))
check("N5 tail preserved", capped.rstrip().endswith(tail_marker))
check("N5 omitted-chars marker present", "omitted to fit the context budget" in capped)
short = "a concise prior answer well under the cap, over sixteen chars."
check("N5 short prior passes through unchanged", live._cap_prior(short) == short)
check("N5 empty prior -> placeholder", live._cap_prior("") == "(none captured)")
# integration: the augment block injects the capped form, not the raw 3x-cap text
block = live.dedup_augment_block(prior_answer=long_prior, sim=0.95, turn=None,
                                 orig_session=None, same_session=False)
check("N5 augment block injects the CAPPED prior (block not bloated by full text)",
      ("omitted to fit the context budget" in block) and (len(block) < len(long_prior)))

# ---------------------------------------------------------------- N6
print("\n=== N6 — retention purge ===")
DAY = 86400
t_now = db.now()
def insert_row(qid, age_days, source="live"):
    created = t_now - age_days*DAY
    with db.write_txn(c):
        c.execute("INSERT INTO queries (id, team, tenant, source, prompt, status, created_at, decided_at) "
                  "VALUES (?,?,?,?,?,?,?,?)", (qid, "live:t", "local", source, "p"+qid, "serviced", created, created))
        c.execute("INSERT INTO ledger (query_id, team, source, model, tokens, token_cost, fee, total, settled, created_at) "
                  "VALUES (?,?,?,?,?,?,?,?,?,?)", (qid, "live:t", source, "m", 1, 0.0, 0.0, 0.0, 0, created))
        c.execute("INSERT INTO events (query_id, kind, payload, created_at) VALUES (?,?,?,?)",
                  (qid, "serviced", "{}", created))

with db.connect_closing() as c:
    insert_row("old_live", 100, "live")     # expired
    insert_row("new_live", 1, "live")       # fresh
    insert_row("old_demo", 100, "demo")     # expired but demo -> keep when source='live'
    res = db.purge_expired(c, days=30, source="live")
    qids = {r["id"] for r in c.execute("SELECT id FROM queries")}
    led_old = c.execute("SELECT COUNT(*) n FROM ledger WHERE query_id='old_live'").fetchone()["n"]
    ev_old = c.execute("SELECT COUNT(*) n FROM events WHERE query_id='old_live'").fetchone()["n"]

check("N6 expired live query deleted", "old_live" not in qids, detail=str(qids))
check("N6 fresh live query kept", "new_live" in qids)
check("N6 old DEMO row untouched (source-scoped to live)", "old_demo" in qids)
check("N6 expired row's ledger cascade-deleted", led_old == 0)
check("N6 expired row's events cascade-deleted", ev_old == 0)
check("N6 purge_expired reports the deleted count", res.get("queries") == 1, detail=str(res))
# disabled-by-default: days=None with no env -> no-op
os.environ.pop("BURSAR_RETENTION_DAYS", None)
with db.connect_closing() as c:
    res0 = db.purge_expired(c, days=None)
check("N6 disabled by default (RETENTION_DAYS unset -> no-op)",
      res0 == {"queries": 0, "ledger": 0, "events": 0}, detail=str(res0))
check("N6 retention_days() defaults to 0", db.retention_days() == 0)

# ---------------------------------------------------------------- sharpening
print("\n=== Sharpening — false-positive / boundary probes ===")
# N3: a real question that just STARTS with a bracket but isn't injected machinery
check("N3 '[urgent] ...' (non-keyword bracket) is still a turn opening",
      live.is_turn_opening_request(req("[urgent] please explain the retry logic")) is True)
# N4: word-boundary false positives that must NOT trip the deixis regex
for ph, why in [("what's going on with quantum computing", "going on != go on"),
                ("explain continued fractions in number theory", "continued != continue"),
                ("how do paragraphs proceed in typography", "proceed mid-sentence is borderline")]:
    # 'proceed' WILL match by design (continuation verb); only assert the first two.
    pass
check("N4 'going on' does NOT trip deixis (boundary)",
      live.is_stateful_intent("what's going on with quantum computing") is False)
check("N4 'continued fractions' does NOT trip deixis (boundary)",
      live.is_stateful_intent("explain continued fractions in number theory") is False)
# Known/accepted false positive (safe direction = missed reuse, never stale serve):
print("NOTE accepted FP: 'is X the same for both?' -> stateful =",
      live.is_stateful_intent("is the rule the same for both cases"),
      "(safe: only costs a missed reuse)")
# N5: cap disabled (<=0) returns full text
_orig = live._MAX_PRIOR_CHARS
live._MAX_PRIOR_CHARS = 0
check("N5 cap<=0 disables truncation (full text returned)",
      live._cap_prior(long_prior) == long_prior)
live._MAX_PRIOR_CHARS = _orig
# N6: source=None purges demo too; and purge is idempotent (2nd run deletes 0)
with db.connect_closing() as c:
    insert_row2 = None
    # reuse helper bound to a fresh conn
    def insert_row_c(qid, age_days, source):
        created = t_now - age_days*DAY
        with db.write_txn(c):
            c.execute("INSERT INTO queries (id, team, tenant, source, prompt, status, created_at, decided_at) "
                      "VALUES (?,?,?,?,?,?,?,?)", (qid,"live:t","local",source,"p","serviced",created,created))
    insert_row_c("old_demo2", 100, "demo")
    r_all = db.purge_expired(c, days=30, source=None)   # purge ALL sources
    r_again = db.purge_expired(c, days=30, source=None) # idempotent
    demo_gone = c.execute("SELECT COUNT(*) n FROM queries WHERE id='old_demo2'").fetchone()["n"] == 0
check("N6 source=None purges demo rows too", demo_gone and r_all["queries"] >= 1, detail=str(r_all))
check("N6 purge is idempotent (2nd run deletes 0)",
      r_again == {"queries": 0, "ledger": 0, "events": 0}, detail=str(r_again))

print("\n" + ("ALL PASS" if not fails else f"FAILURES: {fails}"))
sys.exit(1 if fails else 0)
