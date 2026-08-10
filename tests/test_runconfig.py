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

# --- memo_history integration: the series log accumulates with per-game
# headers, a FAILED debrief appends nothing, and memo_history=False keeps
# the old latest-memo-only behavior (wringer pass 3) ---
class ScriptedMemos(LLMAdmiral):
    def __init__(self):
        super().__init__("stub-model", label="Scripty")
        self.game_no = 0

    def decide(self, summary, rng):
        return {"thoughts": "sailing"}

    def plan(self, summary):
        return {}

    def debrief(self, digest):
        self.game_no += 1
        if self.game_no == 3:
            return dict(memo="", tin=0, tout=0, ms=0, cost=0.0, err="boom")
        m = f"memo-{self.game_no}"
        self.notes = m                       # what the real debrief does
        return dict(memo=m, tin=0, tout=0, ms=0, cost=0.0, err=None)


with tempfile.TemporaryDirectory() as td:
    a = ScriptedMemos()
    named = [("Scripty", a), ("m", BOTS["merchant"])]
    ser = {**rc.section_defaults("series"), "games": 3, "memos": True,
           "sim_feedback": False}          # keep the test offline
    rc.run_series(named, seed=5, scenario={"max_ticks": 400, "warmup": False},
                  ser=ser, outdir=td)
    ok("— your memo after game 1 of 3 —" in a.notes and
       "— your memo after game 2 of 3 —" in a.notes,
       f"series log carries per-game headers")
    ok(a.notes.count("memo-1") == 1 and a.notes.count("memo-2") == 1,
       "each game's memo appears exactly once (no 2^k re-append)")
    ok("game 3 of 3" not in a.notes,
       "a FAILED debrief appends nothing to the log")
with tempfile.TemporaryDirectory() as td:
    a2 = ScriptedMemos()
    named = [("Scripty", a2), ("m", BOTS["merchant"])]
    ser = {**rc.section_defaults("series"), "games": 2, "memos": True,
           "memo_history": False, "sim_feedback": False}
    rc.run_series(named, seed=5, scenario={"max_ticks": 400, "warmup": False},
                  ser=ser, outdir=td)
    ok(a2.notes == "memo-2",
       f"memo_history=False keeps latest-memo-only ({a2.notes!r})")

print("FAILURES:", fails)
sys.exit(1 if fails else 0)
