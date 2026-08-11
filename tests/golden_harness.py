#!/usr/bin/env python3
"""The engine-split invisibility gate (docs/ENGINE_SPLIT.md, Stage 0).

Golden replays: every config in tests/golden/configs/ runs through the REAL
entry point (sim/run_config.py, the same CLI the server and aux workers use)
and the bytes of every JSON it writes are hashed. The committed manifest is
the definition of "invisible": a restructuring stage passes only if it
reproduces every hash exactly. Byte-identity subsumes every subtler
compatibility claim — frame shapes, tiebreaks, iteration order, warnings
text, the settings stamp — so nothing needs a bespoke assertion.

  python3 tests/golden_harness.py generate   # double-runs everything, refuses
                                             # to write a manifest that isn't
                                             # provably deterministic, then
                                             # records hashes + a perf baseline
  python3 tests/golden_harness.py verify     # re-runs + compares (the test)

Perf: a fixed scripted match is timed (best of 3) at generate time and on
every verify. Verify always PRINTS the comparison; it only FAILS on
regression beyond the margin when FLOTILLA_PERF_GATE=1 — wall-clock on shared
CI runners is too noisy for a default-on gate, but the split PRs run gated on
the box that wrote the baseline.
"""
import hashlib
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CONFIGS = os.path.join(HERE, "golden", "configs")
MANIFEST = os.path.join(HERE, "golden", "manifest.json")
PERF_MARGIN = 0.05                      # 5% — the ENGINE_SPLIT.md floor
PERF_TICKS = 6000
PERF_CFG = {"mode": "match", "seed": 5, "bots": ["merchant", "corsair"],
            "scenario": {"width": 96, "height": 54, "max_ticks": PERF_TICKS,
                         "role_fallback": True, "warmup": False}}


def run_config(cfg, outdir):
    """One golden run through the real CLI. Returns {relpath: sha256} of every
    JSON written (replays, series.json — whatever the run produces)."""
    cfg = dict(cfg, outdir=outdir)
    cfgp = os.path.join(outdir, "_config.json")
    os.makedirs(outdir, exist_ok=True)
    with open(cfgp, "w") as fh:
        json.dump(cfg, fh)
    r = subprocess.run([sys.executable, "run_config.py", cfgp],
                       cwd=os.path.join(ROOT, "sim"),
                       capture_output=True, text=True, timeout=600)
    if r.returncode != 0:
        raise RuntimeError(f"run failed: {r.stderr[-400:]}")
    hashes = {}
    for dirpath, _dirs, files in os.walk(outdir):
        for fn in sorted(files):
            if not fn.endswith(".json") or fn == "_config.json":
                continue
            p = os.path.join(dirpath, fn)
            rel = os.path.relpath(p, outdir)
            with open(p, "rb") as fh:
                hashes[rel] = hashlib.sha256(fh.read()).hexdigest()
    if not hashes:
        raise RuntimeError(f"run produced no JSON output in {outdir}")
    return hashes


def perf_run():
    """Best-of-3 ticks per CHILD-CPU-second on the fixed perf match. CPU time
    (os.times() children delta), not wall-clock: the workload is CPU-bound and
    deterministic, and wall-clock on a shared box swings ±15% with whatever
    else is running — a 5%-margin gate needs a measure the scheduler can't
    touch. Best-of-3 damps the residual (cache warmth, interpreter startup)."""
    best = 0.0
    for _ in range(3):
        with tempfile.TemporaryDirectory(prefix="golden-perf-") as td:
            t0 = os.times()
            run_config(PERF_CFG, td)
            t1 = os.times()
            cpu = (t1.children_user + t1.children_system
                   - t0.children_user - t0.children_system)
        best = max(best, PERF_TICKS / cpu)
    return round(best, 1)


def all_configs():
    out = {}
    for fn in sorted(os.listdir(CONFIGS)):
        if fn.endswith(".json"):
            with open(os.path.join(CONFIGS, fn)) as fh:
                out[fn[:-5]] = json.load(fh)
    return out


def generate():
    manifest = {"configs": {}, "perf": {}}
    for name, cfg in all_configs().items():
        with tempfile.TemporaryDirectory(prefix="golden-a-") as ta, \
             tempfile.TemporaryDirectory(prefix="golden-b-") as tb:
            h1 = run_config(cfg, ta)
            h2 = run_config(cfg, tb)
        if h1 != h2:
            bad = [k for k in h1 if h1.get(k) != h2.get(k)]
            print(f"FATAL: {name} is NOT run-to-run deterministic ({bad}) — "
                  "no manifest written; fix the nondeterminism first.")
            return 1
        manifest["configs"][name] = h1
        print(f"  {name}: {len(h1)} file(s), deterministic ✓")
    manifest["perf"] = {"ticks_per_cpu_sec": perf_run(),
                        "host": os.uname().nodename}
    with open(MANIFEST, "w") as fh:
        json.dump(manifest, fh, indent=1, sort_keys=True)
    print(f"manifest written: {len(manifest['configs'])} configs, "
          f"perf {manifest['perf']['ticks_per_cpu_sec']} ticks/cpu-s")
    return 0


def verify():
    with open(MANIFEST) as fh:
        manifest = json.load(fh)
    fails = 0
    for name, want in manifest["configs"].items():
        cfg = all_configs().get(name)
        if cfg is None:
            print(f"FAIL {name}: config file missing")
            fails += 1
            continue
        with tempfile.TemporaryDirectory(prefix="golden-v-") as td:
            got = run_config(cfg, td)
        if got == want:
            print(f"PASS {name}: {len(got)} file(s) byte-identical")
        else:
            fails += 1
            for k in sorted(set(want) | set(got)):
                if want.get(k) != got.get(k):
                    print(f"FAIL {name}: {k} "
                          f"{'MISSING' if k not in got else 'hash changed'}")
    base = manifest["perf"]["ticks_per_cpu_sec"]
    now = perf_run()
    delta = (now - base) / base
    line = (f"perf: {now} ticks/cpu-s vs baseline {base} "
            f"({delta:+.1%}, margin ±{PERF_MARGIN:.0%}, "
            f"baseline host {manifest['perf']['host']})")
    if os.environ.get("FLOTILLA_PERF_GATE") == "1" and delta < -PERF_MARGIN:
        print("FAIL " + line)
        fails += 1
    else:
        print(("WARN " if delta < -PERF_MARGIN else "PASS ") + line)
    print("FAILURES:", fails)
    return 1 if fails else 0


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "verify"
    sys.exit(generate() if mode == "generate" else verify())
