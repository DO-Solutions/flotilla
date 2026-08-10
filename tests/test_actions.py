"""Action-field isolation + order forensics (the Kimi build_yard bug,
2026-08-09): one malformed field must never drop the fields after it; every
decision records which fields it carried (acts) and the warnings it generated
(warns); unknown fields warn instead of vanishing. Plus the domination
flag_salvage default (50 unless explicitly set)."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "sim"))
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

    def decide(self, summary, rng):
        return self.queue.pop(0) if self.queue else {}


SCN = {"width": 48, "height": 32, "max_ticks": 400, "warmup": False,
       "start_cargo": 100, "flag_move": True}


def run(actions_queue, scn=None, ticks=250):
    eng = Engine([("A", Scripted(actions_queue)), ("B", Idle())], seed=9,
                 scenario=scn or SCN)
    for _ in range(ticks):
        eng.tick()
    return eng


# 1. THE Kimi scenario: a field that throws (malformed relocate under
#    flag_move) rides in the same reply as build + build_yard — both must
#    still apply, and the bad field gets a named warning
eng = run([{"relocate": [9999, 9999],       # out of bounds -> raises
            "build": [{"preset": "trawler", "squad": "A"}],
            "build_yard": True}])
ok(any(e["k"] == "yard_start" and e["fleet"] == 0 for e in eng.events),
   "build_yard applies even when an earlier field throws")
ok(any(e["k"] == "build_start" and e["fleet"] == 0 for e in eng.events),
   "build applies even when an earlier field throws")
w = [d for d in eng.decisions if d.get("fleet") == 0 and d.get("warns")]
ok(w and any("relocate" in x for x in w[0]["warns"]),
   f"the bad field is named in the recorded warning "
   f"({w[0]['warns'] if w else 'none'})")

# 2. build + build_yard same window with funds for both (Kimi W9/W10)
eng2 = run([{"build": [{"preset": "trawler", "squad": "A"}],
             "build_yard": True}])
ok(any(e["k"] == "yard_start" for e in eng2.events) and
   any(e["k"] == "build_start" for e in eng2.events),
   "build + build_yard in one reply both start")

# 3. unknown top-level fields warn instead of silently vanishing
eng3 = run([{"yard_expand": True, "build_yard": True}])
d3 = next(d for d in eng3.decisions if d.get("fleet") == 0 and d.get("warns"))
ok(any("unknown field 'yard_expand'" in x for x in d3["warns"]),
   "unknown field warned")
ok(any(e["k"] == "yard_start" for e in eng3.events),
   "the correctly-named field still applied")

# 4. decisions record which action fields the reply carried
eng4 = run([{"build_yard": True, "parley": [], "thoughts": "hi"}])
d4 = next(d for d in eng4.decisions if d.get("fleet") == 0 and d.get("acts"))
ok(d4["acts"] == ["build_yard", "parley"],
   f"acts records the reply's action fields ({d4['acts']})")

# 5. domination flag_salvage default = 50; explicit value wins; other modes 0
e5 = Engine([("A", Idle()), ("B", Idle())], seed=1,
            scenario={"win": "domination"})
ok(e5.cfg["flag_salvage"] == 50, "domination default flag_salvage = 50")
e6 = Engine([("A", Idle()), ("B", Idle())], seed=1,
            scenario={"win": "domination", "flag_salvage": 200})
ok(e6.cfg["flag_salvage"] == 200, "explicit flag_salvage wins")
e7 = Engine([("A", Idle()), ("B", Idle())], seed=1, scenario={})
ok(e7.cfg["flag_salvage"] == 0, "timed_score default stays 0")

# 6. flagship battery: the whole command circle is a kill zone, up to
#    flag_targets ships per volley (nearest first), outside-zone untouched
e8 = Engine([("A", Idle()), ("B", Idle())], seed=3,
            scenario={"width": 48, "height": 32, "warmup": False,
                      "flag_targets": 4, "harbor_r": 3})
fa = e8.fleets[0]
while sum(1 for sh in e8.ships.values() if sh.fleet == 1) < 6:
    e8._spawn(e8.fleets[1], "trawler", "A")
foes = [sh for sh in e8.ships.values() if sh.fleet == 1]
# park 5 enemies INSIDE A's circle at staggered ranges + 1 outside it
spots = [(1, 0), (2, 0), (0, 2), (3, 0), (0, 3)]
for sh, (dx, dy) in zip(foes, spots):
    sh.x, sh.y = fa.hx + dx, fa.hy + dy
ok(len(foes) > 5, f"fixture must yield >5 foes for the outside case "
   f"({len(foes)})")
outside = foes[5]
outside.x, outside.y = fa.hx + e8.cfg["harbor_r"] + 2, fa.hy
hull0 = {sh.id: sh.hull for sh in foes}
e8.t = e8.cfg["volley"]                    # a volley tick
e8.tick()
hit = [sh.id for sh in foes[:5] if sh.hull < hull0[sh.id]]
ok(len(hit) == 4, f"battery fires on exactly flag_targets ships ({len(hit)})")
d3 = [sh for sh in foes[:5]
      if max(abs(sh.x - fa.hx), abs(sh.y - fa.hy)) == 3]
spared = [sh for sh in d3 if sh.hull == hull0[sh.id]]
ok(len(spared) == 1, f"exactly one range-3 ship is the spared fifth "
   f"({len(spared)})")
ok(outside.hull == hull0[outside.id], "outside the circle: untouched")

print(f"FAILURES: {len(FAILS)}")
sys.exit(1 if FAILS else 0)
