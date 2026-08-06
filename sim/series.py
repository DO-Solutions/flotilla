#!/usr/bin/env python3
"""Series mode: the same admirals play N games back-to-back; between games each LLM
admiral studies its own record and rewrites a strategy memo that rides into the next
game's prompts. This is the "watch them learn" mode.

Fog discipline: the between-game digest contains ONLY what that admiral could know —
its own thoughts/orders/usage, its own fleet's events, and the public score sheet.
Enemy intent logs are never shown to a learning admiral.
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from core import Engine  # noqa: E402
from run_match import make_bot      # noqa: E402
from llm import LLMAdmiral          # noqa: E402


def digest_for(replay, fid, game_no, total_games):
    names = {int(k): v for k, v in replay["result"]["names"].items()} \
        if isinstance(list(replay["result"]["names"])[0], str) else replay["result"]["names"]
    scores = {int(k) if isinstance(k, str) else k: v
              for k, v in replay["result"]["scores"].items()}
    rank = sorted(scores, key=lambda k: -scores[k]).index(fid) + 1
    own_dec = [d for d in replay["decisions"] if d["fleet"] == fid]
    own_thoughts = [d["thoughts"] for d in own_dec if d["thoughts"]]
    spawns = {}
    losses = {}
    for e in replay["events"]:
        if e["k"] == "spawn" and e["fleet"] == fid:
            spawns[e["preset"]] = spawns.get(e["preset"], 0) + 1
        if e["k"] == "sink" and e["fleet"] == fid:
            losses[e["preset"]] = losses.get(e["preset"], 0) + 1
    my_signals = sum(1 for e in replay["events"]
                     if e["k"] == "signal" and e["fleet"] == fid)
    # public score timeline (coarse): every ~1000 ticks
    timeline = []
    for fr in replay["frames"]:
        if fr["t"] % 1000 == 0:
            timeline.append({"t": fr["t"],
                             **{names[row[0]]: row[4] for row in fr["f"]}})
    return json.dumps({
        "series_game": f"{game_no}/{total_games}",
        "final_scores": {names[k]: v for k, v in sorted(scores.items())},
        "your_fleet": names[fid], "your_rank": rank,
        "your_ships_built": spawns, "your_ships_lost": losses,
        "your_signal_hoists": my_signals,
        "public_score_timeline": timeline,
        "your_thoughts_first": own_thoughts[:3],
        "your_thoughts_last": own_thoughts[-5:],
    }, indent=1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bots", required=True)
    ap.add_argument("--games", type=int, default=3)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--vary-seeds", action="store_true",
                    help="new map each game (default: same map = sharper learning)")
    ap.add_argument("--ticks", type=int, default=None)
    ap.add_argument("--scenario", default=None, help="path to a scenario JSON")
    ap.add_argument("--outdir", default="series-out")
    args = ap.parse_args()

    os.makedirs(args.outdir, exist_ok=True)
    specs = [b.strip() for b in args.bots.split(",")]
    bots = [make_bot(s) for s in specs]           # SAME instances across games -> memos persist
    names = [getattr(b, "model_label", None) or s for b, s in zip(bots, specs)]
    from collections import Counter
    dupes = {n for n, c in Counter(names).items() if c > 1}
    seen = {}
    for i, n in enumerate(names):
        if n in dupes:
            seen[n] = seen.get(n, -1) + 1
            names[i] = f"{n}-{chr(65 + seen[n])}"
    series = {"bots": specs, "seed": args.seed, "games": [], "memos": {n: [] for n in names}}

    for g in range(1, args.games + 1):
        seed = args.seed + (g - 1 if args.vary_seeds else 0)
        for b in bots:
            if isinstance(b, LLMAdmiral):
                b._last_thoughts = []             # thoughts reset per game; memo persists
        scenario = json.load(open(args.scenario)) if args.scenario else None
        eng = Engine(list(zip(names, bots)), seed=seed, max_ticks=args.ticks,
                     scenario=scenario)
        result = eng.run()
        replay = eng.replay(result)
        gpath = os.path.join(args.outdir, f"g{g}.json")
        with open(gpath, "w") as fh:
            json.dump(replay, fh, separators=(",", ":"))
        print(json.dumps({"game": g, "seed": seed, "scores": result["scores"],
                          "winner": result["names"][result["winner"]]}), flush=True)
        series["games"].append({"game": g, "seed": seed, "file": gpath,
                                "scores": {result["names"][k]: v
                                           for k, v in result["scores"].items()},
                                "winner": result["names"][result["winner"]]})
        if g < args.games:                        # between-game study
            game_memos = {}
            for fid, b in enumerate(bots):
                if not isinstance(b, LLMAdmiral):
                    continue
                dg = digest_for(replay, fid, g, args.games)
                out = b.debrief(dg)
                series["memos"][names[fid]].append(
                    {"after_game": g, "memo": out["memo"], "cost": out["cost"],
                     "err": out["err"]})
                game_memos[names[fid]] = {"memo": out["memo"], "err": out["err"]}
                print(json.dumps({"debrief": names[fid], "after_game": g,
                                  "err": out["err"],
                                  "memo_head": out["memo"][:140]}), flush=True)
            replay["memos"] = game_memos              # post-game notes ride IN the replay
            with open(gpath, "w") as fh:
                json.dump(replay, fh, separators=(",", ":"))

    with open(os.path.join(args.outdir, "series.json"), "w") as fh:
        json.dump(series, fh, indent=1)
    print(json.dumps({"series_done": True,
                      "winners": [gm["winner"] for gm in series["games"]]}), flush=True)


if __name__ == "__main__":
    main()
