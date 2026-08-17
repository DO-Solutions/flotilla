#!/usr/bin/env python3
"""Config envelope: an unknown top-level section must be REFUSED, not ignored.

Knobs inside the four schema bags were always validated — config_schema.resolve
raises "unknown config key 'x'" for a typo. A whole unknown SECTION had no such
check: merged_scenario() reads exactly scenario/admirals/series/tournament and
drops everything else without a word, so a config that reads correctly can run
as something else entirely.

That is not hypothetical. A cup was launched with pipeline_depth inside a
top-level "pacing" block — its real SCHEMA section, but not an envelope section
— so it was dropped, the tournament ran unpipelined, and two days of a supposed
A/B compared two identical configurations before anyone checked a replay.

The regression case at the bottom is that exact config.
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
TMP = tempfile.mkdtemp(prefix="flotilla-cfgsec-")
os.environ["FLOTILLA_LIBRARY"] = TMP

import run_config                                            # noqa: E402
from keelspring.runner import validate_config, merged_scenario  # noqa: E402
import server                                                # noqa: E402

fails = 0


def ok(cond, msg):
    global fails
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        fails += 1


GOOD = {"mode": "match", "seed": 1, "name": "ok",
        "bots": ["merchant", "corsair"],
        "scenario": {"width": 64, "height": 36},
        "admirals": {"timeout_s": 60},
        "series": {}, "tournament": {}}

ok(validate_config(GOOD) == [],
   "a well-formed config passes clean")
ok(validate_config(dict(GOOD, outdir="/tmp/x", ack_cost=True,
                        executor="auxiliary", aux_size="s-1vcpu-1gb",
                        public=True)) == [],
   "the server's own submit options (outdir/ack_cost/executor/aux_size/public) "
   "are accepted")
ok(validate_config(dict(GOOD, participants=["a", "b"])) == [],
   "participants is accepted (the tournament spelling of bots)")

# ---- the trap: a real SCHEMA section used as an envelope section ----
bad = dict(GOOD, pacing={"pipeline_depth": 5})
problems = validate_config(bad)
ok(len(problems) == 1, f"one complaint for one bad section (got {problems})")
ok("pacing" in problems[0] and "scenario" in problems[0],
   f"the message names the offending section AND where its knobs belong: "
   f"{problems[0]!r}")
ok("ignored" in problems[0].lower(),
   "the message says the section would be IGNORED — the whole point is that it "
   "looks applied and is not")

for sec in ("world", "economy", "combat", "scenario_extra"):
    p = validate_config(dict(GOOD, **{sec: {}}))
    ok(len(p) == 1, f"a top-level {sec!r} section is refused")

ok(validate_config(dict(GOOD, participnats=["a"]))[0].startswith("unknown"),
   "a plain typo is reported as an unknown key with the valid list")
ok(validate_config("not a dict") == ["config must be a JSON object"],
   "a non-object config is refused rather than crashing")

# ---- the drop is real: prove merged_scenario ignores the bad section ----
m = merged_scenario(bad)
ok("pipeline_depth" not in m,
   "merged_scenario() genuinely drops the misplaced section — this is the "
   "silence the validator now breaks")
m2 = merged_scenario(dict(GOOD, scenario=dict(GOOD["scenario"],
                                              pipeline_depth=5)))
ok(m2.get("pipeline_depth") == 5,
   "and the SAME knob inside `scenario` is picked up, which is the fix")

# ---- the server refuses it at submit, before provisioning anything ----
try:
    server.submit_run(dict(bad))
    ok(False, "submit_run accepted a config with an unknown section")
except ValueError as e:
    ok("pacing" in str(e),
       f"submit_run rejects it with a ValueError -> HTTP 400 ({str(e)[:80]!r})")
except Exception as e:                       # any other failure is the wrong one
    ok(False, f"submit_run raised the wrong error: {type(e).__name__}: {e}")

# ---- and the CLI refuses it too, so an aux worker cannot run it either ----
d = tempfile.mkdtemp(prefix="cfgsec-cli-")
p = os.path.join(d, "cfg.json")
json.dump(dict(bad, outdir=d, scenario=dict(bad["scenario"], max_ticks=100)),
          open(p, "w"))
r = subprocess.run([sys.executable, os.path.join(ROOT, "sim", "run_config.py"), p],
                   capture_output=True, text=True, timeout=300)
ok(r.returncode != 0, f"the CLI refuses it too (rc {r.returncode})")
ok("pacing" in (r.stdout + r.stderr),
   "the CLI says which section is wrong")

# ---- REGRESSION: the exact cup config that lost two days ----
cup = {"mode": "tournament", "seed": 8300, "name": "regression",
       "participants": [{"model": "qwen3.5-397b-a17b", "label": "Qwen3.5"},
                        {"model": "kimi-k3", "label": "KimiK3"}],
       "scenario": {"win": "territory", "width": 192, "height": 108},
       "admirals": {"timeout_s": 480},
       "series": {"vary_seeds": True},
       "tournament": {"format": "round_robin", "games_per_match": 5},
       "pacing": {"pipeline_depth": 5},        # <- the mistake, verbatim
       "ack_cost": True, "executor": "auxiliary"}
probs = validate_config(cup)
ok(len(probs) == 1 and "pacing" in probs[0],
   "the cup config that silently ran unpipelined for two days is now refused "
   "outright")
ok("pipeline_depth" not in merged_scenario(cup),
   "…and the reason is confirmed: the knob never reached the engine")

print(f"FAILURES: {fails}")
sys.exit(1 if fails else 0)
