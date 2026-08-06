#!/usr/bin/env python3
"""Contact plot + full-game memory: persistence, aging, TTL expiry, live-only
fallback, parley transcript accumulation, and the prompt history builder."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "sim"))
from core import Engine, WINDOW          # noqa: E402
from llm import LLMAdmiral               # noqa: E402


class Idle:
    name = "idle"

    def decide(self, summary, rng):
        return {}


fails = 0


def ok(cond, msg):
    global fails
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        fails += 1


# --- contact mechanism: sighted -> persists at last-known -> ages -> expires ---
eng = Engine([("A", Idle()), ("B", Idle())], seed=7,
             scenario={"contact_ttl": 300})
f0 = eng.fleets[0]
enemy = next(s for s in eng.ships.values() if s.fleet == 1)
mine = next(s for s in eng.ships.values() if s.fleet == 0)

enemy.x, enemy.y = mine.x + 2, mine.y            # inside vision (>= 6)
eng._update_contacts()
ok(enemy.id in f0.contacts, "sighted enemy lands on the plot")
seen = [e for e in eng.summary_for(f0)["enemies"] if e["age_s"] == 0]
ok(any(e["x"] == enemy.x for e in seen), "summary reports live sighting age_s=0")

last_x = enemy.x
enemy.x, enemy.y = eng.W - 2, eng.H - 2          # teleport far out of sight
eng.t += 100
eng._update_contacts()
stale = [e for e in eng.summary_for(f0)["enemies"] if e["age_s"] > 0]
ok(any(e["x"] == last_x for e in stale),
   "out-of-sight contact persists at LAST-KNOWN position")
ok(all(e["x"] != enemy.x for e in eng.summary_for(f0)["enemies"]),
   "plot never leaks the enemy's true new position")

eng.t += 301                                     # past contact_ttl
eng._update_contacts()
ok(enemy.id not in f0.contacts, "contact expires after contact_ttl")

# --- ttl=0 restores live-only reporting ---
eng0 = Engine([("A", Idle()), ("B", Idle())], seed=7, scenario={"contact_ttl": 0})
g0 = eng0.fleets[0]
e0 = next(s for s in eng0.ships.values() if s.fleet == 1)
m0 = next(s for s in eng0.ships.values() if s.fleet == 0)
e0.x, e0.y = m0.x + 2, m0.y
eng0._update_contacts()
live = eng0.summary_for(g0)["enemies"]
ok(any(en["x"] == e0.x and en["age_s"] == 0 for en in live),
   "ttl=0: live sighting still reported")
e0.x, e0.y = eng0.W - 2, eng0.H - 2
ok(not any(en["age_s"] > 0 for en in eng0.summary_for(g0)["enemies"]),
   "ttl=0: nothing stale is ever reported")

# --- parley transcript accumulates both directions ---
class Chatty:
    name = "chatty"

    def decide(self, summary, rng):
        return dict(parley=[dict(to="all", text="the deal: split the center wrecks")])


engp = Engine([("Chatty", Chatty()), ("Quiet", Idle())], seed=3)
for _ in range(WINDOW * 3):
    engp.tick()
sender, receiver = engp.fleets[0], engp.fleets[1]
ok(any("to" in m for m in sender.parley_log), "sender logs its outbound messages")
ok(any(m.get("frm") == "Chatty" for m in receiver.parley_log),
   "receiver logs inbound with sender name")
ok("parley_log" in engp.summary_for(receiver), "summary carries the transcript")

# --- prompt history builder: caps, ordering, drop-oldest marker ---
adm = LLMAdmiral("test-model", history_chars=400)
adm._last_thoughts = [(w, f"thought number {w} " + "x" * 40) for w in range(30)]
plog = [dict(w=w, frm="Rival", text=f"msg {w} " + "y" * 40) for w in range(30)]
h = adm._history(plog)
ok(len(h) < 700, f"history respects the char budget (got {len(h)})")
ok("(…older entries dropped…)" in h, "drop-oldest marker present when over budget")
ok("thought number 29" in h and "msg 29" in h, "newest entries survive the cap")
ok("thought number 0" not in h, "oldest entries are the ones dropped")
ok(adm._history([]) == "" or "JOURNAL" in adm._history([]),
   "empty parley log does not break the builder")
ok(LLMAdmiral("m", history_chars=0)._history(plog) == "", "history_chars=0 disables")

# --- custom prompt hook + memo cap plumbing ---
a2 = LLMAdmiral("m", prompt="Always favor trade over war.", memo_chars=500)
ok("OPERATOR DIRECTIVE" in a2.system and "favor trade" in a2.system,
   "custom prompt lands in the system message")
ok(LLMAdmiral("m").system.find("OPERATOR") < 0, "no directive block without a prompt")

print("FAILURES:", fails)
sys.exit(1 if fails else 0)
