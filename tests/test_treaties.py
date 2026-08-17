#!/usr/bin/env python3
"""Official treaties: recorded and announced, never enforced.

The design brief asked for "an alliance forming or breaking" as a spectator
beat. The engine's answer is a formal PUBLIC pact: proposed and signed as
actions, listed in everyone's state, announced to all when formed, dissolved,
or broken. The rules of the game do not change — damage, territory, and
vision are treaty-blind — so the ONE enforcement is informational: damaging a
partner voids the pact instantly, and the aggressor is named in the public
record. What can rot here:

  * the offer/accept handshake (including expiry and unknown-fleet junk)
  * the betrayal choke point — every damage path flows through _combat_note,
    so a treaty must break on ANY partner damage, with cause and culprit
  * publicity — every admiral sees every pact; offers stay private
  * survival through freeze/thaw (a resumed match must keep its pacts)
  * the off switch, and old configs that predate the knob
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


SCN = {"width": 48, "height": 32, "max_ticks": 800, "warmup": False,
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


# ---- the handshake ----
eng = mk([{"treaty": {"propose": "B", "terms": "no raids on trawlers"}}],
         [{}, {"treaty": {"accept": 0}}], ticks=350, third=True)
formed = evs(eng, "treaty")
ok(len(formed) == 1 and formed[0]["fleet"] == 1 and formed[0]["other"] == 0
   and formed[0]["terms"] == "no raids on trawlers",
   f"propose -> accept forms ONE treaty with the terms on record ({formed})")
ok(frozenset((0, 1)) in eng.treaties, "the pact is in force in engine state")
b_bot = eng.fleets[1].bot
offer_seen = any(o.get("from_fleet") == 0
                 for st in b_bot.states for o in st.get("treaty_offers", []))
ok(offer_seen, "the recipient sees the offer in state.treaty_offers")
inbox_note = any("[TREATY OFFER]" in m.get("text", "")
                 for st in b_bot.states for m in st.get("messages", []))
ok(inbox_note, "…and as a message in the diplomacy inbox it cannot miss")
c_bot = eng.fleets[2].bot
c_sees = any(sorted(t.get("between", [])) == [0, 1]
             for st in c_bot.states for t in st.get("treaties", []))
c_offer = any(st.get("treaty_offers") for st in c_bot.states)
ok(c_sees, "a THIRD fleet sees the formed pact — treaties are public")
ok(not c_offer, "…but never the private offer")

# ---- betrayal: damage through the choke point breaks it, names the breaker.
# EVERY damage site in the engine reports through _combat_note (assault-flag
# hits, ship-vs-ship, flagship adjacency, flagship guns), so the break is
# tested at that choke point — with dealt>0 exactly as the aggressor's side
# of a real hit reports it.
eng = mk([{"treaty": {"propose": "B", "terms": "peace"}}],
         [{}, {"treaty": {"accept": 0}}], ticks=350)
ok(frozenset((0, 1)) in eng.treaties, "pact formed (betrayal setup)")
eng._combat_note(0, 5, 1, 3, 0, 10, 10)      # A's ship deals 3 to B
broken = evs(eng, "treaty_end")
ok(len(broken) == 1 and broken[0]["cause"] == "aggression"
   and broken[0]["fleet"] == 0 and broken[0]["other"] == 1,
   f"damage to a partner voids the pact and NAMES the aggressor ({broken})")
ok(frozenset((0, 1)) not in eng.treaties, "the pact is gone from state")
ok(any("FIRED ON" in w for w in eng.fleets[0].warnings)
   and any("TREATY BROKEN" in w for w in eng.fleets[1].warnings),
   "both sides are told in words, not just events")
eng._combat_note(0, 5, 1, 3, 0, 10, 10)      # hitting again after the break
ok(len(evs(eng, "treaty_end")) == 1,
   "…and further damage is just war, not a second break event")
# the VICTIM's side of the same hit (taken>0, dealt=0) must never re-brand
eng2 = mk([{"treaty": {"propose": "B", "terms": "p"}}],
         [{}, {"treaty": {"accept": 0}}], ticks=350)
eng2._combat_note(1, 7, 0, 0, 3, 10, 10)     # B RECORDS taking damage
ok(not evs(eng2, "treaty_end") and frozenset((0, 1)) in eng2.treaties,
   "the victim-side combat record (taken>0, dealt=0) does not break the pact")

# ---- honorable exit ----
eng = mk([{"treaty": {"propose": "B", "terms": "t"}}, {},
          {"treaty": {"dissolve": 1}}],
         [{}, {"treaty": {"accept": 0}}], ticks=450)
ended = evs(eng, "treaty_end")
ok(len(ended) == 1 and ended[0]["cause"] == "dissolved"
   and ended[0]["fleet"] == 0,
   f"dissolve ends the pact openly, cause=dissolved ({ended})")

# ---- junk + guard rails ----
eng = mk([{"treaty": {"propose": "Nobody"}},
          {"treaty": {"accept": 1}},                 # no offer exists
          {"treaty": "not a dict"},
          {"treaty": {"weird": 1}}],
         [{}], ticks=550)
ok(not evs(eng, "treaty") and not evs(eng, "treaty_end"),
   "junk treaty actions form nothing and end nothing")
ok(any("treaty" in w for d in eng.decisions if d["fleet"] == 0
       for w in (d.get("warns") or [])),
   "…and the admiral is told why, not silently ignored (decision forensics)")

# ---- offer expiry ----
eng = mk([{"treaty": {"propose": "B", "terms": "old offer"}}],
         [{}] * 7 + [{"treaty": {"accept": 0}}],
         scn=dict(SCN, max_ticks=1200), ticks=1000)
ok(not evs(eng, "treaty")
   and any("expired" in w for d in eng.decisions if d["fleet"] == 1
           for w in (d.get("warns") or [])),
   "a stale offer (>6 windows) cannot be accepted — the acceptor is told")

# ---- the off switch, and configs that predate the knob ----
eng = mk([{"treaty": {"propose": "B"}}], [{}],
         scn=dict(SCN, treaties=False), ticks=250)
ok(not eng.treaty_offers and not evs(eng, "treaty")
   and any("disabled" in w for d in eng.decisions if d["fleet"] == 0
           for w in (d.get("warns") or [])),
   "treaties=false ignores the action loudly")
eng = Engine([("A", Idle()), ("B", Idle())], seed=3, scenario=SCN)
ok(eng.cfg.get("treaties", True) is True,
   "the knob defaults ON (and cfg.get guards configs that predate it)")

# ---- freeze/thaw carries pacts; old checkpoints thaw to none ----
eng = mk([{"treaty": {"propose": "B", "terms": "carry me"}}],
         [{}, {"treaty": {"accept": 0}}], ticks=350)
data = eng.freeze()
eng2 = Engine.thaw(data, [("A", Idle()), ("B", Idle())])
ok(eng2.treaties.get(frozenset((0, 1)), {}).get("terms") == "carry me",
   "a thawed match keeps its pacts")
del data["treaties"], data["treaty_offers"]          # a pre-treaty checkpoint
eng3 = Engine.thaw(data, [("A", Idle()), ("B", Idle())])
ok(eng3.treaties == {} and eng3.treaty_offers == {},
   "a checkpoint from before the feature thaws clean")

print(f"FAILURES: {len(FAILS)}")
sys.exit(1 if FAILS else 0)
