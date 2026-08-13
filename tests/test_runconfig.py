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

# --- parallel tournaments: matchups in worker threads, staggered starts,
# fresh bots per matchup, a consistent bracket at the end ---
with tempfile.TemporaryDirectory() as td:
    cfg = {"mode": "tournament", "seed": 11,
           "participants": ["merchant", "corsair", "admiralty"],
           "scenario": {"max_ticks": 300, "warmup": False},
           "series": {},
           "tournament": {"format": "round_robin", "players_per_match": 2,
                          "games_per_match": 1, "memo_policy": "none",
                          "parallel": 3, "stagger_s": 0}}
    adm = rc.section_defaults("admirals")
    scenario = rc.merged_scenario(cfg)
    rc.run_tournament(cfg, adm, scenario, td)
    tj = json.load(open(os.path.join(td, "tournament.json")))
    ok(len(tj["matchups"]) == 3, f"parallel round_robin plays all 3 matchups")
    ok(tj.get("champion") in ("merchant", "corsair", "admiralty"),
       "parallel tournament crowns a champion")
    games = sum(st["games"] for st in tj["standings"].values())
    ok(games == 6, f"standings count every game exactly once ({games})")
    sw = sum(st["series_wins"] for st in tj["standings"].values())
    ok(sw == 3, f"every decided matchup credits exactly one series win ({sw})")
    ok(tj["standings"][tj["champion"]]["series_wins"]
       == max(st["series_wins"] for st in tj["standings"].values()),
       "the champion has the most series wins (not just game wins)")
    dirs = sorted(d for d in os.listdir(td) if d.startswith("m"))
    ok(len(dirs) == 3 and all(os.path.isfile(os.path.join(td, d, "g1.json"))
                              for d in dirs),
       "every matchup dir holds its replay")

# --- tournament map set (#125): with map_set on (the default) game N is the
# SAME map in every matchup; off restores the per-slot seed derivation ---


def _matchup_seeds(td):
    """{matchup dir: [g1 seed, g2 seed, ...]} from the replays on disk."""
    out = {}
    for d in sorted(os.listdir(td)):
        if not d.startswith("m"):
            continue
        seeds = []
        for g in sorted(os.listdir(os.path.join(td, d))):
            if g.startswith("g") and g.endswith(".json"):
                rp = json.load(open(os.path.join(td, d, g)))
                seeds.append(rp["meta"]["seed"])
        out[d] = seeds
    return out


MAPSET_CFG = {"mode": "tournament", "seed": 77,
              "participants": ["merchant", "corsair", "admiralty"],
              "scenario": {"max_ticks": 300, "warmup": False},
              "series": {"vary_seeds": True},   # a SET of maps, not one map
              "tournament": {"format": "round_robin", "players_per_match": 2,
                             "games_per_match": 2, "memo_policy": "none",
                             "full_series": True}}
with tempfile.TemporaryDirectory() as td:
    rc.run_tournament(MAPSET_CFG, rc.section_defaults("admirals"),
                      rc.merged_scenario(MAPSET_CFG), td)
    seeds = _matchup_seeds(td)
    ok(len(seeds) == 3 and len(set(tuple(v) for v in seeds.values())) == 1,
       f"map_set on: every matchup plays the identical seed walk ({seeds})")
    first = next(iter(seeds.values()))
    ok(first == [77, 78],
       f"...vary_seeds walks the set from the tournament seed ({first})")
with tempfile.TemporaryDirectory() as td:
    cfg = json.loads(json.dumps(MAPSET_CFG))
    cfg["tournament"]["map_set"] = False
    rc.run_tournament(cfg, rc.section_defaults("admirals"),
                      rc.merged_scenario(cfg), td)
    seeds = _matchup_seeds(td)
    ok([v[0] for v in seeds.values()] == [1077, 2077, 3077],
       f"map_set off: each bracket slot derives its own seeds ({seeds})")
ok("map_set" in rc.config_schema.SCHEMA["tournament"],
   "map_set rides the schema (dash + agents see it)")
# persistent memos force sequential (shared mind can't be parallelized)
with tempfile.TemporaryDirectory() as td:
    cfg = {"mode": "tournament", "seed": 11,
           "participants": ["merchant", "corsair"],
           "scenario": {"max_ticks": 300, "warmup": False},
           "tournament": {"format": "round_robin", "games_per_match": 1,
                          "memo_policy": "persistent", "parallel": 4,
                          "stagger_s": 0}}
    rc.run_tournament(cfg, rc.section_defaults("admirals"),
                      rc.merged_scenario(cfg), td)
    tj = json.load(open(os.path.join(td, "tournament.json")))
    ok(len(tj["matchups"]) == 1, "persistent-memo tournament still completes")

print("FAILURES:", fails)
sys.exit(1 if fails else 0)
