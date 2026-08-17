#!/usr/bin/env python3
"""Historic Moments (docs/SPECTATOR.md §5): anchors, fog-of-material,
validation, synthesis, and the cost plumbing.

The LLM's prose is judged by a human (the bake-off); everything AROUND the
call is what can rot silently and is proven here:

  * the anchor rule — a beat citing a moment that never happened is dropped
    MECHANICALLY, before it renders (the anti-hallucination contract)
  * the material rule — a player's narration input carries its OWN thoughts,
    memos and parley, never a rival's thoughts
  * config validation — historic_moments without a narrator model, or a
    tournament switch without the series switch it synthesizes from, is
    refused loudly at submit, not skipped silently hours later
  * cost — narration lands in the pre-flight estimate (and therefore under
    lim_cost) and in the aux worker's provider-key scoping

No network anywhere: the narrator transport is stubbed and narrate_* return
the same shapes the real calls do.
"""
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "sim"))
TMP = tempfile.mkdtemp(prefix="flotilla-moments-")
os.environ["FLOTILLA_LIBRARY"] = TMP

from keelspring import moments                               # noqa: E402
from keelspring.runner import validate_config                # noqa: E402
import server                                                # noqa: E402

fails = 0


def ok(cond, msg):
    global fails
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        fails += 1


def mk_replay():
    return {
        "result": {"names": {"0": "Alpha", "1": "Beta"},
                   "scores": {"0": 10, "1": 5}, "winner": 0, "ticks": 100},
        "decisions": [
            {"t": 0, "fleet": 0, "thoughts": "we sail at dawn", "u": {}},
            {"t": 50, "fleet": 1, "thoughts": "I am worried about the raiders",
             "u": {}},
        ],
        "events": [
            {"k": "parley", "t": 20, "fleet": 0, "to": 1, "text": "surrender"},
            {"k": "sink", "t": 40, "fleet": 1, "by": 0, "ship": 7,
             "x": 1, "y": 2, "preset": "raider"},
            {"k": "region", "t": 60, "fleet": 0, "region": 2,
             "name": "Foo Waters", "prev": None},
            {"k": "flag_sunk", "t": 90, "fleet": 1, "by": 0},
            {"k": "treaty", "t": 30, "fleet": 1, "other": 0,
             "terms": "no raids"},
            {"k": "treaty_end", "t": 70, "fleet": 0, "other": 1,
             "cause": "aggression"},
        ],
        "memos": {"Alpha": {"memo": "keep the pressure on"},
                  "Beta": {"memo": "avoid the trap next time"}},
        "frames": [],
        "meta": {"frame_every": 2},
    }


# ---- anchors: the citation vocabulary IS the validator ----
keys, lines = moments.anchors_for([mk_replay(), mk_replay()])
ok((1, 40) in keys and (2, 90) in keys and (1, 20) in keys,
   "anchors carry (game, tick) for every big beat, per game")
ok(any("Alpha sank Beta's raider" in ln for ln in lines),
   f"anchor lines read like the record ({[l for l in lines if 't40' in l]})")
ok(any("ELIMINATED" in ln for ln in lines),
   "a flagship kill is named for what it is")
ok(any("signed a treaty" in ln for ln in lines)
   and any("BROKE the treaty" in ln for ln in lines),
   "treaty beats are citable anchors — formed and broken both")

# ---- material: the player's OWN words only ----
mat = moments.player_material([mk_replay()], 1, "Beta")
ok("I am worried" in mat and "avoid the trap" in mat,
   "Beta's material carries Beta's thoughts and memo")
ok("we sail at dawn" not in mat,
   "a RIVAL's thoughts never enter the material — the emotional read is "
   "grounded in the admiral's own words")
ok("surrender" in mat, "parley the player was party to is included")

# ---- beat validation: mechanical, before anything renders ----
beats = [
    {"game": 1, "tick": 40, "title": "First blood", "note": "n",
     "emotion": "Triumphant"},
    {"game": 1, "tick": 41, "title": "Almost right", "note": "n"},   # no anchor
    {"game": 9, "tick": 40, "title": "Wrong game", "note": "n"},
    {"tick": 40, "title": "No game at all"},
    {"game": 2, "tick": 90, "title": "The kill",
     "note": "x" * 999, "emotion": "y" * 99},
]
kept, dropped = moments.validate_beats(beats, keys, 10)
ok(len(kept) == 2 and dropped == 3,
   f"anchored beats survive, hallucinated ones drop ({len(kept)} kept, "
   f"{dropped} dropped)")
ok(kept[0]["emotion"] == "triumphant" and len(kept[1]["note"]) <= 280
   and len(kept[1]["emotion"]) <= 24,
   "beat fields are normalized and capped")
kept2, _ = moments.validate_beats([dict(b, game=1, tick=40) for b in [{}] * 9],
                                  keys, 3)
ok(len(kept2) == 3, "the beat count cap trims, it does not reject")
tkeys = {("m01_A_v_B", 1, 40)}
tk, td = moments.validate_beats(
    [{"matchup": "m01_A_v_B", "game": 1, "tick": 40, "title": "t"},
     {"matchup": "m02_X_v_Y", "game": 1, "tick": 40, "title": "t"}],
    tkeys, 8, extra="matchup")
ok(len(tk) == 1 and td == 1 and tk[0]["matchup"] == "m01_A_v_B",
   "tournament beats must cite their matchup too — a valid (game, tick) in "
   "the WRONG matchup is still a hallucination")

# ---- narrate_series with a stubbed transport ----
class FakeBot:
    price = (1.0, 2.0)

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = 0

    def _chat(self, msgs):
        self.calls += 1
        return self.replies.pop(0), 1000, 200, 50

    @staticmethod
    def _extract_json(text):
        from keelspring.llm import LLMAdmiral
        return LLMAdmiral._extract_json(text)


REPLY = json.dumps({"story": "Alpha pressed early and never let go.",
                    "beats": [
                        {"game": 1, "tick": 40, "title": "First blood",
                         "note": "The raider went down.", "emotion": "grim"},
                        {"game": 1, "tick": 77, "title": "Invented",
                         "note": "never happened"},
                    ]})
fake = FakeBot([REPLY, REPLY])
moments.narrator, _real_narrator = (lambda m, t, c: fake), moments.narrator
try:
    out = moments.narrate_series([mk_replay()], [("Alpha", 0), ("Beta", 1)],
                                 "glm-5.2", 300, 2500)
finally:
    moments.narrator = _real_narrator
ok(out["_meta"]["model"] == "glm-5.2" and fake.calls == 2,
   "one narrator call per LLM player, model recorded")
ok(out["Alpha"]["story"].startswith("Alpha pressed")
   and len(out["Alpha"]["beats"]) == 1
   and out["Alpha"]["dropped_beats"] == 1,
   f"the story lands, the invented beat is dropped and COUNTED "
   f"({out['Alpha'].get('dropped_beats')})")
expect_cost = round(2 * (1000 * 1.0 + 200 * 2.0) / 1e6, 6)
ok(abs(out["_meta"]["cost"] - expect_cost) < 1e-9,
   f"usage cost accumulates across players (${out['_meta']['cost']})")
no_model = moments.narrate_series([mk_replay()], [("Alpha", 0)], "", 300, 2500)
ok(no_model["_meta"].get("err") and "Alpha" not in no_model,
   "no narrator model = a recorded error and ZERO calls, never a crash")

# ---- narrate_tournament synthesizes from the series stories ----
sm = [{"dir": "m01_Alpha_v_Beta", "players": ["Alpha", "Beta"],
       "winner": "Alpha",
       "moments": {"Alpha": {"story": "s", "beats": [
           {"game": 1, "tick": 40, "title": "First blood", "note": "n"}]},
           "Beta": {"story": "s2", "beats": []}}}]
TREPLY = json.dumps({"story": "Across the bracket, Alpha ruled the water.",
                     "beats": [
                         {"matchup": "m01_Alpha_v_Beta", "game": 1, "tick": 40,
                          "title": "Where it started", "note": "n"},
                         {"matchup": "m99_fake", "game": 1, "tick": 40,
                          "title": "Invented", "note": "n"}]})
fake2 = FakeBot([TREPLY, TREPLY])
moments.narrator = (lambda m, t, c: fake2)
try:
    tout = moments.narrate_tournament(
        sm, {"Alpha": {"series_wins": 1}, "Beta": {"series_wins": 0}},
        "Alpha", "glm-5.2", 300, 2500)
finally:
    moments.narrator = _real_narrator
ok(len(tout["Alpha"]["beats"]) == 1 and tout["Alpha"]["dropped_beats"] == 1,
   "tournament beats validate against the SERIES beats' anchor triples")
ok(tout["Alpha"]["beats"][0]["matchup"] == "m01_Alpha_v_Beta",
   "a kept tournament beat carries its matchup for the deep link")

# ---- config validation: loud at the boundary ----
p = validate_config({"mode": "series", "bots": ["merchant", "corsair"],
                     "series": {"historic_moments": True}})
ok(any("historic_moments_model" in x for x in p),
   f"historic_moments without a model is refused at submit ({p[:1]})")
p = validate_config({"mode": "tournament", "participants": ["a", "b"],
                     "tournament": {"historic_moments": True}})
ok(any("series.historic_moments" in x for x in p),
   "tournament narration without the series pass it synthesizes from is "
   "refused")
p = validate_config({"mode": "tournament", "participants": ["a", "b"],
                     "series": {"historic_moments": True,
                                "historic_moments_model": "glm-5.2"},
                     "tournament": {"historic_moments": True}})
ok(p == [], f"a fully-specified narration config passes ({p})")

# ---- cost: narration rides the estimate, so lim_cost can see it ----
BASE = {"mode": "tournament", "seed": 1,
        "participants": ["llm:kimi-k3:A", "llm:glm-5.2:B",
                         "llm:kimi-k3:C", "llm:glm-5.2:D"],
        "scenario": {"max_ticks": 6000},
        "tournament": {"format": "round_robin", "players_per_match": 2,
                       "games_per_match": 1}}
est_off = server._estimate_cost(json.loads(json.dumps(BASE)))
cfg_on = json.loads(json.dumps(BASE))
cfg_on["series"] = {"historic_moments": True,
                    "historic_moments_model": "glm-5.2"}
est_series = server._estimate_cost(cfg_on)
cfg_both = json.loads(json.dumps(cfg_on))
cfg_both["tournament"]["historic_moments"] = True
est_both = server._estimate_cost(cfg_both)
tin, tout_ = server._narr_tokens()
per_narr = (tin * 0.70 + tout_ * 2.20) / 1e6          # glm-5.2 prices
ok(est_series > est_off and
   abs((est_series - est_off) - per_narr * 2 * 6) < 0.03,
   f"per-series narration adds ppm×matchups calls to the estimate "
   f"(+${est_series - est_off:.2f}, expected ≈${per_narr * 12:.2f})")
ok(est_both > est_series and
   abs((est_both - est_series) - per_narr * 4) < 0.03,
   f"the tournament switch adds one call per participant "
   f"(+${est_both - est_series:.2f})")

# series mode: one call per LLM player
S2 = {"mode": "series", "seed": 1,
      "bots": ["llm:kimi-k3:A", "llm:glm-5.2:B"],
      "scenario": {"max_ticks": 6000}, "series": {"games": 3}}
e_off = server._estimate_cost(json.loads(json.dumps(S2)))
s_on = json.loads(json.dumps(S2))
s_on["series"] = {"games": 3, "historic_moments": True,
                  "historic_moments_model": "glm-5.2"}
e_on = server._estimate_cost(s_on)
ok(abs((e_on - e_off) - per_narr * 2) < 0.03,
   f"series mode: one narration per player (+${e_on - e_off:.2f})")

# an unpriced narrator adds nothing (and, like unpriced players, is honest
# about it elsewhere) rather than inventing a number
s_unpriced = json.loads(json.dumps(s_on))
s_unpriced["series"]["historic_moments_model"] = "totally-unknown-model"
ok(server._estimate_cost(s_unpriced) == e_off,
   "an unpriced narrator model adds $0 rather than a guess")

# ---- the narrator model reaches the aux worker's key scoping ----
mods = server._cfg_models(cfg_on)
ok("glm-5.2" in mods and "kimi-k3" in mods,
   "the narrator model joins the job's model set — least-privilege provider "
   "scoping must ship ITS key too")
mods_off = server._cfg_models(json.loads(json.dumps(BASE)))
ok("glm-5.2" in mods_off,                      # glm plays in BASE anyway
   "sanity: player models still scope")
cfg_narr_only = {"bots": ["llm:kimi-k3:A"],
                 "series": {"historic_moments": True,
                            "historic_moments_model": "openai-gpt-5.6-luna"}}
ok("openai-gpt-5.6-luna" in server._cfg_models(cfg_narr_only),
   "…including a narrator nobody plays")

# ---- keystore: the Server-tab default narrator round-trips ----
st = server._keystore()
ok(st.get("narrator", {}).get("model") == "",
   "a fresh keystore has an empty (no-default) narrator")
st["narrator"]["model"] = "glm-5.2"
server._save_keystore(st)
ok(server._keystore()["narrator"]["model"] == "glm-5.2",
   "the narrator default persists")
# and the estimate falls back to it when the config names no model
s_fallback = json.loads(json.dumps(s_on))
s_fallback["series"]["historic_moments_model"] = ""
ok(abs(server._estimate_cost(s_fallback) - e_on) < 0.011,
   "the estimate uses the Server-tab default when the config is silent — "
   "the same fallback submit-time injection applies")

# ---- the series.json shipping chain (source anchors) ----
# A matchup's series.json is the ONLY home of its memos, sim_feedback and
# historic_moments, and the worker box is disposable. Three links must exist:
# the runner announces the write AFTER it happens, the aux agent ships on
# that line, and the local executor mirrors the file into the library.
runner_src = open(os.path.join(ROOT, "keelspring", "runner.py")).read()
aux_src = open(os.path.join(ROOT, "scripts", "aux_agent.py")).read()
srv_src = open(os.path.join(ROOT, "server.py")).read()
ok('"series_saved"' in runner_src
   and runner_src.index("json.dump(doc, fh") <
       runner_src.index('_emit({"series_saved"'),
   "the runner emits series_saved AFTER the write (an emit before ships a "
   "stale or missing file — the memos_saved lesson)")
ok('"series_saved"' in aux_src,
   "the aux agent ships the matchup series.json on that line")
ok('fn == "series.json" and root != outdir' in srv_src,
   "the local executor mirrors matchup series.json into the library")
ok("server-managed" in srv_src,
   "series-mode ingest refuses a raw series.json overwrite (the ledger is "
   "_update_series_json's)")

# ---- end to end: a real (scripted) series run writes the section ----
# Scripted bots mean zero narrator calls (no LLM players), but the whole
# plumbing fires: ser carries the knobs, run_series calls the pass, and
# series.json gains historic_moments with the model recorded. This is the
# leg that catches "the knob exists but nothing reads it" — the exact class
# of the pipeline_depth bug.
import subprocess                                            # noqa: E402
out_dir = os.path.join(TMP, "e2e-series")
cfg = {"mode": "series", "seed": 7, "outdir": out_dir,
       "bots": ["merchant", "corsair"],
       "scenario": {"width": 48, "height": 30, "max_ticks": 120,
                    "warmup": False},
       "series": {"games": 1, "memos": False, "sim_feedback": False,
                  "historic_moments": True,
                  "historic_moments_model": "glm-5.2"}}
os.makedirs(out_dir, exist_ok=True)
cp = os.path.join(out_dir, "cfg.json")
json.dump(cfg, open(cp, "w"))
r = subprocess.run([sys.executable, os.path.join(ROOT, "sim", "run_config.py"),
                    cp], capture_output=True, text=True, timeout=600)
ok(r.returncode == 0,
   f"the scripted probe series runs (rc {r.returncode}): "
   f"{(r.stdout + r.stderr)[-200:]}")
if r.returncode == 0:
    sj = json.load(open(os.path.join(out_dir, "series.json")))
    hm = sj.get("historic_moments")
    ok(isinstance(hm, dict) and hm.get("_meta", {}).get("model") == "glm-5.2",
       f"series.json carries historic_moments with the narrator recorded "
       f"({(hm or {}).get('_meta')})")
    ok(all(k == "_meta" for k in hm),
       "scripted players get no story (narration is for LLM admirals) and "
       "no phantom call was made")

print(f"FAILURES: {fails}")
sys.exit(1 if fails else 0)
