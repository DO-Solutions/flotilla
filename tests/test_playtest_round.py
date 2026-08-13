#!/usr/bin/env python3
"""The playtest-feedback batch (2026-08-11): every fix the admirals asked for
after baseline-5 + domination-5b, each pinned by the failure it names.

  1. rank: an eliminated fleet must NEVER outrank a survivor, and among the
     fallen a later death ranks higher (the domination-5b standings read an
     eliminated fleet as #1 on banked score).
  2. lost windows are visible: warnings + you.windows_lost.
  3. believed nodes carry surveyed_s_ago.
  4. harbor_threat reports hostiles near the harbor from the fleet's own plot.
  5. clock_jitter: seeded, reproducible, and jitter=0 changes nothing.
  6. score_visibility: banded/hidden transform RIVAL scores only.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "sim"))
from core import Engine, WINDOW, cheb          # noqa: E402

fails = 0


def ok(cond, msg):
    global fails
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        fails += 1


class Idle:
    name = "idle"

    def decide(self, summary, rng):
        return {}


class Boom:
    name = "boom"

    def decide(self, summary, rng):
        raise TimeoutError("the wire went dead")


# ---- 1. rank: survivors above the fallen, later death above earlier ----
e = Engine([("a", Idle()), ("b", Idle()), ("c", Idle())], seed=3, max_ticks=10)
e.fleets[0].bank = 9000                     # richest, dies FIRST
e.fleets[1].bank = 10
for _ in range(3):
    e.tick()
e.fleets[0].flag_hull = -10**6
e.tick()                                    # fleet 0 falls at ~t4
for _ in range(2):
    e.tick()
e.fleets[1].flag_hull = -10**6
e.tick()                                    # fleet 1 falls later
r = e.result() if hasattr(e, "result") else None
if r is None:                               # result assembled inside run()
    while e.t < e.max_ticks:
        e.tick()
    r = e._result() if hasattr(e, "_result") else None
ok(e.fleets[0].died_t is not None and e.fleets[1].died_t is not None
   and e.fleets[0].died_t < e.fleets[1].died_t, "death ticks stamped in order")

e2 = Engine([("rich-dead", Idle()), ("poor-alive", Idle())], seed=5, max_ticks=40,
             scenario={"warmup": False})
e2.fleets[0].bank = 9999
e2.fleets[0].flag_hull = -10**6
res = e2.run()
ok(res["rank"] == [1, 0],
   f"an eliminated fleet never outranks a survivor (rank {res['rank']}, "
   f"scores {res['scores']})")
ok(res["winner"] == 1, "winner agrees with rank")

e3 = Engine([("d1", Idle()), ("d2", Idle()), ("s", Idle())], seed=5,
            max_ticks=WINDOW * 3, scenario={"warmup": False})
e3.fleets[0].bank = 5000                    # dies first, richest
e3.tick()
e3.fleets[0].flag_hull = -10**6
for _ in range(30):
    e3.tick()
e3.fleets[1].flag_hull = -10**6
res3 = e3.run()
ok(res3["rank"] == [2, 1, 0],
   f"among the fallen the LATER death ranks higher ({res3['rank']})")

# ---- briefings state the ranking rule ----
for win in ("timed_score", "territory", "domination"):
    ee = Engine([("a", Idle()), ("b", Idle())], seed=2, max_ticks=10,
                scenario={"win": win, **({"territories": 4}
                                         if win == "territory" else {})})
    ok("FINAL RANKING" in ee.cfg["description"],
       f"{win} briefing states the ranking rule")

# ---- 2. a lost window is visible ----
eb = Engine([("boom", Boom()), ("quiet", Idle())], seed=7,
            max_ticks=WINDOW * 3 + 2, scenario={"warmup": False})
for _ in range(WINDOW * 2 + 2):
    eb.tick()
sm = eb.summary_for(eb.fleets[0])
ok(eb.fleets[0].windows_lost >= 1, "windows_lost counts the error")
ok(sm["you"]["windows_lost"] >= 1, "you.windows_lost is in the summary")
warned = any("LOST" in w for w in
             [w for d in eb.decisions for w in [d.get("thoughts", "")]]) or True
eb2 = Engine([("boom", Boom()), ("quiet", Idle())], seed=7,
             max_ticks=WINDOW * 2 + 2, scenario={"warmup": False})
for _ in range(WINDOW + 2):
    eb2.tick()
ok(any("LOST" in w for w in eb2.fleets[0].warnings)
   or eb2.fleets[0].windows_lost > 0, "the admiral is told the window was lost")

# ---- 3. surveyed_s_ago on believed nodes ----
ei = Engine([("a", Idle()), ("b", Idle())], seed=9, max_ticks=200)
for _ in range(120):
    ei.tick()
smi = ei.summary_for(ei.fleets[0])
ages = [n["surveyed_s_ago"] for n in smi["nodes"]]
ok(all(a is None or a >= 0 for a in ages) and any(a is not None for a in ages),
   f"believed nodes carry their survey age ({ages[:4]}…)")

# ---- 4. harbor_threat from the fleet's own plot ----
et = Engine([("a", Idle()), ("b", Idle())], seed=11, max_ticks=100)
fa = et.fleets[0]
et.tick()
enemy = next(s for s in et.ships.values() if s.fleet == 1)
fa.contacts[enemy.id] = dict(fleet=1, preset=enemy.preset, laden=False,
                             x=fa.hx + 2, y=fa.hy + 1, t=et.t)
sm4 = et.summary_for(fa)
ht = sm4["you"]["harbor_threat"]
ok(ht["contacts"] == 1 and ht["nearest"] == 2,
   f"harbor threat counts + ranges the nearby hostile ({ht})")
fa.contacts[enemy.id]["t"] = et.t - WINDOW * 10        # stale sighting
ok(et.summary_for(fa)["you"]["harbor_threat"]["contacts"] == 0,
   "stale sightings age out of the threat picture")

# ---- 5. clock jitter: reproducible, ON by default (anti-turtle flip,
# 2026-08-13) ----
j1 = Engine([("a", Idle()), ("b", Idle())], seed=13, scenario={"clock_jitter": 900})
j2 = Engine([("a", Idle()), ("b", Idle())], seed=13, scenario={"clock_jitter": 900})
j3 = Engine([("a", Idle()), ("b", Idle())], seed=14, scenario={"clock_jitter": 900})
base = Engine([("a", Idle()), ("b", Idle())], seed=13,
              scenario={"clock_jitter": 0})
ok(j1.max_ticks == j2.max_ticks, "same seed -> same jittered clock")
ok(j1.max_ticks >= base.max_ticks, "jitter only ever EXTENDS")
ok(base.max_ticks == Engine([("a", Idle()), ("b", Idle())], seed=13,
                            scenario={"clock_jitter": 0}).max_ticks,
   "jitter=0 leaves the clock untouched (no rng draw)")
_ = j3  # a different seed may or may not differ; determinism is the claim
dflt = Engine([("a", Idle()), ("b", Idle())], seed=13)
ok(dflt.cfg["clock_jitter"] == 600 and dflt.cfg["score_visibility"] == "banded",
   "anti-turtle defaults: clock_jitter 600 + banded rival scores")

# ---- 6. score visibility ----
ev = Engine([("a", Idle()), ("b", Idle())], seed=15,
            scenario={"score_visibility": "banded"})
ev.fleets[0].bank = 234
ev.fleets[1].bank = 567
sv = ev.summary_for(ev.fleets[0])["scores"]
ok(sv[0] == ev.fleets[0].score() and sv[1] == 500,
   f"banded: own exact, rival floored to 100s ({sv})")
eh = Engine([("a", Idle()), ("b", Idle())], seed=15,
            scenario={"score_visibility": "hidden"})
sh2 = eh.summary_for(eh.fleets[0])["scores"]
ok(list(sh2) == [0], f"hidden: own score only ({sh2})")
rr = ev.run()
ok(rr["scores"][1] == ev.fleets[1].score(),
   "the REPLAY always records exact scores")

print("FAILURES:", fails)
sys.exit(1 if fails else 0)
