"""Tournament pause/resume: the two-level checkpoint (tournament state +
per-lane series checkpoints) must survive any pause source and resume on a
fresh process with nothing lost — and a pause that CANNOT be honored must be
refused, never answered with rc 75 and an empty disk (champions-cup-1 and -2
both died to exactly that)."""
import io
import contextlib
import json
import os
import subprocess
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "sim"))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                ".."))
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

fails = 0


def ok(cond, msg):
    global fails
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        fails += 1


def run_cfg(cfg, outdir, resume=False, timeout=600):
    cfgpath = os.path.join(outdir, "cfg.json")
    if not resume:
        json.dump(dict(cfg, outdir=outdir), open(cfgpath, "w"))
    argv = [sys.executable, os.path.join(HERE, "sim", "run_config.py")] + \
        (["--resume", outdir] if resume else [cfgpath])
    return subprocess.run(argv, capture_output=True, text=True,
                          timeout=timeout)


BASE = {"mode": "tournament", "seed": 21,
        "participants": ["merchant", "corsair", "admiralty"],
        "scenario": {"width": 64, "height": 36, "max_ticks": 900,
                     "role_fallback": True, "warmup": False},
        "series": {},
        "tournament": {"format": "round_robin", "players_per_match": 2,
                       "games_per_match": 2, "memo_policy": "none",
                       "parallel": 1, "stagger_s": 0, "full_series": True}}


def standings_of(outdir):
    return json.load(open(os.path.join(outdir, "tournament.json")))["standings"]


# ---- control: one uninterrupted run to compare everything against ----
ctl = tempfile.mkdtemp(prefix="ft-tp-ctl-")
r = run_cfg(BASE, ctl)
assert r.returncode == 0, r.stdout[-500:] + r.stderr[-500:]
CTL = standings_of(ctl)
CTL_TJ = json.load(open(os.path.join(ctl, "tournament.json")))

# ---- 1. pause BETWEEN matchups (flag pre-planted): freeze with zero loss,
# resume finishes with standings identical to the control ----
a = tempfile.mkdtemp(prefix="ft-tp-a-")
os.makedirs(a, exist_ok=True)
open(os.path.join(a, "pause.flag"), "w").write("1")
r = run_cfg(BASE, a)
ok(r.returncode == 75, f"pre-planted flag freezes the tournament (rc {r.returncode})")
ck = json.load(open(os.path.join(a, "checkpoint.json")))
ok(ck["kind"] == "tournament" and ck["completed"] == []
   and ck["paused_lanes"] == [],
   "between-matchup checkpoint: kind=tournament, nothing in flight")
ok(not os.path.exists(os.path.join(a, "pause.flag")), "flag consumed")
r = run_cfg(BASE, a, resume=True)
ok(r.returncode == 0, f"resume completes (rc {r.returncode}): "
   + (r.stdout[-200:] if r.returncode else ""))
ok(standings_of(a) == CTL,
   "resumed standings identical to the uninterrupted control")
ok(not os.path.exists(os.path.join(a, "checkpoint.json")),
   "tournament checkpoint consumed on completion")

# ---- 2. pause MID-matchup (sequential): the lane freezes, the tournament
# embeds its checkpoint, resume replays nothing ----
b = tempfile.mkdtemp(prefix="ft-tp-b-")
os.makedirs(b, exist_ok=True)
cfgpath = os.path.join(b, "cfg.json")
json.dump(dict(BASE, outdir=b), open(cfgpath, "w"))
p = subprocess.Popen([sys.executable,
                      os.path.join(HERE, "sim", "run_config.py"), cfgpath],
                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                     text=True)
# plant the flag once the FIRST matchup is recorded — the pause then lands
# inside a later matchup, mid-game
deadline = time.time() + 300
planted = False
while time.time() < deadline:
    tj = os.path.join(b, "tournament.json")
    if os.path.isfile(tj):
        try:
            if json.load(open(tj)).get("matchups"):
                open(os.path.join(b, "pause.flag"), "w").write("1")
                planted = True
                break
        except ValueError:
            pass
    time.sleep(0.2)
out, _ = p.communicate(timeout=300)
ok(planted, "test scaffolding: flag planted after matchup 1 recorded")
ok(p.returncode == 75, f"mid-matchup pause exits 75 (rc {p.returncode}): "
   + out[-200:])
ck = json.load(open(os.path.join(b, "checkpoint.json")))
ok(ck["completed"] and len(ck["paused_lanes"]) == 1,
   f"checkpoint: {len(ck['completed'])} matchups done, 1 lane frozen")
lane = ck["paused_lanes"][0]
ok(lane.get("checkpoint") and lane["checkpoint"].get("kind") == "series",
   "the lane's own series checkpoint is EMBEDDED in the tournament's")
# freeze the bytes of every existing replay: resume must not touch them
pre = {}
for root, _d, files in os.walk(b):
    for fn in files:
        if fn.startswith("g") and fn.endswith(".json"):
            fp = os.path.join(root, fn)
            pre[fp] = os.path.getmtime(fp)
r = run_cfg(BASE, b, resume=True)
ok(r.returncode == 0, f"mid-matchup resume completes (rc {r.returncode})")
replayed = [fp for fp, m in pre.items()
            if os.path.getmtime(fp) != m and "checkpoint" not in fp
            and lane["dir"] not in fp]
ok(not replayed, f"no completed game outside the frozen lane was replayed "
   f"({len(replayed)} touched)")
ok(standings_of(b) == CTL, "mid-matchup resumed standings match the control")

# ---- 3. PARALLEL lanes: one pause request freezes every live lane, each
# with its own durable state; resume completes them all ----
PAR = json.loads(json.dumps(BASE))
PAR["tournament"]["parallel"] = 3
c = tempfile.mkdtemp(prefix="ft-tp-c-")
os.makedirs(c, exist_ok=True)
cfgpath = os.path.join(c, "cfg.json")
json.dump(dict(PAR, outdir=c), open(cfgpath, "w"))
p = subprocess.Popen([sys.executable,
                      os.path.join(HERE, "sim", "run_config.py"), cfgpath],
                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                     text=True)
deadline = time.time() + 300
planted = False
while time.time() < deadline:      # any lane's first replay = all lanes live
    if any(fn.startswith("g") and fn.endswith(".json")
           for _r, _d, fs in os.walk(c) for fn in fs):
        open(os.path.join(c, "pause.flag"), "w").write("1")
        planted = True
        break
    time.sleep(0.2)
out, _ = p.communicate(timeout=300)
ok(planted, "test scaffolding: flag planted while parallel lanes were live")
ok(p.returncode == 75, f"parallel pause exits 75 (rc {p.returncode}): "
   + out[-200:])
ck = json.load(open(os.path.join(c, "checkpoint.json")))
ok(len(ck["paused_lanes"]) >= 1, f"{len(ck['paused_lanes'])} lanes frozen")
ok(all(L.get("checkpoint") or L.get("rows") is not None
       for L in ck["paused_lanes"]),
   "every frozen lane carries a checkpoint (or at worst its game rows)")
r = run_cfg(PAR, c, resume=True)
ok(r.returncode == 0, f"parallel resume completes (rc {r.returncode})")
sc = standings_of(c)
ok(sum(s["series_wins"] for s in sc.values()) == 3
   and json.load(open(os.path.join(c, "tournament.json"))).get("champion"),
   "parallel resume: all 3 matchups decided, champion crowned")

# ---- 4. a lane whose checkpoint was LOST resumes from its game rows,
# losing at most the one in-flight game ----
d = tempfile.mkdtemp(prefix="ft-tp-d-")
os.makedirs(d, exist_ok=True)
# reuse scenario 2's freeze shape: build it, then strip the lane checkpoint
cfgpath = os.path.join(d, "cfg.json")
json.dump(dict(BASE, outdir=d), open(cfgpath, "w"))
p = subprocess.Popen([sys.executable,
                      os.path.join(HERE, "sim", "run_config.py"), cfgpath],
                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                     text=True)
deadline = time.time() + 300
while time.time() < deadline:
    tj = os.path.join(d, "tournament.json")
    if os.path.isfile(tj):
        try:
            if json.load(open(tj)).get("matchups"):
                open(os.path.join(d, "pause.flag"), "w").write("1")
                break
        except ValueError:
            pass
    time.sleep(0.2)
p.communicate(timeout=300)
ckp = os.path.join(d, "checkpoint.json")
ck = json.load(open(ckp))
assert ck["paused_lanes"], "scaffolding: need a frozen lane"
lane = ck["paused_lanes"][0]
lane["checkpoint"] = None                       # the hard-kill scenario
json.dump(ck, open(ckp, "w"), separators=(",", ":"))
# the on-disk lane checkpoint must not rescue it — this simulates a fresh
# worker that only received the (stripped) tournament checkpoint
lck = os.path.join(d, lane["dir"], "checkpoint.json")
if os.path.isfile(lck):
    os.remove(lck)
r = run_cfg(BASE, d, resume=True)
ok(r.returncode == 0, f"rows-only lane resume completes (rc {r.returncode}): "
   + (r.stdout[-200:] if r.returncode else ""))
ok("lane_checkpoint_lost" in r.stdout,
   "the degraded path announces itself (lane_checkpoint_lost)")
sd = standings_of(d)
ok(sum(s["series_wins"] for s in sd.values()) == 3,
   "rows-only resume still decides all 3 matchups")

# ---- 5. fail-safe: a pause the runner cannot checkpoint is REFUSED and the
# run continues — never rc 75 with nothing on disk ----
sys.path.insert(0, os.path.join(HERE, "sim"))
from keelspring import runner as kr                      # noqa: E402
import run_config as rc                                  # noqa: E402,F401

e = tempfile.mkdtemp(prefix="ft-tp-e-")
calls = {"n": 0}


def flaky_pause():
    calls["n"] += 1
    return 3 <= calls["n"] <= 5        # a transient pause request


def boom(outdir, payload):
    raise OSError(28, "No space left on device (simulated)")


keep = kr.write_checkpoint
kr.write_checkpoint = boom
try:
    buf = io.StringIO()
    from bots import BOTS                                # noqa: E402
    named = [("m", BOTS["merchant"]), ("c", BOTS["corsair"])]
    ser = {**rc.section_defaults("series"), "games": 1, "memos": False,
           "sim_feedback": False}
    with contextlib.redirect_stdout(buf):
        rows = kr.run_series(named, seed=7,
                             scenario={"max_ticks": 400, "warmup": False},
                             ser=ser, outdir=e, pause_check=flaky_pause,
                             pause_mode="raise")
    ok(len(rows) == 1 and rows[0].get("winner"),
       "unhonorable pause refused: the game still completed")
    ok('"pause_refused"' in buf.getvalue(),
       "the refusal is announced (pause_refused)")
    ok(not os.path.exists(os.path.join(e, "checkpoint.json")),
       "no half-checkpoint left behind")
finally:
    kr.write_checkpoint = keep

# ---- 6. single_elim: pause between rounds, resume rebuilds the bracket from
# records (winners re-derived, nothing replayed) ----
EL = json.loads(json.dumps(BASE))
EL["participants"] = ["merchant", "corsair", "admiralty", "turtle"]
EL["tournament"].update({"format": "single_elim", "games_per_match": 1})
f = tempfile.mkdtemp(prefix="ft-tp-f-")
os.makedirs(f, exist_ok=True)
cfgpath = os.path.join(f, "cfg.json")
json.dump(dict(EL, outdir=f), open(cfgpath, "w"))
p = subprocess.Popen([sys.executable,
                      os.path.join(HERE, "sim", "run_config.py"), cfgpath],
                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                     text=True)
deadline = time.time() + 300
while time.time() < deadline:      # freeze once round 1 is fully recorded
    tj = os.path.join(f, "tournament.json")
    if os.path.isfile(tj):
        try:
            if len(json.load(open(tj)).get("matchups", [])) >= 2:
                open(os.path.join(f, "pause.flag"), "w").write("1")
                break
        except ValueError:
            pass
    time.sleep(0.2)
out, _ = p.communicate(timeout=300)
ok(p.returncode == 75, f"elim pause exits 75 (rc {p.returncode})")
r = run_cfg(EL, f, resume=True)
ok(r.returncode == 0, f"elim resume completes (rc {r.returncode})")
ftj = json.load(open(os.path.join(f, "tournament.json")))
ok(ftj.get("champion") in EL["participants"]
   and len(ftj["matchups"]) == 3,
   f"elim bracket complete after resume (3 matchups, champion "
   f"{ftj.get('champion')})")

print("FAILURES:", fails)
sys.exit(1 if fails else 0)
