#!/usr/bin/env python3
"""Swiss tournaments: pair on the standings, keep everyone playing.

Swiss is the third DYNAMIC format (with single_elim and, trivially, resume):
its bracket is discovered as it plays rather than laid out up front. The thing
that makes it delicate is RESUME — a restored run replays the pairing logic for
rounds it has already played, so pairing must depend only on rounds BEFORE the
one being paired. Read the final table instead and round 2 gets re-paired
differently from the round 2 that actually happened, contradicting the record
the run just restored.

The pairing is a pure function, so most of this is fast unit work; one real
4-bot tournament at the end proves it actually drives the runner.
"""
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "sim"))
sys.path.insert(0, ROOT)

from keelspring.runner import swiss_pairs, swiss_standings   # noqa: E402

fails = 0


def ok(cond, msg):
    global fails
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        fails += 1


def matchup(rnd, a, b, winner, games=1):
    return {"round": rnd, "players": [a, b], "winner": winner,
            "games": [{"winner": winner, "scores": {a: 1, b: 0}}] * games}


N4 = ["Alfa", "Bravo", "Charlie", "Delta"]

# ---- round 1: no record yet, so pair straight down the (name-ordered) table
r1 = swiss_pairs(N4, [], 1)
ok(r1 == [["Alfa", "Bravo"], ["Charlie", "Delta"]],
   f"round 1 pairs down an empty table, deterministically (got {r1})")
ok(swiss_pairs(N4, [], 1) == r1,
   "the same input pairs the same way every time (no rng anywhere)")

# ---- round 2: winners meet winners, losers meet losers
after_r1 = [matchup(1, "Alfa", "Bravo", "Alfa"),
            matchup(1, "Charlie", "Delta", "Charlie")]
r2 = swiss_pairs(N4, after_r1, 2)
ok(sorted(r2[0]) == ["Alfa", "Charlie"] and sorted(r2[1]) == ["Bravo", "Delta"],
   f"round 2 pairs the two winners together and the two losers together "
   f"(got {r2})")

# ---- the resume property: pairing round 2 must not see round 2's own results
after_r2 = after_r1 + [matchup(2, "Alfa", "Charlie", "Charlie"),
                       matchup(2, "Bravo", "Delta", "Bravo")]
ok(swiss_pairs(N4, after_r2, 2) == r2,
   "re-pairing round 2 with round 2 ALREADY RECORDED yields the identical "
   "pairing — this is what makes a resumed tournament rejoin its own bracket "
   "instead of forking a new one")
ok(swiss_standings(N4, after_r2, 2)["Alfa"]["series_wins"] == 1,
   "standings 'before round 2' count only round 1 (Alfa 1 win, not 1-1)")

# ---- rematch avoidance
r3 = swiss_pairs(N4, after_r2, 3)
pairs_seen = {frozenset(m["players"]) for m in after_r2}
fresh = [p for p in r3 if frozenset(p) not in pairs_seen]
ok(len(fresh) == len(r3),
   f"round 3 avoids every pairing already played (got {r3})")

# ---- rematches ARE allowed rather than not playing at all
tiny = ["Alfa", "Bravo"]
forced = swiss_pairs(tiny, [matchup(1, "Alfa", "Bravo", "Alfa")], 2)
ok(forced == [["Alfa", "Bravo"]],
   "with no fresh opponent left, a rematch beats an empty round")

# ---- odd field: a bye each round, and it rotates
N5 = N4 + ["Echo"]
b1 = swiss_pairs(N5, [], 1)
sat1 = set(N5) - {p for pair in b1 for p in pair}
ok(len(b1) == 2 and len(sat1) == 1,
   f"an odd field plays floor(n/2) matchups and sits exactly one out (got {b1})")
rec1 = [matchup(1, *b1[0], b1[0][0]), matchup(1, *b1[1], b1[1][0])]
b2 = swiss_pairs(N5, rec1, 2)
sat2 = set(N5) - {p for pair in b2 for p in pair}
ok(sat1 != sat2,
   f"the bye moves to someone else next round (round 1 {sat1}, round 2 {sat2})")
ok(len(b2) == 2, "the round still plays two matchups")

# ---- a bigger field behaves
N8 = ["P%d" % i for i in range(8)]
e1 = swiss_pairs(N8, [], 1)
ok(len(e1) == 4 and len({p for pair in e1 for p in pair}) == 8,
   "8 players pair into 4 matchups with nobody left out")

# ---- end to end: a real Swiss tournament through the runner
cfg = {"mode": "tournament", "seed": 21,
       "participants": ["merchant", "corsair", "admiralty", "turtle"],
       "scenario": {"width": 64, "height": 36, "max_ticks": 600,
                    "role_fallback": True, "warmup": False},
       "series": {},
       "tournament": {"format": "swiss", "rounds": 2, "games_per_match": 1,
                      "memo_policy": "none", "parallel": 1, "stagger_s": 0,
                      "full_series": True}}
out = tempfile.mkdtemp(prefix="ft-swiss-")
cfgp = os.path.join(out, "cfg.json")
json.dump(dict(cfg, outdir=out), open(cfgp, "w"))
r = subprocess.run([sys.executable, os.path.join(ROOT, "sim", "run_config.py"), cfgp],
                   capture_output=True, text=True, timeout=900)
ok(r.returncode == 0,
   f"a swiss tournament runs to completion (rc {r.returncode}): "
   f"{(r.stdout + r.stderr)[-300:]}")
if r.returncode == 0:
    tj = json.load(open(os.path.join(out, "tournament.json")))
    ok(len(tj["matchups"]) == 4,
       f"2 rounds x 2 matchups = 4 played (got {len(tj['matchups'])})")
    ok(tj.get("champion") in cfg["participants"],
       f"a champion is crowned from the field (got {tj.get('champion')})")
    ok({m["round"] for m in tj["matchups"]} == {1, 2},
       "matchups are recorded under both rounds")
    ok(all(st["games"] > 0 for st in tj["standings"].values()),
       "NOBODY is eliminated — every admiral played in a swiss bracket")
    # the champion should be the one with the most series wins
    best = max(tj["standings"].items(),
               key=lambda kv: (kv[1]["series_wins"], kv[1]["wins"],
                               kv[1]["score"]))[0]
    ok(tj["champion"] == best,
       f"the champion is the top of the table (champion {tj['champion']}, "
       f"table leader {best})")

print(f"FAILURES: {fails}")
sys.exit(1 if fails else 0)
