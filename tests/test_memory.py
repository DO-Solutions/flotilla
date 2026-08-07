#!/usr/bin/env python3
"""Memory batch: voyage reports, warmup planning, scratchpad, prompt assembly."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "sim"))
from core import Engine, WINDOW          # noqa: E402
from llm import LLMAdmiral               # noqa: E402

fails = 0


def ok(cond, msg):
    global fails
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        fails += 1


class Idle:
    name = "idle"

    def decide(self, summary, rng):
        return {}


class Programmer:
    name = "programmer"

    def __init__(self, queue):
        self.queue = list(queue)
        self.summaries = []

    def decide(self, summary, rng):
        self.summaries.append(summary)
        return self.queue.pop(0) if self.queue else {}


# --- voyage reports: out, back, one-line report in events + next summary ---
OUTBACK = "when self.tick < 250: helm.goto(40, 28)\ndefault: helm.home()"
bot = Programmer([{"programs": {"A": OUTBACK}}])
eng = Engine([("P", bot), ("X", Idle())], seed=5)
for _ in range(WINDOW * 8):
    eng.tick()
reps = [e for e in eng.events if e["k"] == "report" and e["fleet"] == 0]
ok(len(reps) > 0, f"voyage reports filed on return ({len(reps)})")
ok("returned:" in reps[0]["s"] and "hits" in reps[0]["s"],
   f"report reads like a report: {reps[0]['s'][:70]!r}")
got = [s for s in bot.summaries if s.get("reports")]
ok(got and any("returned:" in r for r in got[0]["reports"]),
   "reports delivered in the next window's summary")
ok(all(len(s.get("reports", [])) <= 12 for s in bot.summaries), "reports capped/window")

engoff = Engine([("P", Programmer([{"programs": {"A": OUTBACK}}])), ("X", Idle())],
                seed=5, scenario={"voyage_reports": False})
for _ in range(WINDOW * 8):
    engoff.tick()
ok(not any(e["k"] == "report" for e in engoff.events), "voyage_reports=False: silent")

# --- warmup: engine runs plan() before window 0 and records it ---
class Planner(Idle):
    name = "planner"
    model_label = "planbot"

    def __init__(self):
        self.planned = 0

    def plan(self, summary):
        self.planned += 1
        return dict(plan="Open with trade; scout Nihiru.", tin=10, tout=5,
                    ms=100, cost=0.001, err=None)


pb = Planner()
eng2 = Engine([("P", pb), ("X", Idle())], seed=5, max_ticks=WINDOW)
eng2.run()
ok(pb.planned == 1, "warmup calls plan() exactly once")
plans = [d for d in eng2.decisions if d.get("plan")]
ok(plans and plans[0]["thoughts"].startswith("(opening plan)"),
   "plan recorded in decisions for the replay")
pb2 = Planner()
eng3 = Engine([("P", pb2), ("X", Idle())], seed=5, max_ticks=WINDOW,
              scenario={"warmup": False})
eng3.run()
ok(pb2.planned == 0, "warmup=False skips planning")

# --- scratchpad + plan prompt assembly (stubbed transport) ---
class StubbedAdmiral(LLMAdmiral):
    def __init__(self, replies, **kw):
        super().__init__("stub-model", **kw)
        self.replies = list(replies)
        self.sent = []

    def _chat(self, messages):
        self.sent.append(messages)
        return self.replies.pop(0), 10, 5, 50


SUM = {"window": 1, "scenario": {"programs": False}, "parley_log": []}
a = StubbedAdmiral(['{"thoughts": "t1", "scratchpad": "K3 has 3 raiders NE"}',
                    '{"thoughts": "t2"}'])
a.decide(dict(SUM), None)
ok(a.pad == "K3 has 3 raiders NE", "scratchpad captured from reply")
a.decide(dict(SUM, window=2), None)
user2 = a.sent[1][1]["content"]
ok("YOUR SCRATCHPAD" in user2 and "K3 has 3 raiders NE" in user2,
   "scratchpad rides every later prompt")
ok(user2.index("SCRATCHPAD") > user2.index("CAMPAIGN JOURNAL")
   if "CAMPAIGN JOURNAL" in user2 else True,
   "scratchpad sits after the append-only history (cache-friendly)")

anp = StubbedAdmiral(['{"thoughts": "x", "scratchpad": "secret"}'], scratchpad=False)
anp.decide(dict(SUM), None)
ok(anp.pad == "", "scratchpad=off ignores the field")

ap = StubbedAdmiral(["Trade first, then raid Tokelau.", '{"thoughts": "t"}'])
out = ap.plan(dict(SUM))
ok(out["plan"] == "Trade first, then raid Tokelau." and ap.plan_text == out["plan"],
   "plan() stores the opening plan")
ap.decide(dict(SUM), None)
ok("opening plan for this game" in ap.sent[1][1]["content"],
   "plan rides subsequent prompts")
ok("WARMUP" in ap.sent[0][0]["content"], "plan call framed as warmup")

ad = StubbedAdmiral(["memo text"])
ad.pad = "final pad"
ad.plan_text = "the plan"
ad.debrief("digest here")
duser = ad.sent[0][1]["content"]
ok("final scratchpad" in duser and "opening plan" in duser,
   "debrief receives scratchpad + plan")

print("FAILURES:", fails)
sys.exit(1 if fails else 0)
