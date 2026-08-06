#!/usr/bin/env python3
"""run_config: dict player specs, per-player overrides, always-debrief series."""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "sim"))
import run_config as rc                  # noqa: E402
from llm import LLMAdmiral               # noqa: E402
from bots import BOTS                    # noqa: E402

fails = 0


def ok(cond, msg):
    global fails
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        fails += 1


adm = rc.section_defaults("admirals")

b = rc.make_bot({"model": "glm-5.2", "label": "Glimmer", "temperature": 0.7,
                 "timeout_s": 90, "prompt": "Favor trade over war."}, adm)
ok(isinstance(b, LLMAdmiral) and b.name == "Glimmer", "dict spec builds a labeled LLM")
ok(b.temperature == 0.7 and b.timeout == 90, "per-player overrides applied")
ok(b.max_tokens == adm["max_tokens"], "unset fields inherit the admirals section")
ok("Favor trade" in b.system, "prompt reaches the system message")

ok(rc.make_bot({"model": "corsair"}, adm) is BOTS["corsair"],
   "dict spec resolves scripted bots")
ok(rc.make_bot("llm:kimi-k3:K", adm).name == "K", "string specs still work")

ok(rc.spec_name({"model": "kimi-k3"}) == "kimi-k3", "spec_name: dict w/o label")
ok(rc.spec_name({"model": "kimi-k3", "label": "Kay"}) == "Kay", "spec_name: label wins")
ok(rc.spec_name("llm:glm-5.2:G") == "G", "spec_name: string label")
ok(rc.spec_name("merchant") == "merchant", "spec_name: scripted")

# always-debrief: scripted-only series still writes the memos key (empty — no LLMs),
# and the series runs the debrief path on the FINAL game without error
with tempfile.TemporaryDirectory() as td:
    named = [("m", BOTS["merchant"]), ("t", BOTS["turtle"])]
    ser = {**rc.section_defaults("series"), "games": 1}
    ser["memos"] = True
    rows = rc.run_series(named, seed=5, scenario={"max_ticks": 600}, ser=ser, outdir=td)
    s = json.load(open(os.path.join(td, "series.json")))
    ok("memos" in s, "series.json carries final memos key")
    g1 = json.load(open(os.path.join(td, "g1.json")))
    ok("memos" in g1, "final game replay carries its debrief block")

print("FAILURES:", fails)
sys.exit(1 if fails else 0)
