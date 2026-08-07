#!/usr/bin/env python3
"""helm language: parser, semantics, budget, engine integration, determinism."""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "sim"))
import helm                              # noqa: E402
from core import Engine, WINDOW          # noqa: E402

fails = 0


def ok(cond, msg):
    global fails
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        fails += 1


def err_of(text):
    try:
        helm.compile_program(text)
        return None
    except helm.HelmError as e:
        return str(e)


# ---- parser ----
p = helm.compile_program("""
# forager
mem trips = 0
when self.hull_pct < orders.retreat: helm.home()
when self.cargo >= self.hold_cap: helm.home()
when enemy.found and enemy.stronger and enemy.dist < 8: helm.flee()
when node.found and node.dist == 0: helm.gather()
when node.found: helm.goto(node.x, node.y)
default: helm.goto(orders.rally_x, orders.rally_y)
""")
ok(p is not None and len(p.stmts) == 6, "reference forager compiles")

ok("line 1" in (err_of("when self.hull_pct <: helm.home()") or ""),
   "syntax error carries line number")
e = err_of("when bogus.sensor > 1: helm.home()")
ok(e and "unknown name" in e, f"unknown sensor rejected ({e})")
e = err_of("when self.x > 1: helm.warp(3)")
ok(e and "unknown action" in e, "unknown action rejected")
e = err_of("when mem.k > 1: helm.hold()")
ok(e and "not declared" in e, "undeclared mem read rejected")
e = err_of("set k = 1\nwhen self.x > 0: helm.hold()")
ok(e and "undeclared" in e, "undeclared set rejected")
e = err_of("mem a = 1")
ok(e and "no when" in e, "program without when rejected")
e = err_of("\n".join(["# pad"] * 100))
ok(e and "64 lines" in e, "line cap enforced")
e = err_of("when self.x > 0: helm.goto(1)")
ok(e and "takes 2 args" in e, "action arity checked")

# ---- semantics ----
prog = helm.compile_program("""
mem state = 0
set state = state_placeholder
when mem.state == 1: helm.home()
when self.cargo > 3: helm.gather()
default: helm.hold()
""".replace("state_placeholder", "mem.state + 0"))
m = prog.init_mem()
out = prog.run({"self.cargo": 5}, m)
ok(out[0] == "gather", "top-down: first true when fires")
m["state"] = 1
out = prog.run({"self.cargo": 5}, m)
ok(out[0] == "home", "earlier when wins when true")

prog2 = helm.compile_program("""
mem n = 0
set n = mem.n + 1
when mem.n >= 3: helm.home()
default: helm.hold()
""")
m2 = prog2.init_mem()
r = [prog2.run({}, m2)[0] for _ in range(4)]
ok(r == ["hold", "hold", "home", "home"], f"mem persists across ticks ({r})")

prog3 = helm.compile_program(
    "when dist(0, 0, self.x, self.y) > 5 and not (self.cargo == 0): "
    "helm.goto(min(self.x, 10), max(self.y, 2))\ndefault: helm.hold()")
out = prog3.run({"self.x": 30, "self.y": 1, "self.cargo": 4}, {})
ok(out[0] == "goto" and out[1] == [10.0, 2.0],
   "funcs + bool ops + args evaluate (cheb dist)")
ok(prog3.run({"self.x": 3, "self.y": 1, "self.cargo": 4}, {})[0] == "hold",
   "condition false falls through to default")

div = helm.compile_program("when 5 / self.cargo > 2: helm.home()\ndefault: helm.hold()")
ok(div.run({"self.cargo": 0}, {})[0] == "hold", "division by zero yields 0, no crash")

# budget: self-referential churn over many statements
big = "mem a = 1\n" + "\n".join(
    f"set a = mem.a + {i} * (1 + 2 * 3) - abs(0 - 1)" for i in range(50)) \
    + "\ndefault: helm.hold()"
bp = helm.compile_program(big)
try:
    for _ in range(10):
        bp.run({}, bp.init_mem())
    budget_ok = True                     # 50 sets fit the budget
except helm.HelmError:
    budget_ok = False
ok(budget_ok, "reasonable program fits the tick budget")

# ---- sensor tables agree with the engine ----
class Idle:
    name = "idle"

    def decide(self, summary, rng):
        return {}


eng = Engine([("A", Idle()), ("B", Idle())], seed=5)
ship = next(s for s in eng.ships.values() if s.fleet == 0)
sen, _ = eng._program_sensors(ship)
missing = set(helm.SENSORS) - set(sen)
extra = set(sen) - set(helm.SENSORS)
ok(not missing and not extra,
   f"engine sensors == helm.SENSORS (missing {missing or '∅'}, extra {extra or '∅'})")

# ---- engine integration ----
class Programmer:
    name = "programmer"

    def __init__(self, queue):
        self.queue = list(queue)

    def decide(self, summary, rng):
        return self.queue.pop(0) if self.queue else {}


GOTO = "when 1 == 1: helm.goto(40, 20)"
eng2 = Engine([("P", Programmer([{"programs": {"A": GOTO}}])), ("X", Idle())], seed=5)
for _ in range(WINDOW + 60):
    eng2.tick()
a_ships = [s for s in eng2.ships.values() if s.fleet == 0 and s.squad == "A"]
ok(a_ships and all(s.program is not None for s in a_ships),
   "delivered program rides the squadron's ships")
ok(any("program L1: goto(40,20)" in s.intent for s in a_ships),
   "intent records program line + action")
moved = [s for s in a_ships if (s.x, s.y) != (eng2.fleets[0].hx, eng2.fleets[0].hy)]
ok(moved and all(abs(s.x - 40) + abs(s.y - 20) <
                 abs(eng2.fleets[0].hx - 40) + abs(eng2.fleets[0].hy - 20)
                 for s in moved), "programmed ships actually sail to the target")
pev = [e for e in eng2.events if e["k"] == "program"]
ok(pev and pev[0]["text"] == GOTO, "program event carries the full text")

# rejection: bad program leaves prior state intact
eng3 = Engine([("P", Programmer([
    {"programs": {"A": GOTO}},
    {"programs": {"A": "when garbage!!!"}},
])), ("X", Idle())], seed=5)
for _ in range(WINDOW * 2 + 10):
    eng3.tick()
rej = [e for e in eng3.events if e["k"] == "program_rejected"]
ok(len(rej) == 1 and "line 1" in rej[0]["reason"], "bad program rejected with reason")
ok(eng3.fleets[0].pending_programs["A"].text == GOTO,
   "rejected program leaves the previous one standing")

# clearing: empty string reverts ships once they're back in the circle — at-sea
# ships keep running the old code until recalled (command physics, not a bug)
eng4 = Engine([("P", Programmer([
    {"programs": {"A": GOTO}},
    {"programs": {"A": ""}, "signal": {"return": "all"}},
])), ("X", Idle())], seed=5)
for _ in range(WINDOW * 2 + 600):
    eng4.tick()
a4 = [s for s in eng4.ships.values() if s.fleet == 0 and s.squad == "A"]
ok(a4 and all(s.program is None for s in a4),
   "cleared program reverts ships after recall brings them home")

# programs=off: delivery ignored
eng5 = Engine([("P", Programmer([{"programs": {"A": GOTO}}])), ("X", Idle())],
              seed=5, scenario={"programs": False})
for _ in range(WINDOW + 10):
    eng5.tick()
ok(eng5.fleets[0].pending_programs == {}, "programs=off ignores delivery")
ok("DISABLED" in eng5.scenario["rules"].split("SHIP PROGRAMS")[0] or
   "programs are DISABLED" in eng5.scenario["rules"], "rules digest says disabled")

# determinism with programs in play
def play(seed):
    e = Engine([("P", Programmer([{"programs": {"A": GOTO}}])), ("X", Idle())],
               seed=seed, max_ticks=WINDOW * 3)
    r = e.run()
    return json.dumps(e.replay(r), sort_keys=True)


ok(play(9) == play(9), "byte-determinism holds with programs")

print("FAILURES:", fails)
sys.exit(1 if fails else 0)
