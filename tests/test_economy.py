#!/usr/bin/env python3
"""Budget-lock relief batch: roles-off default, signal queueing + warnings,
scuttle, passive income, 90s timeout default."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "sim"))
from config_schema import resolve        # noqa: E402
from core import Engine, WINDOW, cheb    # noqa: E402

fails = 0


def ok(cond, msg):
    global fails
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        fails += 1


class Scripted:
    name = "scripted"

    def __init__(self, queue):
        self.queue = list(queue)
        self.summaries = []

    def decide(self, summary, rng):
        self.summaries.append(summary)
        return self.queue.pop(0) if self.queue else {}


class Idle(Scripted):
    name = "idle"

    def __init__(self):
        super().__init__([])


ok(resolve({})["timeout_s"] == 90, "decision timeout default is 90s")
ok(resolve({})["role_fallback"] is False, "role autopilot defaults OFF")

# --- roles off: ordered-but-unprogrammed ships stay in port ---
bot = Scripted([{"orders": {"A": {"role": "forage", "rally": [60, 30]}}}])
eng = Engine([("R", bot), ("X", Idle())], seed=3, max_ticks=WINDOW * 3)
eng.run()
own = [s for s in eng.ships.values() if s.fleet == 0]
f0 = eng.fleets[0]
ok(own and all(cheb(s.x, s.y, f0.hx, f0.hy) <= eng.hr for s in own),
   "roles off: ships never leave the harbor circle")
ok(any("role autopilot disabled" in s.intent for s in own),
   "idle intent names the reason")
ok("ROLE AUTOPILOT DISABLED" in eng.scenario["rules"], "rules digest says so")

r_on = Engine([("A", Idle()), ("B", Idle())], seed=1,
              scenario={"role_fallback": True,
                        "roles_allowed": "forage,scout"}).scenario["rules"]
ok("forage,scout" in r_on.replace(" ", ""), "digest lists the allowed-role subset")
eng_sub = Engine([("A", Idle()), ("B", Idle())], seed=1,
                 scenario={"role_fallback": True, "roles_allowed": "forage,scout"})
ok(eng_sub._clean_order(eng_sub.fleets[0], {"role": "raid"}) is None,
   "disallowed role rejected")
ok(eng_sub._clean_order(eng_sub.fleets[0], {"role": "forage"}) is not None,
   "allowed role accepted")
try:
    Engine([("A", Idle()), ("B", Idle())], seed=1,
           scenario={"roles_allowed": "forage,warlock"})
    ok(False, "unknown role in roles_allowed fails loudly")
except ValueError:
    ok(True, "unknown role in roles_allowed fails loudly")

# --- signal queue on insufficient funds + warning + auto-fire ---
bot2 = Scripted([{"signal": {"return": "all"}}, {}, {}])
eng2 = Engine([("Q", bot2), ("X", Idle())], seed=3)
eng2.fleets[0].cargo = 2                       # cost is 5: cannot afford
eng2.tick()
ok(eng2.fleets[0].queued_signal is not None, "unaffordable hoist queues")
ok(not any(e["k"] == "signal" for e in eng2.events), "…and does not fire yet")
for _ in range(WINDOW + 1):
    eng2.tick()                                # through window 1's summary
warned = [s for s in bot2.summaries[1:] if any(
    "QUEUED" in w for w in s["you"].get("warnings", []))]
ok(warned, "admiral is warned about the queued signal every window")
eng2.fleets[0].cargo = 50
eng2.tick()
ok(any(e["k"] == "signal" for e in eng2.events),
   "queued signal fires the moment funds allow")
ok(eng2.fleets[0].queued_signal is None, "queue clears after firing")

# cancel clears the queue
bot3 = Scripted([{"signal": {"return": "all"}}, {"signal": {"cancel": True}}])
eng3 = Engine([("C", bot3), ("X", Idle())], seed=3)
eng3.fleets[0].cargo = 2
for _ in range(WINDOW + 1):
    eng3.tick()
eng3.fleets[0].cargo = 2                       # deposits may have refilled it
ok(eng3.fleets[0].queued_signal is None, "cancel clears the queued signal")

# build drop warning
bot4 = Scripted([{"build": [{"preset": "trader", "squad": "A"}]}, {}])
eng4 = Engine([("B", bot4), ("X", Idle())], seed=3)
eng4.fleets[0].cargo = 3                       # ship costs 15
for _ in range(WINDOW + 1):
    eng4.tick()
ok(any("DROPPED — insufficient funds" in w
       for s in bot4.summaries for w in s["you"].get("warnings", [])),
   "unaffordable build produces a warning")

# --- scuttle: anywhere, +value, cargo wrecks, sink event tagged ---
eng5 = Engine([("S", Idle()), ("X", Idle())], seed=3)
f5 = eng5.fleets[0]
sh = next(s for s in eng5.ships.values() if s.fleet == 0)
sh.x, sh.y = 60, 30                            # far at sea
sh.cargo = 7
f5.cargo = 0
nodes_before = len(eng5.nodes)
eng5._apply_actions(f5, {"scuttle": [sh.id]})
ok(sh.id not in eng5.ships, "scuttled ship is gone")
ok(f5.cargo == eng5.cfg["scuttle_value"], "treasury recovered")
ok(len(eng5.nodes) == nodes_before + 1, "her cargo went down as a wreck")
ok(any(e["k"] == "sink" and e.get("cause") == "scuttle" for e in eng5.events),
   "sink event tagged scuttle")
ok("SCUTTLE" in eng5.scenario["rules"], "rules digest documents scuttle")

eng6 = Engine([("S", Idle()), ("X", Idle())], seed=3, scenario={"scuttle": False})
sh6 = next(s for s in eng6.ships.values() if s.fleet == 0)
eng6._apply_actions(eng6.fleets[0], {"scuttle": [sh6.id]})
ok(sh6.id in eng6.ships, "scuttle=off ignores the action")

# --- passive income ---
eng7 = Engine([("I", Idle()), ("X", Idle())], seed=3,
              scenario={"income_amount": 3, "income_period": 50})
start = eng7.fleets[0].cargo
for _ in range(101):
    eng7.tick()
ok(eng7.fleets[0].cargo == start + 6, "passive income pays on schedule")
ok("passive income" in eng7.scenario["rules"], "rules digest documents income")
eng8 = Engine([("I", Idle()), ("X", Idle())], seed=3)
s8 = eng8.fleets[0].cargo
for _ in range(101):
    eng8.tick()
ok(eng8.fleets[0].cargo == s8, "income off by default")

print("FAILURES:", fails)
sys.exit(1 if fails else 0)
