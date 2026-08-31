#!/usr/bin/env python3
"""Boundary configs + determinism, scripted bots only (born as the 2026-08-31
wringer). Each config runs TWICE; the replay hashes must match byte-for-byte —
a nondeterministic engine invalidates every A/B the lab runs. Covers the
corners the golden suite does not: 8-way FFA, 4-team play, a 384x216 board,
zero-island and max-island worlds, flooded economies, flex designs, minimum
windows, and maximum clock jitter."""
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SIM = os.path.join(ROOT, "sim")
BOTS8 = ["merchant", "corsair", "admiralty", "turtle"] * 2

CASES = {
  "8way-max-islands": {
    "mode": "match", "seed": 42, "bots": BOTS8,
    "scenario": {"width": 192, "height": 108, "island_coverage": 20,
                 "max_ticks": 2000, "role_fallback": True, "warmup": False}},
  "domination-quick": {
    "mode": "match", "seed": 43, "bots": BOTS8[:4],
    "scenario": {"win": "domination", "domination_cap": 3000,
                 "width": 96, "height": 54, "max_ticks": 4000,
                 "role_fallback": True, "warmup": False}},
  "territory-extreme": {
    "mode": "match", "seed": 44, "bots": BOTS8[:4],
    "scenario": {"win": "territory", "territories": 48,
                 "territory_capture_ticks": 0, "width": 192, "height": 108,
                 "max_ticks": 1500, "role_fallback": True, "warmup": False,
                 "territory_tick": 10}},
  "teams-4x2-flagmove": {
    "mode": "match", "seed": 45, "bots": BOTS8,
    "scenario": {"teams": "0,1|2,3|4,5|6,7", "flag_move": True,
                 "flag_speed": 6, "width": 128, "height": 72,
                 "max_ticks": 1500, "role_fallback": True, "warmup": False}},
  "minimums": {
    "mode": "match", "seed": 46, "bots": ["merchant", "corsair"],
    "scenario": {"width": 64, "height": 36, "island_coverage": 0,
                 "scuttle_value": 0, "design_points": 6, "program_chars": 200,
                 "max_ticks": 500, "role_fallback": True, "warmup": False,
                 "window": 20}},
  "economy-flood": {
    "mode": "match", "seed": 47, "bots": BOTS8[:4],
    "scenario": {"income_amount": 100, "income_period": 10,
                 "flag_salvage": 2000, "width": 96, "height": 54,
                 "max_ticks": 1500, "role_fallback": True, "warmup": False}},
  "flex-designs": {
    "mode": "match", "seed": 48, "bots": BOTS8[:4],
    "scenario": {"flex_design": True, "design_points_max": 40,
                 "width": 96, "height": 54, "max_ticks": 1500,
                 "role_fallback": True, "warmup": False}},
  "huge-board": {
    "mode": "match", "seed": 49, "bots": BOTS8[:4],
    "scenario": {"width": 384, "height": 216, "island_coverage": 10,
                 "max_ticks": 1000, "role_fallback": True, "warmup": False}},
  "long-jitter": {
    "mode": "match", "seed": 50, "bots": ["merchant", "corsair"],
    "scenario": {"width": 64, "height": 36, "max_ticks": 6000,
                 "role_fallback": True, "warmup": False,
                 "clock_jitter": 18000, "window": 1000}},
}

fails = 0


def run_once(cfg, outdir):
    cfgp = os.path.join(outdir, "_config.json")
    os.makedirs(outdir, exist_ok=True)
    with open(cfgp, "w") as fh:
        json.dump(dict(cfg, outdir=outdir), fh)
    t0 = time.time()
    r = subprocess.run([sys.executable, "run_config.py", cfgp], cwd=SIM,
                       capture_output=True, text=True, timeout=900)
    dt = time.time() - t0
    if r.returncode != 0:
        return None, dt, (r.stderr or r.stdout)[-300:]
    hashes = {}
    for dp, _d, files in os.walk(outdir):
        for fn in sorted(files):
            if fn.endswith(".json") and fn != "_config.json":
                with open(os.path.join(dp, fn), "rb") as fh:
                    hashes[fn] = hashlib.sha256(fh.read()).hexdigest()
    return hashes, dt, None


for name, cfg in CASES.items():
    with tempfile.TemporaryDirectory() as ta, tempfile.TemporaryDirectory() as tb:
        h1, dt1, e1 = run_once(cfg, ta)
        if e1 is not None:
            print(f"FAIL {name}: run errored: {e1}")
            fails += 1
            continue
        # sanity: a replay landed, parses, has frames + a result
        rep = [fn for fn in os.listdir(ta) if fn.endswith(".json")
               and fn != "_config.json"]
        okr = False
        for fn in rep:
            try:
                d = json.load(open(os.path.join(ta, fn)))
                if d.get("frames") and d.get("result"):
                    okr = True
                    tick = d["result"].get("ticks")
            except Exception:
                pass
        if not okr:
            print(f"FAIL {name}: no parseable replay with frames+result")
            fails += 1
            continue
        h2, dt2, e2 = run_once(cfg, tb)
        if e2 is not None:
            print(f"FAIL {name}: second run errored: {e2}")
            fails += 1
        elif h1 != h2:
            print(f"FAIL {name}: NONDETERMINISTIC (same seed, different bytes)")
            fails += 1
        else:
            print(f"PASS {name}: deterministic, ticks={tick}, "
                  f"{dt1:.1f}s/{dt2:.1f}s")
print("FAILURES:", fails)
sys.exit(1 if fails else 0)
