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

from replay_codec import fleet_dyn   # v3 replays mix 8-col and 4-col fleet rows


def digest_for(replay, fid, game_no, total_games, full_info=False):
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
                             **{names[row[0]]: fleet_dyn(row)[2]
                                for row in fr["f"]}})
    # elimination is the loudest lesson a game can teach — never let it pass
    # silently. Attacks can end a fleet inside one window; the debrief is where
    # the admiral finds out what happened and from whom.
    # parenthesized deliberately: the old one-liner parsed as
    # max(1, (ticks or last_frame) if frames else 1) — a replay with ticks but
    # no frames yielded total_t=1 and at_pct_of_game=30000
    total_t = replay["result"].get("ticks") \
        or (replay["frames"][-1]["t"] if replay.get("frames") else 0)
    total_t = max(1, total_t)
    death = None
    my_kills = []
    for e in replay["events"]:
        if e["k"] != "flag_sunk":
            continue
        by = names.get(e.get("by")) if e.get("by") is not None else None
        if e["fleet"] == fid:
            death = {
                "what": "YOUR FLAGSHIP WAS DESTROYED — you were ELIMINATED",
                "at_tick": e["t"],
                "at_pct_of_game": round(100 * e["t"] / total_t),
                "destroyed_by": by or "unknown",
                "consequence": "you kept your banked score but lost every ship "
                               "and could take no further action for the rest "
                               "of the game",
                "study": "trace HOW their force reached your flagship: what "
                         "warning signs (contacts, combat reports, hull drops) "
                         "did you see in your final windows, and what defense "
                         "or earlier response was missing?",
            }
        elif e.get("by") == fid:
            my_kills.append({"eliminated": names[e["fleet"]], "at_tick": e["t"]})
    out = {
        "series_game": f"{game_no}/{total_games}",
        "final_scores": {names[k]: v for k, v in sorted(scores.items())},
        "your_fleet": names[fid], "your_rank": rank,
        "your_ships_built": spawns, "your_ships_lost": losses,
        "your_signal_hoists": my_signals,
        "public_score_timeline": timeline,
        "your_thoughts_first": own_thoughts[:3],
        "your_thoughts_last": own_thoughts[-5:],
    }
    if death:
        out["YOUR_ELIMINATION"] = death
    if my_kills:
        out["flagships_you_destroyed"] = my_kills
    # DEATH-LIFTED FULL PICTURE (debrief_full_info): once you're eliminated you
    # can watch the rest of the match unfold — so a beaten admiral sees WHY it
    # lost (the winner's economy curve, build rate, attrition) from its own
    # death onward. Before death it keeps only what its own windows saw. A
    # SURVIVING admiral (won, or the game ended on the clock) gets nothing extra
    # — its fog is never lifted. The window it died in is included, so it never
    # loses the snapshot from the moment it was killed.
    if full_info and death is not None:
        window = int((replay.get("meta") or {}).get("config", {}).get("window", 100)) or 100
        death_t = death["at_tick"]
        from_t = (death_t // window) * window        # start of the kill window
        fleets = {}
        for k in sorted(names):
            fleets[names[k]] = {"ships_built": {}, "ships_lost": {},
                                "signal_hoists": 0, "eliminated_at": None}
        for e in replay["events"]:
            nm = names.get(e.get("fleet"))
            if nm is None:
                continue
            if e["k"] == "spawn":
                b = fleets[nm]["ships_built"]
                b[e["preset"]] = b.get(e["preset"], 0) + 1
            elif e["k"] == "sink":
                lo = fleets[nm]["ships_lost"]
                lo[e["preset"]] = lo.get(e["preset"], 0) + 1
            elif e["k"] == "signal":
                fleets[nm]["signal_hoists"] += 1
            elif e["k"] == "flag_sunk":
                fleets[nm]["eliminated_at"] = e["t"]
        econ = []
        for fr in replay["frames"]:
            if fr["t"] >= from_t and fr["t"] % 1000 == 0:
                econ.append({"t": fr["t"],
                             **{names[row[0]]: {"treasury": fleet_dyn(row)[0],
                                                "hauled": fleet_dyn(row)[1]}
                                for row in fr["f"] if row[0] in names}})
        out["FULL_PICTURE"] = {
            "note": f"you were eliminated at tick {death_t}; from then on you "
                    "could watch the whole board. Full record of every fleet "
                    "AFTER your death (before it, you only had your own windows) "
                    "— study how the game finished without you.",
            "fleets": fleets,
            "economy_timeline": econ,
        }
    return json.dumps(out, indent=1)


def main():
    """Thin delegate: this module used to carry its own series loop with
    SUBTLY different semantics (skipped the final debrief, reset less per-game
    state) — same nominal config, silently different results. The one true
    runner lives in run_config; this entrypoint just builds a config for it."""
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
    scenario = json.load(open(args.scenario)) if args.scenario else {}
    if args.ticks:
        scenario["max_ticks"] = args.ticks
    cfg = {"mode": "series", "seed": args.seed, "outdir": args.outdir,
           "bots": [b.strip() for b in args.bots.split(",")],
           "scenario": scenario,
           "series": {"games": args.games, "vary_seeds": args.vary_seeds}}
    import run_config
    run_config.run(cfg)


if __name__ == "__main__":
    main()
