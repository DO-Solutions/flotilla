#!/usr/bin/env python3
"""Official treaties v2: negotiated inside parley, two typed pacts.

The redesign (2026-08-17, after operator review of v1):
  * treaty verbs ride INSIDE parley messages — one diplomacy channel, and
    the transcript carries the proposal and the signature
  * NON-AGGRESSION breaks only when one side SINKS the other's ship. Ships
    fire automatically on adjacency, so v1's damage trigger made every pact
    a time bomb; a sinking is deliberate enough to mean betrayal
  * BORDER: an agreed x/y line neither side crosses — broken only when a
    signer SEES the other's ship beyond it (fog applies; an unseen crossing
    is deniable, which is what makes scouts matter)

What can rot: the parley-carried handshake and its transcript markers, the
two break rules (including what must NOT break them), the flagship-side
guard on borders, publicity vs private offers, expiry, the off switch, and
freeze/thaw with typed records.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "sim"))
from core import Engine

FAILS = []


def ok(cond, msg):
    if cond:
        print(f"PASS {msg}")
    else:
        FAILS.append(msg)
        print(f"FAIL {msg}")


class Idle:
    name = "idle"

    def decide(self, summary, rng):
        return {}


class Scripted:
    name = "scripted"

    def __init__(self, queue):
        self.queue = list(queue)
        self.states = []

    def decide(self, summary, rng):
        self.states.append(summary)
        return self.queue.pop(0) if self.queue else {}


SCN = {"width": 48, "height": 32, "max_ticks": 900, "warmup": False,
       "start_cargo": 100, "window": 100}


def mk(a_queue, b_queue, scn=None, ticks=500, third=False):
    players = [("A", Scripted(a_queue)), ("B", Scripted(b_queue))]
    if third:
        players.append(("C", Scripted([])))
    eng = Engine(players, seed=9, scenario=scn or SCN)
    for _ in range(ticks):
        eng.tick()
    return eng


def evs(eng, kind):
    return [e for e in eng.events if e["k"] == kind]


def pmsg(to, text=None, treaty=None):
    m = {"to": to}
    if text is not None:
        m["text"] = text
    if treaty is not None:
        m["treaty"] = treaty
    return {"parley": [m]}


NA = {"type": "non_aggression"}

# ---- the handshake rides parley ----
eng = mk([pmsg("B", "let us fish in peace", NA)],
         [{}, pmsg(0, treaty="accept")], ticks=350, third=True)
formed = evs(eng, "treaty")
ok(len(formed) == 1 and formed[0]["type"] == "non_aggression"
   and formed[0]["fleet"] == 1 and formed[0]["other"] == 0,
   f"a parley-carried proposal, accepted in parley, forms the pact ({formed})")
par = [e["text"] for e in evs(eng, "parley")]
ok(any("[TREATY PROPOSAL" in t and "non-aggression" in t for t in par),
   "the proposal is a real parley message that announces itself")
ok(any("[TREATY SIGNED]" in t for t in par),
   "the signature lands in the same transcript")
b = eng.fleets[1].bot
ok(any(o.get("type") == "non_aggression"
       for st in b.states for o in st.get("treaty_offers", [])),
   "the recipient sees the typed offer in state.treaty_offers")
c = eng.fleets[2].bot
ok(any(t.get("type") == "non_aggression"
       for st in c.states for t in st.get("treaties", [])),
   "a third fleet sees the formed pact — treaties are public")
ok(not any(st.get("treaty_offers") for st in c.states),
   "…but never the private offer")

# ---- non-aggression: damage is NOT betrayal, sinking is ----
eng = mk([pmsg("B", "peace", NA)], [{}, pmsg(0, treaty="accept")], ticks=350)
ok(frozenset((0, 1)) in eng.treaties, "NA pact formed (break setup)")
eng._combat_note(0, 5, 1, 3, 0, 10, 10)      # a stray adjacency shot
ok(frozenset((0, 1)) in eng.treaties and not evs(eng, "treaty_end"),
   "DAMAGE does not break a non-aggression pact — passing fire is automatic, "
   "not betrayal (the v1 rule made every pact a time bomb)")
eng._treaty_sunk(0, 1)                        # the sink path's hook
broken = evs(eng, "treaty_end")
ok(len(broken) == 1 and broken[0]["cause"] == "aggression"
   and broken[0]["fleet"] == 0 and broken[0]["type"] == "non_aggression",
   f"a SINKING voids the pact and names the aggressor ({broken})")
ok(any("SANK" in w for w in eng.fleets[0].warnings)
   and any("TREATY BROKEN" in w for w in eng.fleets[1].warnings),
   "both admirals are told in words")
src = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "..", "sim", "core.py")).read()
ok("self._treaty_sunk(by, s.fleet)" in src,
   "the combat-sink path calls the hook (source anchor); the scuttle path, "
   "with by=None, cannot")

# ---- border: geometry, the flagship-side guard, and the sighting break ----
probe = Engine([("A", Idle()), ("B", Idle())], seed=9, scenario=SCN)
ax, bx = probe.fleets[0].hx, probe.fleets[1].hx
line = (ax + bx) // 2
a_side = "low" if ax < bx else "high"
BORDER = {"type": "border", "axis": "x", "line": line, "my_side": a_side}

eng = mk([pmsg("B", "stay on your side", BORDER)],
         [{}, pmsg(0, treaty="accept")], ticks=350)
formed = evs(eng, "treaty")
ok(len(formed) == 1 and formed[0]["type"] == "border"
   and formed[0]["line"] == line
   and {formed[0]["low"], formed[0]["high"]} == {0, 1},
   f"a border pact records its line and which fleet holds which side "
   f"({formed})")
rec = eng.treaties[frozenset((0, 1))]
violator = rec["high"]                       # the fleet that must stay high
watcher = 1 - violator
wf = eng.fleets[watcher]
ship = next(s for s in eng.ships.values() if s.fleet == violator)
ship.x, ship.y = wf.hx + 1, wf.hy            # across the line, in plain sight
eng.tick()
broken = evs(eng, "treaty_end")
ok(len(broken) == 1 and broken[0]["cause"] == "border"
   and broken[0]["fleet"] == violator and broken[0]["type"] == "border",
   f"a SIGHTED crossing voids the border pact and names the crosser "
   f"({broken})")

# an UNSEEN crossing does not break it — fog is the whole point
eng = mk([pmsg("B", "", BORDER)], [{}, pmsg(0, treaty="accept")], ticks=350)
rec = eng.treaties[frozenset((0, 1))]
violator = rec["high"]
ship = next(s for s in eng.ships.values() if s.fleet == violator)
ship.x, ship.y = max(0, line - 1) if violator == rec["high"] else line + 1, 0
# across the line but in the empty far corner, away from every eye
for _ in range(30):
    eng.tick()
ok(frozenset((0, 1)) in eng.treaties and not evs(eng, "treaty_end"),
   "an UNSEEN crossing goes unpunished — a border breaks on sight, not on "
   "trespass")

# the guard: a border whose line a flagship already violates is refused
bad = {"type": "border", "axis": "x",
       "line": (ax - 2) if a_side == "low" else (ax + 2), "my_side": a_side}
eng = mk([pmsg("B", "bad line", bad)], [{}], ticks=250)
ok(not eng.treaty_offers and any(
    "flagship" in w for d in eng.decisions if d["fleet"] == 0
    for w in (d.get("warns") or [])),
   "a border a flagship already violates is refused at proposal — it would "
   "break on first sight")

# border needs the contact plot
eng = mk([pmsg("B", "", BORDER)], [{}],
         scn=dict(SCN, contact_ttl=0), ticks=250)
ok(not eng.treaty_offers and any(
    "contact" in w for d in eng.decisions if d["fleet"] == 0
    for w in (d.get("warns") or [])),
   "no contact plot (contact_ttl=0) = no border treaties: a crossing could "
   "never be seen")

# ---- guard rails shared by both types ----
eng = mk([pmsg("all", "hello", NA),                 # treaty to "all"
          pmsg("B", treaty="accept"),               # no offer
          pmsg("B", "x", {"type": "friendship"})],  # unknown type
         [{}], ticks=450)
ok(not evs(eng, "treaty") and not eng.treaty_offers,
   "junk verbs form nothing: 'all' target, ghost accept, unknown type")
ok(any("treaty" in w for d in eng.decisions if d["fleet"] == 0
       for w in (d.get("warns") or [])),
   "…each refusal warns the sender")
ok(any("hello" in e["text"] for e in evs(eng, "parley")),
   "…and a refused verb never eats the plain words riding with it")

# expiry
eng = mk([pmsg("B", "old", NA)],
         [{}] * 7 + [pmsg(0, treaty="accept")],
         scn=dict(SCN, max_ticks=1200), ticks=1000)
ok(not evs(eng, "treaty")
   and any("expired" in w for d in eng.decisions if d["fleet"] == 1
           for w in (d.get("warns") or [])),
   "a stale offer (>6 windows) cannot be signed")

# dissolve, in the transcript
eng = mk([pmsg("B", "", NA), {}, pmsg("B", "it served us", treaty="dissolve")],
         [{}, pmsg(0, treaty="accept")], ticks=450)
ended = evs(eng, "treaty_end")
ok(len(ended) == 1 and ended[0]["cause"] == "dissolved",
   f"dissolve ends the pact openly ({ended})")
ok(any("[TREATY DISSOLVED]" in e["text"] for e in evs(eng, "parley")),
   "…and says so in the transcript")

# the off switch
eng = mk([pmsg("B", "try", NA)], [{}],
         scn=dict(SCN, treaties=False), ticks=250)
ok(not eng.treaty_offers
   and any("disabled" in w for d in eng.decisions if d["fleet"] == 0
           for w in (d.get("warns") or [])),
   "treaties=false refuses the verb loudly")

# ---- freeze/thaw with typed records ----
eng = mk([pmsg("B", "", BORDER)], [{}, pmsg(0, treaty="accept")], ticks=350)
data = eng.freeze()
eng2 = Engine.thaw(data, [("A", Idle()), ("B", Idle())])
rec2 = eng2.treaties.get(frozenset((0, 1)), {})
ok(rec2.get("type") == "border" and rec2.get("line") == line
   and "low" in rec2,
   f"a thawed match keeps its typed pact ({rec2})")
del data["treaties"], data["treaty_offers"]
eng3 = Engine.thaw(data, [("A", Idle()), ("B", Idle())])
ok(eng3.treaties == {} and eng3.treaty_offers == {},
   "a pre-treaty checkpoint thaws clean")

print(f"FAILURES: {len(FAILS)}")
sys.exit(1 if FAILS else 0)
