"""Perfect information about your own ships (the bounty-g5 lesson,
2026-08-28): an admiral watched trawlers 'gather' 5 cells off a node for
4000 ticks and concluded helm.goto was broken. Three guarantees now hold:
1. gather's intent says WHY nothing is happening (off-island / fished dry /
   hold full) instead of a bare 'gather';
2. every own-ship summary row carries `doing` (the live intent string the
   spectator viewer always had) and `idle_s` once the ship idles at sea;
3. a ship that has neither moved nor gained cargo for 30s at sea raises ONE
   fleet warning naming the ship and its task — re-armed only after it
   moves again."""
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


class Prog:
    name = "prog"

    def __init__(self, q):
        self.q = list(q)
        self.summaries = []

    def decide(self, summary, rng):
        self.summaries.append(summary)
        return self.q.pop(0) if self.q else {}


SCN = {"width": 48, "height": 32, "max_ticks": 6000, "warmup": False,
       "flag_move": False, "win": "timed_score"}

# --- 1+2+3: the exact bounty-g5 shape — gather guard fires short of the node
TRAP = """when dist(self.x, self.y, 30, 10) <= 8: helm.gather()
default: helm.goto(30, 10)"""

bot = Prog([{"programs": {"A": TRAP}}])
eng = Engine([("K", Prog([{"programs": {"A": TRAP}}])), ("X", Idle())],
             seed=9, scenario=SCN)
kbot = eng.fleets[0].bot
for _ in range(700):
    eng.tick()

traw = [s for s in eng.ships.values() if s.fleet == 0 and s.preset == "trawler"]
ok(all(s.x == 22 and s.y == 10 for s in traw),
   f"trawlers froze at the guard radius ({[(s.x, s.y) for s in traw]})")
ok(all("NOT on an island" in s.intent for s in traw),
   f"gather intent says WHY it holds ({traw[0].intent!r})")

summ = eng.summary_for(eng.fleets[0])
rows = {r["id"]: r for r in summ["you"]["ships"]}
t0 = traw[0]
ok(rows[t0.id].get("doing") == t0.intent,
   "own-ship row carries `doing` = the live intent")
ok(rows[t0.id].get("idle_s", 0) >= 30,
   f"idle_s visible and growing ({rows[t0.id].get('idle_s')})")

warned = [w for s2 in kbot.summaries for w in s2.get("you", {}).get("warnings", [])]
# warnings ride summary_for's `warnings` — find where they landed
if not warned:
    warned = [w for s2 in kbot.summaries for w in s2.get("warnings", [])]
warned += summ.get("warnings", [])
stuck_warns = [w for w in warned if "has not moved" in w]
ok(any(f"#{t0.id}" in w and "gather" in w for w in stuck_warns),
   f"stuck warning names the ship and its task ({stuck_warns[:1]})")
ok(len([w for w in stuck_warns if f"#{t0.id}" in w]) == 1,
   f"warning fires once per episode, not every window "
   f"({len([w for w in stuck_warns if f'#{t0.id}' in w])})")

# --- happy path: gather ON a node still reads as loading + no warning
node = next(iter(eng.nodes.values()))
GOOD = f"""when self.full: helm.home()
when dist(self.x, self.y, {node.x}, {node.y}) == 0: helm.gather()
default: helm.goto({node.x}, {node.y})"""
eng2 = Engine([("G", Prog([{"programs": {"A": GOOD}}])), ("X", Idle())],
              seed=9, scenario=SCN)
gbot = eng2.fleets[0].bot
for _ in range(450):                      # node still stocked in this span
    eng2.tick()
traw2 = [s for s in eng2.ships.values() if s.fleet == 0 and s.preset == "trawler"]
ok(any(s.cargo > 0 or s.trip_gathered > 0 for s in traw2) or
   eng2.fleets[0].bank > 0,
   f"on-node gather actually loads (cargo={[s.cargo for s in traw2]}, "
   f"bank={eng2.fleets[0].bank})")
gw = [w for s2 in gbot.summaries for w in s2.get("you", {}).get("warnings", [])]
gw += [w for s2 in gbot.summaries for w in s2.get("warnings", [])]
ok(not any("has not moved" in w for w in gw),
   "a productive gatherer never trips the stuck warning")

# --- run on: the node drains dry and the holder is called out truthfully
for _ in range(250):
    eng2.tick()
traw2 = [s for s in eng2.ships.values() if s.fleet == 0 and s.preset == "trawler"]
ok(any("fished dry" in s.intent for s in traw2),
   f"holding on a drained node reads as 'fished dry' ({traw2[0].intent!r})")
gw2 = [w for s2 in gbot.summaries for w in s2.get("you", {}).get("warnings", [])]
gw2 += [w for s2 in gbot.summaries for w in s2.get("warnings", [])]
ok(any("has not moved" in w and "fished dry" in w for w in gw2),
   "the dry-node holder eventually warns, with the reason in the task text")

print(f"\n{len(FAILS)} failures")
sys.exit(1 if FAILS else 0)
