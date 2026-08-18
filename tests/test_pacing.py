#!/usr/bin/env python3
"""Window-pacing stat (dash tournament stats): live-vs-catching-up per admiral.

Pipelined replays record t (applied-at) and ot (ordered-at) on every harvested
decision; the dash reduces (t-ot)/window per reply into a live rate and an
average windows-behind. The contract worth pinning:

  * lag of exactly ONE window = live (the reply landed inside its own window
    and decided the very next boundary — that IS the on-time cadence)
  * lockstep replays (pipeline_depth 0) and the synchronous t=0 openers carry
    no ot and must produce no pacing rows — the columns only exist for
    pipelined cups
  * behind = lag-1 windows, so a permanently-live admiral averages 0

node is TEST-ONLY (FLOTILLA_REQUIRE_NODE=1 in CI makes a missing node a
failure, not a skip).
"""
import json
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
from jsextract import extract                                # noqa: E402

DASH = os.path.join(ROOT, "dash", "tournament.html")
fails = 0


def ok(cond, msg):
    global fails
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        fails += 1


src = open(DASH, encoding="utf-8").read()
game_pace = extract(src, "gamePace")
ok(bool(game_pace), "gamePace is still extractable from the dash")

# ---- source anchors: the reducer folds pacing, the table renders it ----
ok("a.pace.behind += p.behind" in src,
   "buildStats folds per-game pacing into the per-admiral aggregate")
ok("live windows" in src and "avg behind" in src,
   "the stats table carries the two pacing columns")
ok("STATS[n].pace.n > 0" in src,
   "the columns are gated on pacing data actually existing (pipelined only)")

NODE = shutil.which("node")
if not NODE:
    if os.environ.get("FLOTILLA_REQUIRE_NODE") == "1":
        ok(False, "node is required (FLOTILLA_REQUIRE_NODE=1) but not installed")
    else:
        print("SKIPPED the JS checks — node is not installed")
    print(f"FAILURES: {fails}")
    sys.exit(1 if fails else 0)


def node_eval(js):
    r = subprocess.run([NODE, "-e", js], capture_output=True, text=True,
                       timeout=60)
    if r.returncode != 0:
        raise RuntimeError(r.stderr[:400])
    return json.loads(r.stdout)


W = 100
# fleet 0: opener (no ot) + live, live, 3-behind — 2/3 live, avg (0+0+2)/3
# fleet 1: opener + live, 6-behind (the depth+1 worst case) — 1/2 live
REPLAY = {
    "meta": {"config": {"window": W, "pipeline_depth": 5}},
    "decisions": [
        {"t": 0, "fleet": 0}, {"t": 0, "fleet": 1},
        {"t": 1 * W, "fleet": 0, "ot": 0},
        {"t": 2 * W, "fleet": 0, "ot": 1 * W},
        {"t": 6 * W, "fleet": 0, "ot": 3 * W},
        {"t": 1 * W, "fleet": 1, "ot": 0},
        {"t": 7 * W, "fleet": 1, "ot": 1 * W},
    ],
}
LOCKSTEP = {"meta": {"config": {"window": W, "pipeline_depth": 0}},
            "decisions": [{"t": 0, "fleet": 0}, {"t": W, "fleet": 0}]}

out = node_eval(
    game_pace + ";" + f"""
const rp = {json.dumps(REPLAY)};
console.log(JSON.stringify({{
  pace: gamePace(rp),
  lockstep: gamePace({json.dumps(LOCKSTEP)}),
}}))""")

p0, p1 = out["pace"]["0"], out["pace"]["1"]
ok(p0["n"] == 3 and p0["live"] == 2 and p0["behind"] == 2 and p0["worst"] == 2,
   f"fleet 0: 3 replies, 2 live, 2 windows behind total, worst 2 (got {p0})")
ok(p1["n"] == 2 and p1["live"] == 1 and p1["behind"] == 5 and p1["worst"] == 5,
   f"fleet 1: the depth-capped straggler counts 5 behind (got {p1})")
ok(out["lockstep"] is None,
   "a lockstep replay produces no pacing at all — the stat is pipelined-only")

print(f"FAILURES: {fails}")
sys.exit(1 if fails else 0)
