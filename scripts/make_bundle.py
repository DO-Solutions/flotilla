#!/usr/bin/env python3
"""Build a self-contained, spoiler-free series bundle HTML from a series dir.

Importable (server.py's /api/bundle uses build_bundle) or CLI:
  make_bundle.py --series-dir <dir> --name <title> --out <file.html>
"""
import argparse
import json
import os

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def build_bundle(series_dir, name, out):
    s = json.load(open(os.path.join(series_dir, "series.json")))
    games = []
    for g in s["games"]:
        rp = json.load(open(os.path.join(series_dir, os.path.basename(g["file"]))))
        games.append({"game": g["game"], "replay": rp})
    tpl = open(os.path.join(HERE, "viewer", "index.html")).read()
    payload = json.dumps({"series": {"name": name, "games": games}},
                         separators=(",", ":"))
    with open(out, "w") as fh:
        fh.write(tpl.replace("/*" + "EMBED_REPLAY" + "*/null", payload, 1))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--series-dir", required=True)
    ap.add_argument("--name", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    build_bundle(a.series_dir, a.name, a.out)
    print(f"{a.out}: {os.path.getsize(a.out) // 1048576}MB")


if __name__ == "__main__":
    main()
