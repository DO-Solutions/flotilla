"""Dispatch cutters (#129, Opus5's playtest ask): a costly mid-course way to
re-order a committed squad. The cutter is a real, fast, unarmed, one-volley
ship — travel takes time (the mail can arrive stale) and interception loses
it. These pin the whole lifecycle plus every refusal path."""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "sim"))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                ".."))

from core import Engine, CUTTER            # noqa: E402
from bots import BOTS                      # noqa: E402
import run_config                          # noqa: E402,F401

fails = 0


def ok(cond, msg):
    global fails
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        fails += 1


def mk(extra=None):
    scen = {"max_ticks": 4000, "warmup": False, "width": 64, "height": 36,
            "clock_jitter": 0, "island_coverage": 0}
    scen.update(extra or {})
    return Engine([("P0", BOTS["merchant"]), ("P1", BOTS["merchant"])],
                  seed=9, scenario=scen)


def far_squad(e, fid, sq, n=2):
    """Move n of fleet fid's ships into squad sq, parked far from harbor."""
    f = e.fleets[fid]
    ships = [s for s in e.ships.values() if s.fleet == fid][:n]
    for i, s in enumerate(ships):
        s.squad = sq
        s.x = min(e.W - 2, f.hx + 25)
        s.y = min(e.H - 2, f.hy + 8 + i)
        s.orders = dict(role="guard")
    return ships


def run_until(e, cond, ticks=3000):
    for _ in range(ticks):
        e.tick()
        e.t += 1
        if cond():
            return True
    return False


NEW_ORDER = {"role": "escort"}

# ---- 1. the whole lifecycle: same-window orders+dispatch, sail, deliver at
# sea, sail home, absorbed ----
e = mk()
f0 = e.fleets[0]
squad = far_squad(e, 0, "D")
f0.cargo = 50
cargo0 = f0.cargo
e._apply_actions(f0, {"orders": {"D": dict(NEW_ORDER)},
                      "dispatch": "D", "thoughts": ""})
cutters = [s for s in e.ships.values() if s.courier is not None]
ok(len(cutters) == 1 and cutters[0].preset == "cutter"
   and cutters[0].stats == CUTTER,
   "orders + dispatch in ONE reply launches a cutter (orders apply first)")
ok(f0.cargo == cargo0 - e.cfg["courier_cost"],
   f"launch costs courier_cost ({cargo0} -> {f0.cargo})")
ok(all(s.orders.get("role") == "guard" for s in squad),
   "the squad still sails on its OLD orders while the mail travels")
delivered = run_until(e, lambda: all(s.orders.get("role") == "escort"
                                     for s in squad))
ok(delivered, "the cutter reaches the squad and delivers AT Sea")
ok(any(ev.get("k") == "courier" and ev.get("what") == "delivered"
       for ev in e.events), "delivery is on the event record")
gone = run_until(e, lambda: not any(s.courier is not None
                                    for s in e.ships.values()))
ok(gone and any(ev.get("k") == "courier" and ev.get("what") == "home"
                for ev in e.events),
   "the cutter sails home and is absorbed")

# ---- 2. refusals: nothing pending / no such squad / can't afford ----
e = mk()
f0 = e.fleets[0]
far_squad(e, 0, "D")   # squads A-C have init-seeded pending; D never does
f0.cargo = 50
e._apply_actions(f0, {"dispatch": "D", "thoughts": ""})
ok(not any(s.courier for s in e.ships.values())
   and any("no standing orders" in w for w in f0.warnings),
   "no pending orders -> refused with the fix spelled out")
f0.warnings.clear()
e._apply_actions(f0, {"dispatch": "Z", "thoughts": ""})
ok(any("no ships" in w for w in f0.warnings),
   "an empty squad -> refused (nothing to deliver to)")
f0.warnings.clear()
f0.pending["D"] = dict(NEW_ORDER)
f0.cargo = 3
e._apply_actions(f0, {"dispatch": "D", "thoughts": ""})
ok(any("costs" in w for w in f0.warnings)
   and not any(s.courier for s in e.ships.values()),
   "an unaffordable cutter is refused, not queued")

# ---- 3. the knob: couriers off -> the action warns and does nothing ----
e = mk({"couriers": False})
f0 = e.fleets[0]
far_squad(e, 0, "B")
f0.pending["B"] = dict(NEW_ORDER)
f0.cargo = 50
e._apply_actions(f0, {"dispatch": "B", "thoughts": ""})  # noqa
ok(any("disabled" in w for w in f0.warnings)
   and not any(s.courier for s in e.ships.values()),
   "couriers=false disables the action loudly")
ok("DISPATCH CUTTER" not in e.cfg["rules"],
   "...and the rules text does not advertise it")
e2 = mk()
ok("DISPATCH CUTTER" in e2.cfg["rules"]
   and "ONLY way to re-task" in e2.cfg["rules"],
   "couriers on (default) + return_only: the rules teach the cutter")

# ---- 4. interception: a sunk cutter loses the mail, loudly ----
e = mk()
f0 = e.fleets[0]
squad = far_squad(e, 0, "D")
f0.cargo = 50
e._apply_actions(f0, {"orders": {"D": dict(NEW_ORDER)},
                      "dispatch": "D", "thoughts": ""})
cutter = next(s for s in e.ships.values() if s.courier is not None)
f0.warnings.clear()
e._apply_actions(f0, {"scuttle": [cutter.id], "thoughts": ""})
ok(cutter.id not in e.ships
   and any("SUNK" in w and "never arrived" in w for w in f0.warnings),
   "a lost cutter tells its admiral the orders never arrived")
ok(any(ev.get("k") == "courier" and ev.get("what") == "lost"
       for ev in e.events), "the loss is on the event record")
ok(all(s.orders.get("role") == "guard" for s in squad),
   "the squad keeps its old orders — the mail went down with the boat")

# ---- 5. squad wiped mid-flight: the cutter turns for home ----
e = mk()
f0 = e.fleets[0]
squad = far_squad(e, 0, "D", n=1)
f0.cargo = 50
e._apply_actions(f0, {"orders": {"D": dict(NEW_ORDER)},
                      "dispatch": "D", "thoughts": ""})
e._apply_actions(f0, {"scuttle": [squad[0].id], "thoughts": ""})
f0.warnings.clear()
homed = run_until(e, lambda: not any(s.courier is not None
                                     for s in e.ships.values()))
ok(homed and any("no ships left" in w for w in f0.warnings),
   "a cutter whose squad is gone returns home and reports it")

# ---- 6. the mail bag survives freeze -> JSON -> thaw mid-flight ----
e = mk()
f0 = e.fleets[0]
squad = far_squad(e, 0, "D")
f0.cargo = 50
e._apply_actions(f0, {"orders": {"D": dict(NEW_ORDER)},
                      "dispatch": "D", "thoughts": ""})
for _ in range(20):
    e.tick()
    e.t += 1
frozen = json.loads(json.dumps(e.freeze()))
e2 = Engine.thaw(frozen, [("P0", BOTS["merchant"]), ("P1", BOTS["merchant"])])
c2 = [s for s in e2.ships.values() if s.courier is not None]
ok(len(c2) == 1 and c2[0].courier["orders"].get("role") == "escort"
   and c2[0].courier["state"] == "out",
   "the courier + its mail bag survive freeze -> JSON -> thaw")
sq2 = [s for s in e2.ships.values()
       if s.fleet == 0 and s.squad == "D" and s.courier is None]
ok(run_until(e2, lambda: all(s.orders.get("role") == "escort" for s in sq2)),
   "...and the thawed cutter still delivers")

# ---- 7. a cutter holds no ground and pads no tiebreak ----
e = mk({"win": "territory", "territories": 5})
f0 = e.fleets[0]
far_squad(e, 0, "B")
f0.pending["B"] = dict(NEW_ORDER)
f0.cargo = 50
e._apply_actions(f0, {"dispatch": "B", "thoughts": ""})
cutter = next(s for s in e.ships.values() if s.courier is not None)
pres = e._presence()
ok(all(cutter.fleet not in v or any(
    s.courier is None and s.fleet == cutter.fleet
    and e._cellregion[s.x][s.y] == rid for s in e.ships.values())
    for rid, v in pres.items()),
   "territory presence never counts the mail boat")
e = mk()
f0 = e.fleets[0]
far_squad(e, 0, "B")
f0.pending["B"] = dict(NEW_ORDER)
f0.cargo = 50
base_worth = dict(e.tiebreak_rungs()[-1][1])
e._apply_actions(f0, {"dispatch": "B", "thoughts": ""})
worth = dict(e.tiebreak_rungs()[-1][1])
ok(worth[0] == base_worth[0] - e.cfg["courier_cost"],
   "net-worth tiebreak: the cutter itself adds nothing (only its cost left)")

print("FAILURES:", fails)
sys.exit(1 if fails else 0)
