#!/usr/bin/env python3
"""Publish the replay library to the dashboard bucket.

Scans a library dir for replay JSONs (matches) and series dirs (series.json + g*.json),
builds index.json, then rclone-syncs everything + the dashboard pages to an S3-style
bucket, for pull-based static hosting setups. (If you run server.py, you don't need
this — the server serves the library directly.)

Usage: python3 scripts/publish.py --library ~/flotilla-library [--remote flotilla:my-bucket]
Layout in the library dir:
  matches/*.json         one replay per file (from run_match --out / batch.py)
  series/<name>/         series.json + g1.json g2.json ... (from series.py --outdir)
"""
import argparse
import datetime
import json
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def match_row(path, rel, mtype):
    try:
        rp = json.load(open(path))
    except Exception as e:
        print(f"skip {path}: {e}", file=sys.stderr)
        return None
    res = rp.get("result") or {}
    names = res.get("names") or {}
    names = {int(k) if isinstance(k, str) else k: v for k, v in names.items()}
    scores = {int(k) if isinstance(k, str) else k: v
              for k, v in (res.get("scores") or {}).items()}
    cost = sum((d.get("u") or {}).get("cost", 0) for d in rp.get("decisions", []))
    when = datetime.datetime.utcfromtimestamp(os.path.getmtime(path)).isoformat()
    return dict(
        when=when, type=mtype, seed=(rp.get("meta") or {}).get("seed"),
        scores={names[k]: v for k, v in sorted(scores.items())},
        winner=names.get(res.get("winner")),
        ticks=res.get("ticks") or (rp.get("frames") or [{}])[-1].get("t", 0),
        cost=round(cost, 3), file=rel)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--library", required=True)
    ap.add_argument("--remote", required=True, help="rclone remote:bucket")
    ap.add_argument("--no-upload", action="store_true")
    args = ap.parse_args()

    lib = os.path.expanduser(args.library)
    stage = os.path.join(lib, "_site")
    if os.path.isdir(stage):
        shutil.rmtree(stage)
    os.makedirs(os.path.join(stage, "replays"), exist_ok=True)

    idx = dict(updated=datetime.datetime.utcnow().isoformat(), matches=[], series=[])
    mdir = os.path.join(lib, "matches")
    if os.path.isdir(mdir):
        for fn in sorted(os.listdir(mdir)):
            if not fn.endswith(".json"):
                continue
            rel = f"replays/{fn}"
            row = match_row(os.path.join(mdir, fn), rel,
                            "mirror" if "mirror" in fn else "match")
            if row:
                idx["matches"].append(row)
                shutil.copy2(os.path.join(mdir, fn), os.path.join(stage, rel))
    sdir = os.path.join(lib, "series")
    if os.path.isdir(sdir):
        for name in sorted(os.listdir(sdir)):
            spath = os.path.join(sdir, name, "series.json")
            if not os.path.isfile(spath):
                continue
            s = json.load(open(spath))
            os.makedirs(os.path.join(stage, "replays", name), exist_ok=True)
            games = []
            for g in s.get("games", []):
                gfn = os.path.basename(g["file"])
                rel = f"replays/{name}/{gfn}"
                src = os.path.join(sdir, name, gfn)
                row = match_row(src, rel, f"series:{name} g{g['game']}")
                if row:
                    idx["matches"].append(row)
                    games.append(dict(game=g["game"], winner=g["winner"], file=rel))
                    rp = json.load(open(src))
                    if "memos" not in rp:             # older series: graft from series.json
                        gm = {}
                        for player, entries in (s.get("memos") or {}).items():
                            for e in entries:
                                if e.get("after_game") == g["game"]:
                                    gm[player] = {"memo": e.get("memo", ""),
                                                  "err": e.get("err")}
                        if gm:
                            rp["memos"] = gm
                    with open(os.path.join(stage, rel), "w") as fh:
                        json.dump(rp, fh, separators=(",", ":"))
            idx["series"].append(dict(name=name, display_name=s.get("display_name"),
                                      games=games, memos=s.get("memos", {})))

    bdir = os.path.join(lib, "bundles")     # pre-built shareable bundles, served as-is
    if os.path.isdir(bdir):
        os.makedirs(os.path.join(stage, "bundles"), exist_ok=True)
        for fn in sorted(os.listdir(bdir)):
            if fn.endswith(".html"):
                shutil.copy2(os.path.join(bdir, fn), os.path.join(stage, "bundles", fn))
                idx["bundles"] = idx.get("bundles", []) + [f"bundles/{fn}"]

    idx["matches"].sort(key=lambda m: m["when"], reverse=True)
    with open(os.path.join(stage, "index.json"), "w") as fh:
        json.dump(idx, fh, indent=1)
    sys.path.insert(0, os.path.join(HERE, "sim"))
    import config_schema
    with open(os.path.join(stage, "config-schema.json"), "w") as fh:
        fh.write(config_schema.schema_json())
    with open(os.path.join(stage, "CONFIG.md"), "w") as fh:
        fh.write(config_schema.config_md())
    shutil.copy2(os.path.join(HERE, "dash", "dashboard.html"),
                 os.path.join(stage, "index.html"))
    shutil.copy2(os.path.join(HERE, "viewer", "index.html"),
                 os.path.join(stage, "player.html"))
    print(f"staged {len(idx['matches'])} matches, {len(idx['series'])} series -> {stage}")

    if not args.no_upload:
        subprocess.run(["rclone", "sync", stage, args.remote,
                        "--s3-acl", "private", "-q"], check=True)
        print(f"synced to {args.remote}")


if __name__ == "__main__":
    main()
