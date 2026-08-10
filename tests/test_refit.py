#!/usr/bin/env python3
"""Refits + custom ship designs: dock conversion, standing directives, validation,
operator classes, budget rule, replay stamping."""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "sim"))
from core import Engine, WINDOW, PRESETS     # noqa: E402

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


class Scripted:
    name = "scripted"

    def __init__(self, queue):
        self.queue = list(queue)
        self.summaries = []

    def decide(self, summary, rng):
        self.summaries.append(summary)
        return self.queue.pop(0) if self.queue else {}


# --- refit: docked ships convert, pay, and it's a standing directive ---
bot = Scripted([{"refit": {"A": "frigate"}}])
eng = Engine([("R", bot), ("X", Idle())], seed=7,
             scenario={"shipyard_slots": 8})
f0 = eng.fleets[0]
f0.cargo = 100
start_cargo_after_w0 = None
for _ in range(WINDOW + 5):
    eng.tick()
a_ships = [s for s in eng.ships.values() if s.fleet == 0 and s.squad == "A"]
docked_a = [s for s in a_ships if max(abs(s.x - f0.hx), abs(s.y - f0.hy)) <= eng.hr]
refit_evs = [e for e in eng.events if e["k"] == "refit" and e["fleet"] == 0]
ok(len(refit_evs) >= 1, f"docked ships refit ({len(refit_evs)} events)")
ok(all(s.preset == "frigate" and s.stats == PRESETS["frigate"] for s in docked_a),
   "refitted ships carry the new class + stats")
deposited = sum(e["amount"] for e in eng.events
                if e["k"] == "deposit" and e["fleet"] == 0)
ok(f0.cargo == 100 + deposited - len(refit_evs) * eng.cfg["refit_cost"],
   "each refit charges refit_cost (net of deposits)")
ok(f0.pending_refits.get("A") == "frigate", "directive STANDS until cleared")

bot2 = Scripted([{"refit": {"A": "frigate"}}, {"refit": {"A": None}}])
eng2 = Engine([("R", bot2), ("X", Idle())], seed=7)
for _ in range(WINDOW * 2 + 5):
    eng2.tick()
ok("A" not in eng2.fleets[0].pending_refits, "null clears the refit directive")

engpoor = Engine([("R", Scripted([{"refit": {"A": "frigate"}}])), ("X", Idle())], seed=7)
engpoor.fleets[0].cargo = 0
for _ in range(WINDOW + 5):
    engpoor.tick()
ok(not any(e["k"] == "refit" for e in engpoor.events),
   "no cargo, no refit (ships wait)")

# --- delayed refit: drydock hold, completes on time, paid up front ---
engd = Engine([("R", Scripted([{"refit": {"A": "frigate"}}])), ("X", Idle())],
              seed=7, scenario={"refit_ticks": 200, "shipyard_slots": 8})
engd.fleets[0].cargo = 100
for _ in range(WINDOW + 5):
    engd.tick()
starts = [e for e in engd.events if e["k"] == "refit_start"]
ok(len(starts) >= 1 and not any(e["k"] == "refit" for e in engd.events),
   "delayed refit: work starts, conversion pending")
holding = [s for s in engd.ships.values() if s.refit_to == "frigate"]
ok(holding and all("drydock" in s.intent for s in holding),
   "refitting ships hold in drydock with visible intent")
for _ in range(220):
    engd.tick()
done = [e for e in engd.events if e["k"] == "refit"]
ok(len(done) == len(starts), "drydock works complete after refit_ticks")
ok(all(s.refit_to is None for s in engd.ships.values()), "drydock state clears")

# --- designs: create, build, refit into; validation + caps ---
CORVETTE = {"speed": 4, "hold": 1, "guns": 2, "armor": 2, "hull": 2, "lookout": 1}
bot3 = Scripted([
    {"designs": {"corvette": CORVETTE},
     "build": [{"preset": "corvette", "squad": "D"}],
     "refit": {"B": "corvette"}},
])
eng3 = Engine([("D", bot3), ("X", Idle())], seed=7)
eng3.fleets[0].cargo = 100
for _ in range(WINDOW * 3):
    eng3.tick()
f3 = eng3.fleets[0]
ok(f3.designs.get("corvette") == CORVETTE, "design accepted")
built = [s for s in eng3.ships.values() if s.fleet == 0 and s.preset == "corvette"]
ok(any(s.squad == "D" for s in built), "custom class is buildable")
ok(all(s.stats == CORVETTE for s in built), "built ships carry the designed stats")
ok(any(e["k"] == "design" and e["name"] == "corvette" for e in eng3.events),
   "design recorded in events")
rp = eng3.replay(dict(ticks=0, scores={}, alive=[], winner=0,
                      names={0: "D", 1: "X"}))
ok(rp["fleets"][0]["designs"].get("corvette") == CORVETTE,
   "replay stamps the fleet's designs (viewer shape/stats source)")

bad = Scripted([{"designs": {
    "cheat": {"speed": 9, "hold": 9, "guns": 9, "armor": 9, "hull": 9, "lookout": 9},
    "trawler": CORVETTE,
    "ok-1": CORVETTE, "ok-2": CORVETTE, "ok-3": CORVETTE, "ok-4": CORVETTE,
    "ok-5": CORVETTE}}])
eng4 = Engine([("B", bad), ("X", Idle())], seed=7)
eng4.tick()
f4 = eng4.fleets[0]
ok("cheat" not in f4.designs, "over-budget design rejected")
ok("trawler" not in f4.designs or f4.designs.get("trawler") != CORVETTE,
   "built-in names are reserved")
ok(len(f4.designs) <= 4, f"max 4 custom classes ({len(f4.designs)})")
ok(any(e["k"] == "design_rejected" for e in eng4.events), "rejections are events")

# --- operator classes: symmetric, in meta.presets, buildable by everyone ---
eng5 = Engine([("A", Idle()), ("B", Idle())], seed=7,
              scenario={"ship_designs": json.dumps({"clipper": CORVETTE})})
ok(eng5.class_stats(eng5.fleets[1], "clipper") == CORVETTE,
   "operator class available to all fleets")
rp5 = eng5.replay(dict(ticks=0, scores={}, alive=[], winner=0,
                       names={0: "A", 1: "B"}))
ok(rp5["meta"]["presets"].get("clipper") == CORVETTE,
   "operator class lands in meta.presets")
ok("clipper" in eng5.scenario["rules"], "rules digest lists operator classes")
try:
    Engine([("A", Idle()), ("B", Idle())], seed=1,
           scenario={"ship_designs": json.dumps({"x": {"speed": 99}})})
    ok(False, "invalid operator class fails loudly")
except Exception:
    ok(True, "invalid operator class fails loudly")

# --- flex_design: variable point totals, cost scales with size ---
CUTTER = {"speed": 1, "hold": 1, "guns": 1, "armor": 1, "hull": 1, "lookout": 1}
DREAD = {"speed": 4, "hold": 2, "guns": 6, "armor": 6, "hull": 4, "lookout": 2}
engf = Engine([("F", Scripted([{"designs": {"cutter": CUTTER, "dread": DREAD},
                                "build": [{"preset": "cutter", "squad": "A"},
                                          {"preset": "dread", "squad": "B"}]}])),
               ("X", Idle())], seed=7,
              scenario={"flex_design": True, "design_points_max": 24})
ff = engf.fleets[0]
ff.cargo = 100
engf.tick()
ok(ff.designs.get("cutter") == CUTTER and ff.designs.get("dread") == DREAD,
   "flex: 6-point and 24-point classes both accepted")
ok(engf.class_cost(ff, "cutter") == round(15 * 6 / 12)
   and engf.class_cost(ff, "dread") == round(15 * 24 / 12),
   f"flex: cost scales with points (cutter {engf.class_cost(ff, 'cutter')}, "
   f"dread {engf.class_cost(ff, 'dread')})")
ok(ff.cargo == 100 - engf.class_cost(ff, "cutter") - engf.class_cost(ff, "dread"),
   "flex: builds charged the scaled costs")
ok(engf._clean_design("toofat", {**DREAD, "hull": 5}) is None,
   "flex: over design_points_max rejected")
ok("scales with the total" in engf.scenario["rules"], "digest documents flex pricing")
engnf = Engine([("F", Idle()), ("X", Idle())], seed=7)
ok(engnf._clean_design("cutter", CUTTER) is None,
   "flex off (default): non-12-point class rejected")
ok(engnf.class_cost(engnf.fleets[0], "trawler") == 15,
   "flex off: flat ship_cost")

# allow_designs=False ignores live designs
eng6 = Engine([("D", Scripted([{"designs": {"corvette": CORVETTE}}])), ("X", Idle())],
              seed=7, scenario={"allow_designs": False})
eng6.tick()
ok(eng6.fleets[0].designs == {}, "allow_designs=False ignores design actions")

# determinism with designs + refits in play
def play(seed):
    e = Engine([("D", Scripted([{"designs": {"corvette": CORVETTE},
                                 "build": [{"preset": "corvette", "squad": "D"}],
                                 "refit": {"A": "corvette"}}])),
                ("X", Idle())], seed=seed, max_ticks=WINDOW * 3)
    r = e.run()
    return json.dumps(e.replay(r), sort_keys=True)


ok(play(11) == play(11), "byte-determinism holds with designs + refits")

print("FAILURES:", fails)
sys.exit(1 if fails else 0)
