#!/usr/bin/env python3
"""Build the library index (index.json) from a library directory.

Library layout:
  matches/*.json            standalone match replays
  series/<name>/g*.json     series games (+ series.json with games/memos/display_name)
  tournaments/<name>/       tournament.json + m*/g*.json matchup dirs
  bundles/*.html            pre-built shareable pages (served as-is)

Shared by the local/droplet server (serves the library directly) and the legacy
bucket publisher. File refs use the site-relative "replays/..." form the player
and dashboard expect.
"""
import datetime
import json
import os
import sys


def _row(path, rel, mtype):
    try:
        rp = json.load(open(path))
    except Exception as e:
        print(f"libindex: skip {path}: {e}", file=sys.stderr)
        return None
    res = rp.get("result") or {}
    names = {int(k) if isinstance(k, str) else k: v
             for k, v in (res.get("names") or {}).items()}
    scores = {int(k) if isinstance(k, str) else k: v
              for k, v in (res.get("scores") or {}).items()}
    cost = sum((d.get("u") or {}).get("cost", 0) for d in rp.get("decisions", []))
    when = datetime.datetime.utcfromtimestamp(os.path.getmtime(path)).isoformat()
    return dict(when=when, type=mtype, seed=(rp.get("meta") or {}).get("seed"),
                scores={names[k]: v for k, v in sorted(scores.items())},
                winner=names.get(res.get("winner")),
                ticks=res.get("ticks") or (rp.get("frames") or [{}])[-1].get("t", 0),
                cost=round(cost, 3), file=rel)


def build_index(lib):
    idx = dict(updated=datetime.datetime.utcnow().isoformat(),
               matches=[], series=[], tournaments=[], bundles=[])
    mdir = os.path.join(lib, "matches")
    if os.path.isdir(mdir):
        for fn in sorted(os.listdir(mdir)):
            if fn.endswith(".json"):
                row = _row(os.path.join(mdir, fn), f"replays/{fn}",
                           "mirror" if "mirror" in fn else "match")
                if row:
                    idx["matches"].append(row)
    sdir = os.path.join(lib, "series")
    if os.path.isdir(sdir):
        for name in sorted(os.listdir(sdir)):
            spath = os.path.join(sdir, name, "series.json")
            if not os.path.isfile(spath):
                continue
            try:
                s = json.load(open(spath))
            except Exception:
                continue
            games = []
            for g in s.get("games", []):
                gfn = os.path.basename(g["file"])
                row = _row(os.path.join(sdir, name, gfn), f"replays/{name}/{gfn}",
                           f"series:{name} g{g['game']}")
                if row:
                    idx["matches"].append(row)
                    games.append(dict(game=g["game"], winner=g["winner"],
                                      file=f"replays/{name}/{gfn}"))
            idx["series"].append(dict(name=name, display_name=s.get("display_name"),
                                      games=games, memos=s.get("memos", {}),
                                      partial=bool(s.get("partial"))))
    tdir = os.path.join(lib, "tournaments")
    if os.path.isdir(tdir):
        for name in sorted(os.listdir(tdir)):
            tpath = os.path.join(tdir, name, "tournament.json")
            if os.path.isfile(tpath):
                try:
                    t = json.load(open(tpath))
                except Exception:
                    continue
                idx["tournaments"].append(dict(
                    name=name, file=f"tournaments/{name}/tournament.json",
                    champion=t.get("champion"),
                    format=(t.get("config", {}).get("tournament", {})
                            .get("format", "round_robin")),
                    matchups=len(t.get("matchups", []))))
    bdir = os.path.join(lib, "bundles")
    if os.path.isdir(bdir):
        idx["bundles"] = [f"bundles/{fn}" for fn in sorted(os.listdir(bdir))
                          if fn.endswith(".html")]
    idx["matches"].sort(key=lambda m: m["when"], reverse=True)
    with open(os.path.join(lib, "index.json"), "w") as fh:
        json.dump(idx, fh, indent=1)
    return idx


if __name__ == "__main__":
    lib = sys.argv[1] if len(sys.argv) > 1 else os.path.expanduser("~/flotilla-library")
    idx = build_index(lib)
    print(f"{len(idx['matches'])} matches, {len(idx['series'])} series, "
          f"{len(idx['tournaments'])} tournaments, {len(idx['bundles'])} bundles")
