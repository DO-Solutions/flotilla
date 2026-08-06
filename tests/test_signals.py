#!/usr/bin/env python3
"""Signal modes: return_only recall loop, preset flags, custom push + cap,
mode gating, and charging only on valid hoists."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "sim"))
from core import Engine, cheb            # noqa: E402

fails = 0


def ok(cond, msg):
    global fails
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        fails += 1


class Scripted:
    """Feeds a fixed queue of action dicts, one per window."""
    name = "scripted"

    def __init__(self, queue):
        self.queue = list(queue)

    def decide(self, summary, rng):
        return self.queue.pop(0) if self.queue else {}


class Idle:
    name = "idle"

    def decide(self, summary, rng):
        return {}


def at_sea(eng, fid):
    f = eng.fleets[fid]
    return [s for s in eng.ships.values() if s.fleet == fid
            and cheb(s.x, s.y, f.hx, f.hy) > eng.hr]


# --- return_only: recall -> home -> new orders -> back out ---
bot = Scripted([
    dict(orders={"B": dict(role="scout", rally=[60, 30], aggression=0)}),  # w0: send B out
    {},                                                                     # w1
    dict(orders={"B": dict(role="guard", rally=[20, 20], aggression=1)},    # w2: new pending
         signal={"return": ["B"]}),                                         #     + recall
])
eng = Engine([("R", bot), ("X", Idle())], seed=11, scenario={"signal_mode": "return_only"})
for _ in range(210):                     # through w2's hoist
    eng.tick()
f0 = eng.fleets[0]
sea_b = [s for s in at_sea(eng, 0) if s.squad == "B"]
ok(any(s.recall for s in sea_b), "return_only: at-sea B ships flagged recalled")
cargo_after = f0.cargo
ok(eng.events and any(e.get("k") == "signal" and e.get("flag") == "return"
                      for e in eng.events), "signal event tagged 'return'")
for _ in range(1200):                    # let them sail home and cycle
    eng.tick()
b_ships = [s for s in eng.ships.values() if s.fleet == 0 and s.squad == "B"]
ok(b_ships and all(not s.recall for s in b_ships), "recall clears at the circle")
ok(all(s.orders["role"] == "guard" for s in b_ships),
   "recalled ships collected the NEW standing orders in port")

# return_only refuses the classic push
eng2 = Engine([("R", Scripted([dict(orders={"B": dict(role="guard", rally=[5, 5])},
                                    signal=True)])), ("X", Idle())],
              seed=11, scenario={"signal_mode": "return_only"})
c0 = eng2.fleets[0].cargo
eng2.tick()
ok(eng2.fleets[0].cargo == c0, "return_only: signal:true is refused (no charge)")
ok(not any(e.get("k") == "signal" for e in eng2.events), "…and no signal event")

# --- preset mode: named flag applies instantly at sea + updates pending ---
presets = '{"Strike": {"B": {"role": "raid", "rally": [30, 30], "target_fleet": 1}}}'
eng3 = Engine([("P", Scripted([
    dict(orders={"B": dict(role="scout", rally=[60, 30], aggression=0)}),
    {},
    dict(signal={"hoist": "Strike"}),
])), ("X", Idle())], seed=11,
    scenario={"signal_mode": "preset", "signal_presets": presets})
for _ in range(210):
    eng3.tick()
sea_b3 = [s for s in at_sea(eng3, 0) if s.squad == "B"]
ok(sea_b3 and all(s.orders["role"] == "raid" for s in sea_b3),
   "preset: hoisted flag re-orders at-sea ships instantly")
ok(eng3.fleets[0].pending["B"]["role"] == "raid",
   "preset: flag also becomes the standing order")
ok("Strike" in eng3.scenario["signal_flags"], "scenario lists the flag names")
try:
    Engine([("A", Idle()), ("B", Idle())], seed=1,
           scenario={"signal_mode": "preset", "signal_presets": "not json"})
    ok(False, "invalid signal_presets JSON fails loudly")
except Exception:
    ok(True, "invalid signal_presets JSON fails loudly")

# --- custom mode: classic push still works; unknown-flag hoist refused ---
eng4 = Engine([("C", Scripted([
    dict(orders={"B": dict(role="scout", rally=[60, 30], aggression=0)}),
    {},
    dict(orders={"B": dict(role="guard", rally=[10, 10])}, signal=True),
])), ("X", Idle())], seed=11, scenario={"signal_mode": "custom"})
for _ in range(210):
    eng4.tick()
sea_b4 = [s for s in at_sea(eng4, 0) if s.squad == "B"]
ok(sea_b4 and all(s.orders["role"] == "guard" for s in sea_b4),
   "custom: signal:true pushes standing orders to sea")
ok(any(e.get("k") == "signal" and e.get("flag") == "orders-push"
       for e in eng4.events), "custom: event tagged orders-push")

# rules digest reflects the mode
r_ro = Engine([("A", Idle()), ("B", Idle())], seed=1,
              scenario={"signal_mode": "return_only"}).scenario["rules"]
ok("RETURN TO PORT" in r_ro and "NO instant orders-push" in r_ro,
   "rules digest documents return_only")
r_cu = eng4.scenario["rules"]
ok("push your CURRENT standing orders" in r_cu, "rules digest documents custom")

print("FAILURES:", fails)
sys.exit(1 if fails else 0)
