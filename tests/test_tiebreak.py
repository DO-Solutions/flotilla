"""Per-mode tie chains (#126): a tied game is decided by play quality —
Conquest by flagship kills / ships sunk / first flagship kill, Territories by
territories held / most recent capture, Score by cargo hauled / ships sunk /
net worth — and a chain that runs dry is an honest DRAW (winner null, trail
recorded), never a fleet-id coin flip. 895-895 landed in champions-cup-1."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "sim"))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                ".."))

from core import Engine                    # noqa: E402
from bots import BOTS                      # noqa: E402
from keelspring import runner as kr        # noqa: E402
import run_config                          # noqa: E402,F401

fails = 0


def ok(cond, msg):
    global fails
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        fails += 1


def mk(win, n=2, extra=None):
    players = [(f"P{i}", BOTS["merchant"]) for i in range(n)]
    scen = {"max_ticks": 200, "warmup": False, "win": win,
            "width": 64, "height": 36}
    scen.update(extra or {})
    return Engine(players, seed=7, scenario=scen)


# ---- Score mode: cargo hauled -> ships sunk -> net worth -> draw ----
e = mk("timed_score")
f0, f1 = e.fleets[0], e.fleets[1]
f0.bank, f0.kills = 10, 0
f1.bank, f1.kills = 8, 2                   # scores tie 10-10
r = e._final_result()
ok(r["winner"] == 0 and r["tiebreak"]["decided_by"] == "cargo hauled",
   f"score tie: cargo hauled decides ({r.get('tiebreak', {}).get('decided_by')})")

e = mk("timed_score")
f0, f1 = e.fleets[0], e.fleets[1]
f0.bank = f1.bank = 10
f0.kill_count, f1.kill_count = 1, 3
r = e._final_result()
ok(r["winner"] == 1 and r["tiebreak"]["decided_by"] == "ships sunk",
   "score tie + equal hauls: ships sunk decides")

e = mk("timed_score")
f0, f1 = e.fleets[0], e.fleets[1]
f0.bank = f1.bank = 10
f0.cargo, f1.cargo = 20, 50                # same ships, richer treasury
r = e._final_result()
ok(r["winner"] == 1 and r["tiebreak"]["decided_by"] == "fleet net worth",
   "score tie, equal hauls + sinks: net worth decides")

# fully symmetric -> DRAW: winner null, the trail is recorded, rank still
# lists everyone (a total order for display)
e = mk("timed_score")
e.fleets[0].bank = e.fleets[1].bank = 10
r = e._final_result()
ok(r["winner"] is None and r["tiebreak"].get("draw")
   and len(r["tiebreak"]["trail"]) == 3,
   "the chain run dry is an honest draw with the full trail recorded")
ok(sorted(r["rank"]) == [0, 1], "a drawn game still ranks everyone")

# no game rungs (engine default) -> the legacy lowest-id rule survives
e = mk("timed_score")
e.fleets[0].bank = e.fleets[1].bank = 10
e.tiebreak_rungs = lambda: []
r = e._final_result()
ok(r["winner"] == 0 and "tiebreak" not in r,
   "a game without a tie policy keeps the engine's legacy lowest-id rule")

# ---- Conquest: flagship kills -> ships sunk -> first flagship kill ----
e = mk("domination")
f0, f1 = e.fleets[0], e.fleets[1]
f0.kills = f1.kills = 150                  # equal kill POINTS (the score)
f0.flag_kills, f1.flag_kills = 1, 0
r = e._final_result()
ok(r["winner"] == 0 and r["tiebreak"]["decided_by"] == "flagship kills",
   "conquest cap-out: flagship kills decide")

e = mk("domination")
f0, f1 = e.fleets[0], e.fleets[1]
f0.kills = f1.kills = 150
f0.flag_kills = f1.flag_kills = 1
f0.kill_count = f1.kill_count = 2
f0.first_flag_kill_t, f1.first_flag_kill_t = 900, 400
r = e._final_result()
ok(r["winner"] == 1 and r["tiebreak"]["decided_by"] == "first flagship kill",
   "conquest: the EARLIER first flagship kill wins the last rung")

# ---- Territories: held at the bell -> most recent capture ----
e = mk("territory", extra={"territories": 5})
f0, f1 = e.fleets[0], e.fleets[1]
f0.territory = f1.territory = 40           # equal territory points
rids = sorted(e.region_owner)
e.region_owner[rids[0]] = 0
e.region_owner[rids[1]] = 0
e.region_owner[rids[2]] = 1
e.region_owner[rids[3]] = None             # unclaimed counts for no one
r = e._final_result()
ok(r["winner"] == 0 and r["tiebreak"]["decided_by"] == "territories held",
   "territory tie: territories held at the bell decide (unclaimed ignored)")

e = mk("territory", extra={"territories": 5})
f0, f1 = e.fleets[0], e.fleets[1]
f0.territory = f1.territory = 40
for rid in sorted(e.region_owner):         # equal held
    e.region_owner[rid] = None
e.region_owner[sorted(e.region_owner)[0]] = 0
e.region_owner[sorted(e.region_owner)[1]] = 1
f0.last_capture_t, f1.last_capture_t = 300, 500
r = e._final_result()
ok(r["winner"] == 1 and r["tiebreak"]["decided_by"] == "most recent capture",
   "territory tie + equal held: the most recent capture decides")

# ---- team match: rung values sum across the team ----
e = mk("timed_score", n=4)
for fid, tm in ((0, 0), (1, 0), (2, 1), (3, 1)):
    e.fleets[fid].team = tm
e.fleets[0].bank, e.fleets[1].bank = 5, 5      # team 0: 10 score, 10 hauled
e.fleets[2].bank, e.fleets[3].bank = 9, 0      # team 1: 10 score BUT
e.fleets[3].kills = 1                          # 9 hauled + 1 kill point
r = e._final_result()
ok(r.get("team_scores") == {0: 10, 1: 10}, "team scores tie as constructed")
ok(r["winner"] in (0, 1) and r["tiebreak"]["decided_by"] == "cargo hauled",
   "team tie: cargo hauled SUMMED by team decides for team 0")

# ---- the counters survive freeze -> thaw ----
e = mk("domination")
e.fleets[0].flag_kills = 2
e.fleets[0].first_flag_kill_t = 123
e.fleets[0].last_capture_t = 456
import json                                # noqa: E402
frozen = json.loads(json.dumps(e.freeze()))
e2 = Engine([("P0", BOTS["merchant"]), ("P1", BOTS["merchant"])],
            seed=7, scenario={"max_ticks": 200, "warmup": False,
                              "win": "domination",
                              "width": 64, "height": 36})
e2 = Engine.thaw(frozen, [("P0", BOTS["merchant"]), ("P1", BOTS["merchant"])])
ok(e2.fleets[0].flag_kills == 2 and e2.fleets[0].first_flag_kill_t == 123
   and e2.fleets[0].last_capture_t == 456,
   "tiebreak counters survive freeze -> JSON -> thaw")

# ---- a drawn game credits nobody in the series clinch math ----
ok(kr._clinched([{"winner": "A"}, {"winner": None}, {"winner": "A"},
                 {"winner": "A"}], 5),
   "3 wins + a draw in a best-of-5 clinches (draw credits nobody)")
ok(not kr._clinched([{"winner": None}, {"winner": None}], 3),
   "a series of draws never 'clinches' for the None player")

# ---- the rules text tells the admirals the chain (published policy) ----
for mode, needle in (("domination", "flagship kills"),
                     ("territory", "territories held at"),
                     ("timed_score", "cargo hauled, then")):
    e = mk(mode)
    ok("DRAW" in e.cfg["description"] and needle in e.cfg["description"],
       f"{mode}: the tie chain is published in the rules text")

print("FAILURES:", fails)
sys.exit(1 if fails else 0)
