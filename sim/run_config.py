#!/usr/bin/env python3
"""The one entrypoint: run any Flotilla game, series, or tournament from a config
JSON. Agent-first: read config-schema.json, write a config, run this. The dashboard's
Configure tab produces files for exactly this runner.

Config shape (all sections optional except mode + bots/participants):
{
  "mode": "match" | "series" | "tournament",
  "seed": 42,
  "outdir": "runs/my-run",
  "bots":         ["llm:<model-id>[:Label]" | "<scripted-bot>", ...],   # match/series
  "participants": ["llm:<model-id>[:Label]", ...],                      # tournament
  "scenario": { ...any keys from config-schema.json sections world/economy/combat/
                pacing/scenario... },
  "admirals": { "temperature": 0.2, "max_tokens": 700, "timeout_s": 45, "think": false },
  "series":   { "games": 3, "memos": true, "vary_seeds": false, "debrief_timeout_s": 300 },
  "tournament": { "format": "round_robin" | "random_pairs" | "single_elim",
                  "players_per_match": 2 | 4, "games_per_match": 1, "rounds": 1,
                  "memo_policy": "none" | "per_series" | "persistent" }
}
"""
import argparse
import itertools
import json
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from core import Engine                    # noqa: E402
from bots import BOTS                      # noqa: E402
from llm import LLMAdmiral                 # noqa: E402
from series import digest_for              # noqa: E402
from config_schema import SCHEMA           # noqa: E402


def section_defaults(name):
    return {k: s["d"] for k, s in SCHEMA[name].items()}


def make_bot(spec, adm):
    """spec: "llm:<model>[:Label]" | "<scripted>" | a dict for per-player config:
    {"model": <id or scripted name>, "label"?, "prompt"?, and any admirals-section
    override (temperature/max_tokens/timeout_s/think/history_chars/memo_chars)}."""
    if isinstance(spec, dict):
        model = str(spec.get("model", ""))
        if model.startswith("llm:"):
            model = model.split(":", 2)[1]
        if model in BOTS:
            return BOTS[model]
        a = {**adm, **{k: spec[k] for k in
                       ("temperature", "max_tokens", "timeout_s", "think",
                        "history_chars", "memo_chars", "scratchpad",
                        "scratchpad_chars", "warmup_timeout_s") if k in spec}}
        return LLMAdmiral(model, label=spec.get("label") or None,
                          temperature=a["temperature"], max_tokens=a["max_tokens"],
                          timeout=a["timeout_s"], think=a["think"],
                          history_chars=a["history_chars"], memo_chars=a["memo_chars"],
                          prompt=str(spec.get("prompt", ""))[:a["memo_chars"]],
                          scratchpad=a["scratchpad"],
                          scratchpad_chars=a["scratchpad_chars"],
                          warmup_timeout_s=a["warmup_timeout_s"])
    if spec.startswith("llm:"):
        parts = spec.split(":", 2)
        return LLMAdmiral(parts[1], label=parts[2] if len(parts) > 2 else None,
                          temperature=adm["temperature"], max_tokens=adm["max_tokens"],
                          timeout=adm["timeout_s"], think=adm["think"],
                          history_chars=adm["history_chars"],
                          memo_chars=adm["memo_chars"],
                          scratchpad=adm["scratchpad"],
                          scratchpad_chars=adm["scratchpad_chars"],
                          warmup_timeout_s=adm["warmup_timeout_s"])
    return BOTS[spec]


def spec_name(spec):
    if isinstance(spec, dict):
        label = spec.get("label")
        if label:
            return str(label)
        m = str(spec.get("model", "bot"))
        return m.split(":", 2)[1] if m.startswith("llm:") else m
    if spec.startswith("llm:"):
        return spec.split(":")[2] if spec.count(":") == 2 else spec.split(":")[1]
    return spec


def dedupe(names):
    from collections import Counter
    dupes = {n for n, c in Counter(names).items() if c > 1}
    seen = {}
    out = []
    for n in names:
        if n in dupes:
            seen[n] = seen.get(n, -1) + 1
            n = f"{n}-{chr(65 + seen[n])}"
        out.append(n)
    return out


def play_game(named_bots, seed, scenario, outpath, memos_after=None):
    for _, b in named_bots:
        if isinstance(b, LLMAdmiral):
            b._last_thoughts = []
            b.pad = ""                     # per-game working memory; memos carry over
            b.plan_text = ""
    eng = Engine(named_bots, seed=seed, scenario=scenario)
    live_path = os.environ.get("FLOTILLA_LIVE")
    lfh = None
    if live_path:
        # live stream: header + one JSON line per window ("w" truncates = new game;
        # readers detect the reset by their offset exceeding the file size)
        lfh = open(live_path, "w")
        lfh.write(json.dumps(eng.live_header(), separators=(",", ":")) + "\n")
        lfh.flush()

        def _sink(payload):
            lfh.write(json.dumps(payload, separators=(",", ":")) + "\n")
            lfh.flush()
        eng.live = _sink
    result = eng.run()
    if lfh:
        lfh.close()
    replay = eng.replay(result)
    if memos_after:
        replay["memos"] = memos_after
    with open(outpath, "w") as fh:
        json.dump(replay, fh, separators=(",", ":"))
    row = {"seed": seed, "file": outpath,
           "scores": {result["names"][k]: v for k, v in result["scores"].items()},
           "winner": result["names"][result["winner"]]}
    print(json.dumps(row), flush=True)
    return replay, result, row


def debrief_all(named_bots, replay, game_no, total, timeout_s):
    memos = {}
    for fid, (name, b) in enumerate(named_bots):
        if not isinstance(b, LLMAdmiral):
            continue
        keep = b.timeout
        try:
            dg = digest_for(replay, fid, game_no, total)
            out = b.debrief(dg)
        finally:
            b.timeout = keep
        memos[name] = {"memo": out["memo"], "err": out["err"]}
        print(json.dumps({"debrief": name, "after_game": game_no,
                          "err": out["err"]}), flush=True)
    return memos


def run_series(named_bots, seed, scenario, ser, outdir, label="series"):
    os.makedirs(outdir, exist_ok=True)
    games = []
    final_memos = {}
    for g in range(1, ser["games"] + 1):
        gseed = seed + (g - 1 if ser["vary_seeds"] else 0)
        gpath = os.path.join(outdir, f"g{g}.json")
        replay, result, row = play_game(named_bots, gseed, scenario, gpath)
        row["game"] = g
        # every game gets a debrief, INCLUDING the last: the end-of-series review
        # is the memo a future self (rematch, next bracket) picks up
        if ser["memos"]:
            memos = debrief_all(named_bots, replay, g, ser["games"],
                                ser["debrief_timeout_s"])
            replay["memos"] = memos
            with open(gpath, "w") as fh:
                json.dump(replay, fh, separators=(",", ":"))
            if g == ser["games"]:
                final_memos = memos
        games.append(row)
    with open(os.path.join(outdir, "series.json"), "w") as fh:
        json.dump({"games": [dict(game=r["game"], seed=r["seed"],
                                  file=os.path.basename(r["file"]),
                                  winner=r["winner"]) for r in games],
                   "memos": final_memos}, fh, indent=1)
    return games


def matchup_winner(rows, names):
    wins = {n: sum(1 for r in rows if r["winner"] == n) for n in names}
    totals = {n: sum(r["scores"].get(n, 0) for r in rows) for n in names}
    return max(names, key=lambda n: (wins[n], totals[n]))


def run_tournament(cfg, adm, scenario, outdir):
    t = {**section_defaults("tournament"), **cfg.get("tournament", {})}
    ser_defaults = {**section_defaults("series"),
                    "games": t["games_per_match"],
                    **{k: v for k, v in cfg.get("series", {}).items()
                       if k in ("vary_seeds", "debrief_timeout_s")}}
    ser_defaults["memos"] = t["memo_policy"] != "none"
    specs = cfg["participants"]
    names = dedupe([spec_name(s) for s in specs])
    bots = {n: make_bot(s, adm) for n, s in zip(names, specs)}
    rng = random.Random(cfg.get("seed", 42))
    ppm = 2 if t["format"] == "single_elim" else int(t["players_per_match"])

    if t["format"] == "round_robin":
        schedule = [list(c) for c in itertools.combinations(names, ppm)]
        rounds = [schedule]
    elif t["format"] == "random_pairs":
        rounds = []
        for _ in range(int(t["rounds"])):
            order = names[:]
            rng.shuffle(order)
            rounds.append([order[i:i + ppm] for i in range(0, len(order) - ppm + 1, ppm)])
    else:                                              # single_elim
        n = len(names)
        if n & (n - 1):
            raise SystemExit("single_elim needs a power-of-2 participant count")
        order = names[:]
        rng.shuffle(order)
        rounds = "ELIM"

    os.makedirs(outdir, exist_ok=True)
    matchups = []
    standings = {n: {"wins": 0, "games": 0, "score": 0} for n in names}
    seed0 = int(cfg.get("seed", 42))
    midx = 0

    def play_matchup(group, rnd):
        nonlocal midx
        midx += 1
        mdir = os.path.join(outdir, f"m{midx:02d}_" + "_v_".join(group)[:60])
        if t["memo_policy"] == "per_series":
            for n in group:
                if isinstance(bots[n], LLMAdmiral):
                    bots[n].notes = ""
        named = [(n, bots[n]) for n in group]
        ser = dict(ser_defaults)
        # run_series now debriefs after EVERY game incl. the last, so persistent
        # memo carry-over needs no extra pass — notes simply survive on the bot
        rows = run_series(named, seed0 + midx * 1000, scenario, ser, mdir)
        w = matchup_winner(rows, group)
        for n in group:
            standings[n]["games"] += len(rows)
            standings[n]["wins"] += sum(1 for r in rows if r["winner"] == n)
            standings[n]["score"] += sum(r["scores"].get(n, 0) for r in rows)
        matchups.append({"round": rnd, "players": group, "dir": os.path.basename(mdir),
                         "games": [dict(game=r["game"], seed=r["seed"],
                                        file=os.path.relpath(r["file"], outdir),
                                        scores=r["scores"], winner=r["winner"])
                                   for r in rows],
                         "winner": w})
        return w

    if rounds == "ELIM":
        alive = order
        rnd = 0
        while len(alive) > 1:
            rnd += 1
            nxt = []
            for i in range(0, len(alive), 2):
                nxt.append(play_matchup([alive[i], alive[i + 1]], rnd))
            alive = nxt
        champion = alive[0]
    else:
        for rnd, groups in enumerate(rounds, 1):
            for g in groups:
                play_matchup(g, rnd)
        champion = max(names, key=lambda n: (standings[n]["wins"], standings[n]["score"]))

    tj = {"config": cfg, "matchups": matchups, "standings": standings,
          "champion": champion,
          "memos_final": {n: bots[n].notes for n in names
                          if isinstance(bots[n], LLMAdmiral) and bots[n].notes}}
    with open(os.path.join(outdir, "tournament.json"), "w") as fh:
        json.dump(tj, fh, indent=1)
    print(json.dumps({"tournament_done": True, "champion": champion,
                      "standings": standings}), flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("config", help="path to a run-config JSON (see module docstring)")
    args = ap.parse_args()
    cfg = json.load(open(args.config))
    adm = {**section_defaults("admirals"), **cfg.get("admirals", {})}
    scenario = cfg.get("scenario") or None
    outdir = cfg.get("outdir", "run-out")
    mode = cfg.get("mode", "match")
    seed = int(cfg.get("seed", 42))

    if mode == "tournament":
        run_tournament(cfg, adm, scenario, outdir)
        return
    specs = cfg["bots"]
    names = dedupe([spec_name(s) for s in specs])
    named = [(n, make_bot(s, adm)) for n, s in zip(names, specs)]
    os.makedirs(outdir, exist_ok=True)
    if mode == "match":
        play_game(named, seed, scenario, os.path.join(outdir, "match.json"))
    elif mode == "series":
        ser = {**section_defaults("series"), **cfg.get("series", {})}
        run_series(named, seed, scenario, ser, outdir)
    else:
        raise SystemExit(f"unknown mode {mode!r}")


if __name__ == "__main__":
    main()
