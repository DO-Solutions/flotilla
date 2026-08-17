#!/usr/bin/env python3
"""Every schema knob must survive the journey from config JSON to the engine.

The failure this guards against is the expensive kind: a knob that is spelled
correctly, accepted without complaint, and then quietly not applied. That
already happened once with a whole section (see test_config_sections.py); this
walks the ENTIRE schema, knob by knob, and proves each one arrives.

Three legs, because a knob can be lost at any of them:
  1. envelope   — the four config sections are actually read
  2. resolve    — a non-default value survives config_schema.resolve()
  3. the stamp  — it reaches meta.config in the replay, which is the only
                  record anyone can check AFTER a run (and the only reason the
                  pipeline_depth mistake was findable at all)
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

import config_schema as cs                                   # noqa: E402
from keelspring.runner import merged_scenario                # noqa: E402

fails = 0


def ok(cond, msg):
    global fails
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        fails += 1


def probe_value(spec):
    """A LEGAL value that differs from the default, so 'it arrived' can't be
    confused with 'it defaulted'. None = no safe distinct value exists."""
    d, t = spec["d"], spec.get("t")
    if t == "bool":
        return not d
    if t == "enum":
        alt = [o for o in spec.get("opts", []) if o != d]
        return alt[0] if alt else None
    if t in ("int", "float"):
        lo, hi = spec.get("lo"), spec.get("hi")
        for cand in (d + 1, d - 1, lo, hi):
            if cand is None or cand == d:
                continue
            if lo is not None and cand < lo:
                continue
            if hi is not None and cand > hi:
                continue
            return int(cand) if t == "int" else float(cand)
        return None
    if t == "str":
        return None if d else "probe-value"
    return None


# ---- leg 1: all four envelope sections are read ----
base = {"mode": "match", "seed": 1, "bots": ["merchant", "corsair"]}
for sec, knob, val in (("scenario", "width", 77),
                       ("admirals", "timeout_s", 123),
                       ("series", "games", 4),
                       ("tournament", "games_per_match", 3)):
    m = merged_scenario(dict(base, **{sec: {knob: val}}))
    ok(m.get(knob) == val,
       f"the `{sec}` section is read (its {knob} arrives as {val})")

# a section the envelope does NOT read must not appear — the whole bug
ok("pipeline_depth" not in merged_scenario(dict(base,
                                                pacing={"pipeline_depth": 5})),
   "a section outside the envelope is dropped (now rejected up front, but the "
   "drop itself is still the reason it must be)")

# ---- leg 2: every knob in the schema survives resolve() with a real value ----
skipped, checked, lost = [], 0, []
for sec_name, knobs in cs.SCHEMA.items():
    for knob, spec in knobs.items():
        v = probe_value(spec)
        if v is None:
            skipped.append(f"{sec_name}.{knob}")
            continue
        merged = merged_scenario(dict(base, scenario={knob: v}))
        try:
            r = cs.resolve(merged)
        except Exception as e:
            lost.append(f"{sec_name}.{knob}: resolve raised {type(e).__name__}: {e}")
            continue
        checked += 1
        if r.get(knob) != v:
            lost.append(f"{sec_name}.{knob}: set {v!r}, resolved {r.get(knob)!r}")

ok(not lost,
   f"every probed knob survives resolve() — {checked} checked"
   + ("" if not lost else "\n    LOST: " + "\n    LOST: ".join(lost[:12])))
print(f"     ({len(skipped)} knobs skipped: free-form strings with no safe "
      f"distinct probe value)")
ok(checked > 60,
   f"the walk actually covered the schema, not a corner of it ({checked} knobs)")

# ---- leg 3: an overridden knob reaches meta.config in the replay ----
# This is the leg that matters after the fact: meta.config is the only record
# of what a finished run ACTUALLY used.
out = tempfile.mkdtemp(prefix="ft-cfgcov-")
cfg = {"mode": "match", "seed": 7, "outdir": out,
       "bots": ["merchant", "corsair"],
       "scenario": {"width": 48, "height": 30, "max_ticks": 120,
                    "role_fallback": True, "warmup": False,
                    # one knob from each schema section, all off-default
                    "pipeline_depth": 3, "ship_cost": 21, "gather_period": 6},
       "admirals": {"timeout_s": 99}}
json.dump(cfg, open(os.path.join(out, "cfg.json"), "w"))
r = subprocess.run([sys.executable, os.path.join(ROOT, "sim", "run_config.py"),
                    os.path.join(out, "cfg.json")],
                   capture_output=True, text=True, timeout=600)
ok(r.returncode == 0,
   f"the probe match runs (rc {r.returncode}): {(r.stdout + r.stderr)[-200:]}")
if r.returncode == 0:
    mc = json.load(open(os.path.join(out, "match.json")))["meta"]["config"]
    for knob, want in (("pipeline_depth", 3), ("ship_cost", 21),
                       ("gather_period", 6), ("timeout_s", 99),
                       ("width", 48)):
        ok(mc.get(knob) == want,
           f"meta.config records the ACTUAL {knob} ({want}), not the default "
           f"(got {mc.get(knob)!r})")

print(f"FAILURES: {fails}")
sys.exit(1 if fails else 0)
