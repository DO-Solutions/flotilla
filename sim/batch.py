#!/usr/bin/env python3
"""Parallel match batch runner.

Match generation is I/O-bound (~15s CPU per 19-min LLM match; ~50MB RSS) — so a
subprocess pool on one box is all the parallelism we need. Rate limits, not compute,
are the constraint: llm.py backs off on 429/5xx. Run big batches in a throwaway
container on a worker box, not on the orchestrating host.
"""
import argparse
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

HERE = os.path.dirname(os.path.abspath(__file__))


def parse_seeds(spec):
    out = []
    for part in spec.split(","):
        part = part.strip()
        if "-" in part:
            a, b = part.split("-")
            out.extend(range(int(a), int(b) + 1))
        elif part:
            out.append(int(part))
    return out


def run_one(bots, seed, ticks, outdir, idx):
    out = os.path.join(outdir, f"m{idx:03d}_s{seed}.json")
    t0 = time.time()
    p = subprocess.run([sys.executable, os.path.join(HERE, "run_match.py"),
                        "--bots", bots, "--seed", str(seed), "--ticks", str(ticks),
                        "--out", out], capture_output=True, text=True)
    line = p.stdout.strip().splitlines()[-1] if p.stdout.strip() else ""
    try:
        res = json.loads(line)
    except Exception:
        res = {"seed": seed, "error": (p.stderr or "no output")[-300:]}
    res["wall_s"] = round(time.time() - t0)
    res["file"] = out
    print(json.dumps(res), flush=True)
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bots", required=True, help="same seat syntax as run_match.py")
    ap.add_argument("--seeds", required=True, help="e.g. '200-219' or '1,5,9'")
    ap.add_argument("--ticks", type=int, default=6000)
    ap.add_argument("--parallel", type=int, default=6)
    ap.add_argument("--outdir", default="batch-out")
    args = ap.parse_args()

    os.makedirs(args.outdir, exist_ok=True)
    seeds = parse_seeds(args.seeds)
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=args.parallel) as ex:
        futs = [ex.submit(run_one, args.bots, s, args.ticks, args.outdir, i)
                for i, s in enumerate(seeds)]
        results = [f.result() for f in futs]

    ok = [r for r in results if "error" not in r]
    wins = {}
    for r in ok:
        w = r["names"][str(r["winner"])] if isinstance(list(r["names"])[0], str) \
            else r["names"][r["winner"]]
        wins[w] = wins.get(w, 0) + 1
    print(json.dumps({
        "matches": len(results), "errors": len(results) - len(ok),
        "wins": wins, "wall_total_s": round(time.time() - t0),
        "wall_avg_s": round(sum(r["wall_s"] for r in ok) / max(1, len(ok))),
    }), flush=True)


if __name__ == "__main__":
    main()
