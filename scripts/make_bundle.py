#!/usr/bin/env python3
"""Build a self-contained, spoiler-free series bundle HTML from a series dir."""
import argparse
import json
import os

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

ap = argparse.ArgumentParser()
ap.add_argument("--series-dir", required=True)
ap.add_argument("--name", required=True)
ap.add_argument("--out", required=True)
a = ap.parse_args()

s = json.load(open(os.path.join(a.series_dir, "series.json")))
games = []
for g in s["games"]:
    rp = json.load(open(os.path.join(a.series_dir, os.path.basename(g["file"]))))
    games.append({"game": g["game"], "replay": rp})
tpl = open(os.path.join(HERE, "viewer", "index.html")).read()
payload = json.dumps({"series": {"name": a.name, "games": games}},
                     separators=(",", ":"))
open(a.out, "w").write(tpl.replace("/*" + "EMBED_REPLAY" + "*/null", payload, 1))
print(f"{a.out}: {os.path.getsize(a.out) // 1048576}MB, {len(games)} games")
