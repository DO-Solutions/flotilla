#!/usr/bin/env python3
"""Balance + determinism harness: N-seed FFA with seat rotation, summary stats.

Exit-criteria checks (PLAN.md P0):
  - determinism: seed 1 run twice -> identical replay hash
  - no bot winrate > 70%
  - elimination rate < 50% (the timer should usually matter)
  - snowball index: how often the tick-1500 leader is the final winner
"""
import argparse
import hashlib
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from core import Engine            # noqa: E402
from bots import BOTS              # noqa: E402

LINEUP = ["merchant", "corsair", "admiralty", "turtle"]


def run_one(seed, rotate):
    names = LINEUP[rotate:] + LINEUP[:rotate]
    # role_fallback is REQUIRED for scripted bots: ships obey only conn programs
    # by default, so without it every bot idled, every score was 0, nobody was
    # eliminated and the "winner" was just seat order — winrate_cap and elim_rate
    # printed PASS while asserting nothing at all.
    eng = Engine([(n, BOTS[n]) for n in names], seed=seed,
                 scenario={"role_fallback": True})
    result = eng.run()
    # leader at tick 1500 (frame index) for snowball index
    lead1500 = None
    for fr in eng.frames:
        if fr["t"] >= 1500:
            lead1500 = max(fr["f"], key=lambda r: (r[4], -r[0]))[0]
            break
    return eng, result, result["names"].get(lead1500), names


def replay_hash(eng, result):
    return hashlib.sha256(
        json.dumps(eng.replay(result), sort_keys=True, separators=(",", ":"))
        .encode()).hexdigest()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=24)
    args = ap.parse_args()

    # determinism gate
    e1, r1, _, _ = run_one(1, 0)
    e2, r2, _, _ = run_one(1, 0)
    det = replay_hash(e1, r1) == replay_hash(e2, r2)
    print(f"determinism(seed=1 twice): {'PASS' if det else 'FAIL'}")

    wins = {n: 0 for n in LINEUP}
    elims = 0
    snowball_hits = 0
    ticks_all = []
    scores_sum = {n: 0 for n in LINEUP}
    for i in range(args.n):
        seed = 100 + i
        eng, result, lead1500_name, names = run_one(seed, i % 4)
        wname = result["names"][result["winner"]]
        wins[wname] += 1
        if len(result["alive"]) < len(names):
            elims += 1
        if lead1500_name == wname:
            snowball_hits += 1
        ticks_all.append(result["ticks"])
        for fid, sc in result["scores"].items():
            scores_sum[result["names"][fid]] += sc
        print(f"seed={seed} rot={i % 4} winner={wname:9s} ticks={result['ticks']:5d} "
              f"scores={ {result['names'][k]: v for k, v in sorted(result['scores'].items())} }")

    n = args.n
    print("\n== summary ==")
    for name in LINEUP:
        print(f"{name:9s} winrate={wins[name] / n:5.0%}  avg_score={scores_sum[name] / n:7.1f}")
    print(f"elim_rate={elims / n:.0%}  snowball={snowball_hits / n:.0%}  "
          f"avg_ticks={sum(ticks_all) / n:.0f}")
    worst = max(wins.values()) / n
    print(f"exit: determinism={'PASS' if det else 'FAIL'} "
          f"winrate_cap={'PASS' if worst <= 0.70 else 'FAIL'}({worst:.0%}) "
          f"elim_rate={'PASS' if elims / n < 0.5 else 'FAIL'}")


if __name__ == "__main__":
    main()
