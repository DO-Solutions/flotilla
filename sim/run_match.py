#!/usr/bin/env python3
"""Run one Flotilla match -> replay JSON (optionally a self-contained viewer HTML)."""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from core import Engine  # noqa: E402
from bots import BOTS              # noqa: E402
from llm import LLMAdmiral         # noqa: E402


def make_bot(spec):
    """'merchant' -> scripted bot; 'llm:qwen3.5-397b-a17b[:Label]' -> LLM admiral."""
    if spec.startswith("llm:"):
        parts = spec.split(":", 2)
        return LLMAdmiral(parts[1], label=parts[2] if len(parts) > 2 else None)
    return BOTS[spec]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bots", default="merchant,corsair,admiralty,turtle")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--ticks", type=int, default=None)
    ap.add_argument("--scenario", default=None, help="path to a scenario JSON")
    ap.add_argument("--out", default=None, help="replay JSON path")
    ap.add_argument("--html", default=None, help="self-contained viewer HTML path")
    args = ap.parse_args()

    specs = [b.strip() for b in args.bots.split(",")]
    players = []
    for spec in specs:
        bot = make_bot(spec)
        players.append([getattr(bot, "model_label", None) or spec, bot])
    from collections import Counter
    dupes = {n for n, c in Counter(p[0] for p in players).items() if c > 1}
    seen = {}
    for p in players:                       # same model twice -> Name-A, Name-B
        if p[0] in dupes:
            seen[p[0]] = seen.get(p[0], -1) + 1
            p[0] = f"{p[0]}-{chr(65 + seen[p[0]])}"
    scenario = json.load(open(args.scenario)) if args.scenario else None
    eng = Engine(players, seed=args.seed, max_ticks=args.ticks, scenario=scenario)
    result = eng.run()
    print(json.dumps(dict(seed=args.seed, **result)))

    if args.out or args.html:
        replay = eng.replay(result)
    if args.out:
        with open(args.out, "w") as fh:
            json.dump(replay, fh, separators=(",", ":"))
    if args.html:
        tpl_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "viewer", "index.html")
        with open(tpl_path) as fh:
            tpl = fh.read()
        blob = json.dumps(replay, separators=(",", ":"))
        with open(args.html, "w") as fh:
            fh.write(tpl.replace("/*EMBED_REPLAY*/null", blob, 1))


if __name__ == "__main__":
    main()
