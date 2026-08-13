#!/usr/bin/env python3
"""Calibrate the cost estimator against the library's ACTUAL usage.

The pre-flight estimate assumed 4000 in / 800-4000 out tokens per call for
every model. Measured across 250 library games (2026-08-13) the real numbers
were ~7-18k in and 1.2-15k out, wildly model-dependent — GLM emitted 15k
thinking tokens per call while GPT-5.6 emitted 1.4k, so one constant
under-estimated 63% of games while over-estimating others 2x. This script
measures per-model per-call tokens from the replays' decision usage records
and writes assets/cost-calibration.json, which server._estimate_cost and the
dash estimator both read (per-model values, calibrated defaults for models
never seen). p75 keeps the estimate deliberately high-side without the 2-3x
misses.

Usage (run against a live flagship; auth = "user:pass" or a file holding it):
  python3 scripts/calibrate_costs.py --base https://<flagship> --auth <creds>
Writes assets/cost-calibration.json in the repo. Re-run whenever the model
mix shifts or after a big tournament lands; commit the result. Debrief and
feedback calls are not in the decision records, so actuals run ~1 call/game
low — p75 more than covers the gap.
"""
import argparse
import base64
import gzip
import io
import json
import os
import time
import urllib.request

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(HERE, "assets", "cost-calibration.json")


def _get(base, auth, path):
    req = urllib.request.Request(base.rstrip("/") + "/" + path.lstrip("/"),
                                 headers={"Accept-Encoding": "gzip",
                                          "Authorization": "Basic " + auth})
    with urllib.request.urlopen(req, timeout=180) as r:
        raw = r.read()
        if r.headers.get("Content-Encoding") == "gzip":
            raw = gzip.GzipFile(fileobj=io.BytesIO(raw)).read()
        return raw


def harvest(base, auth):
    """[(model, think, tin_per_call, tout_per_call)] for every LLM admiral of
    every library game, from the replays' decision usage records."""
    idx = json.loads(_get(base, auth, "index.json"))
    files = [g["file"] for s in idx.get("series", []) for g in s.get("games", [])]
    files += [m["file"] for m in idx.get("matches", [])]
    samples = []
    for i, path in enumerate(files):
        try:
            rp = json.loads(_get(base, auth, path))
        except Exception as e:
            print(f"skip {path}: {type(e).__name__}", flush=True)
            continue
        spec = {b["label"]: b for b in (rp.get("run") or {}).get("players", [])
                if isinstance(b, dict) and "label" in b}
        names = (rp.get("result") or {}).get("names") or {}
        agg = {}
        for d in rp.get("decisions", []):
            u = d.get("u")
            if isinstance(u, dict):
                a = agg.setdefault(d["fleet"], [0, 0, 0])
                a[0] += 1
                a[1] += u.get("tin", 0) or 0
                a[2] += u.get("tout", 0) or 0
        for fid, (calls, tin, tout) in agg.items():
            sp = spec.get(names.get(str(fid), ""), {})
            if calls and sp.get("model"):
                samples.append((sp["model"], sp.get("think", True) is not False,
                                tin / calls, tout / calls))
        if i % 25 == 0:
            print(f"{i + 1}/{len(files)} replays read", flush=True)
    return samples


def _p75(vals):
    vals = sorted(vals)
    return int(vals[int(0.75 * (len(vals) - 1))]) if vals else None


def calibration(samples):
    """The calibration dict from harvested samples — per-model p75 tokens per
    call, plus all-model defaults for models the library has never seen."""
    per = {}
    for model, think, tin, tout in samples:
        d = per.setdefault(model, {"tin": [], "tout_think": [], "tout_flat": []})
        d["tin"].append(tin)
        d["tout_think" if think else "tout_flat"].append(tout)
    models = {}
    for model, d in sorted(per.items()):
        m = {"n": len(d["tin"]), "tin": _p75(d["tin"])}
        for k in ("tout_think", "tout_flat"):
            v = _p75(d[k])
            if v is not None:
                m[k] = v
        models[model] = m
    alltin = [s[2] for s in samples]
    tthink = [s[3] for s in samples if s[1]]
    tflat = [s[3] for s in samples if not s[1]]
    return {"generated": time.strftime("%Y-%m-%d"),
            "admiral_games": len(samples),
            "percentile": 75,
            "default": {"tin": _p75(alltin) or 4000,
                        "tout_think": _p75(tthink) or 4000,
                        "tout_flat": _p75(tflat) or 800},
            "models": models}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", required=True, help="flagship base URL")
    ap.add_argument("--auth", required=True,
                    help="basic-auth user:pass, or a path to a file holding it")
    a = ap.parse_args()
    creds = a.auth
    if os.path.isfile(os.path.expanduser(creds)):
        creds = open(os.path.expanduser(creds)).read().strip()
    auth = base64.b64encode(creds.encode()).decode()
    cal = calibration(harvest(a.base, auth))
    with open(OUT, "w") as fh:
        json.dump(cal, fh, indent=1)
        fh.write("\n")
    print(f"{OUT}: {len(cal['models'])} models from "
          f"{cal['admiral_games']} admiral-games")
    for m, d in cal["models"].items():
        print(f"  {m:30s} n={d['n']:4d} tin={d['tin']:6d} "
              f"tout_think={d.get('tout_think', '—')}")


if __name__ == "__main__":
    main()
