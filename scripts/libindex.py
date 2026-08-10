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


_CACHE = {}                       # path -> (mtime, size, row) — parse each file once


def _row(path, rel, mtype):
    try:
        st = os.stat(path)
        key = (st.st_mtime, st.st_size)
        hit = _CACHE.get(path)
        if hit and hit[0] == key:
            row = dict(hit[1])
            row["type"] = mtype   # type can change (series rename); rest is content
            row["file"] = rel
            return row
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
    row = dict(when=when, type=mtype, seed=(rp.get("meta") or {}).get("seed"),
               scores={names[k]: v for k, v in sorted(scores.items())},
               winner=names.get(res.get("winner")),
               ticks=res.get("ticks") or (rp.get("frames") or [{}])[-1].get("t", 0),
               cost=round(cost, 3), file=rel)
    _CACHE[path] = (key, dict(row))
    return row


def matches_meta(lib):
    """Sidecar metadata for standalone matches (matches/matches-meta.json):
    {basename: {archived: bool, display_name: str}} — replay files are never
    rewritten for bookkeeping."""
    try:
        with open(os.path.join(lib, "matches", "matches-meta.json")) as fh:
            return json.load(fh)
    except Exception:
        return {}


def save_matches_meta(lib, meta):
    p = os.path.join(lib, "matches", "matches-meta.json")
    meta = {k: v for k, v in meta.items() if v}    # drop empty entries
    # unique temp name: concurrent savers sharing one .tmp could interleave
    # and publish corrupt JSON (server.py serializes with META_LOCK on top)
    tmp = f"{p}.{os.getpid()}.tmp"
    with open(tmp, "w") as fh:
        json.dump(meta, fh, indent=1)
    os.replace(tmp, p)


def build_index(lib):
    idx = dict(updated=datetime.datetime.utcnow().isoformat(),
               matches=[], series=[], tournaments=[], bundles=[])
    mdir = os.path.join(lib, "matches")
    meta = matches_meta(lib)               # archive flags + display names live
    if os.path.isdir(mdir):                # in a sidecar, never in the replays
        for fn in sorted(os.listdir(mdir)):
            if fn.endswith(".json") and fn != "matches-meta.json":
                row = _row(os.path.join(mdir, fn), f"replays/{fn}",
                           "mirror" if "mirror" in fn else "match")
                if row:
                    m = meta.get(fn, {})
                    row["archived"] = bool(m.get("archived"))
                    if m.get("display_name"):
                        row["display_name"] = m["display_name"]
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
            started = s.get("started")
            if not started:                # older series: first game file's mtime
                # over the FILTERED rows — a series.json may list games whose
                # files aren't on disk (continuations inherit rows), and a
                # missing file is OSError, not ValueError: this line once
                # crashed build_index and cascaded into failing healthy runs
                try:
                    started = min(os.path.getmtime(os.path.join(
                        sdir, name, os.path.basename(g["file"])))
                        for g in games)
                except (ValueError, OSError):
                    started = os.path.getmtime(spath)
            idx["series"].append(dict(name=name, display_name=s.get("display_name"),
                                      games=games, memos=s.get("memos", {}),
                                      partial=bool(s.get("partial")),
                                      cancelled=bool(s.get("cancelled")),
                                      archived=bool(s.get("archived")),
                                      started=started,
                                      started_utc=datetime.datetime
                                      .utcfromtimestamp(started)
                                      .strftime("%Y-%m-%d %H:%M")))
    tdir = os.path.join(lib, "tournaments")
    if os.path.isdir(tdir):
        for name in sorted(os.listdir(tdir)):
            tpath = os.path.join(tdir, name, "tournament.json")
            if not os.path.isfile(tpath):
                continue
            try:
                t = json.load(open(tpath))
            except Exception:
                continue
            idx["tournaments"].append(dict(
                name=name, file=f"tournaments/{name}/tournament.json",
                champion=t.get("champion"),
                format=(t.get("config", {}).get("tournament", {})
                        .get("format", "round_robin")),
                matchups=len(t.get("matchups", [])),
                partial=bool(t.get("partial")),
                cancelled=bool(t.get("cancelled")),
                archived=bool(t.get("archived"))))
            # every MATCHUP is also a series row, so in-flight tournament
            # series show on the Games page (⏳ live) and finished ones get
            # the normal spoiler-free Watch. Landed games are read from the
            # matchup DIRS — tournament.json only records a matchup once it
            # is decided, but its games stream in one by one.
            decided = {m.get("dir"): m for m in t.get("matchups", [])}
            for mdir in sorted(os.listdir(os.path.join(tdir, name))):
                mpath = os.path.join(tdir, name, mdir)
                if not os.path.isdir(mpath) or not mdir.startswith("m"):
                    continue
                games = []
                for gfn in sorted(fn for fn in os.listdir(mpath)
                                  if fn.startswith("g") and fn.endswith(".json")):
                    row = _row(os.path.join(mpath, gfn),
                               f"tournaments/{name}/{mdir}/{gfn}",
                               f"series:{name}/{mdir} {gfn[:-5]}")
                    if row:
                        idx["matches"].append(row)
                        games.append(dict(game=int(gfn[1:-5]) if
                                          gfn[1:-5].isdigit() else len(games) + 1,
                                          winner=row.get("winner"),
                                          file=f"tournaments/{name}/{mdir}/{gfn}"))
                if not games:
                    continue
                # "m01_Qwen35_v_Opus5" -> "Qwen35 vs Opus5"
                vs = mdir.split("_", 1)[-1].replace("_v_", " vs ")
                mu = decided.get(mdir)
                try:
                    started = min(os.path.getmtime(os.path.join(
                        mpath, os.path.basename(g["file"]))) for g in games)
                except (ValueError, OSError):
                    started = os.path.getmtime(mpath)
                idx["series"].append(dict(
                    name=f"{name}/{mdir}", display_name=f"🏆 {name}: {vs}",
                    tournament=name, games=sorted(games, key=lambda g: g["game"]),
                    partial=bool(t.get("partial")) and mu is None,
                    cancelled=bool(t.get("cancelled")) and mu is None,
                    # matchups inherit the TOURNAMENT's archive flag — the
                    # tournament is the unit you archive, never a lone matchup
                    archived=bool(t.get("archived")), started=started,
                    started_utc=datetime.datetime.utcfromtimestamp(started)
                    .strftime("%Y-%m-%d %H:%M")))
    bdir = os.path.join(lib, "bundles")
    if os.path.isdir(bdir):
        idx["bundles"] = [f"bundles/{fn}" for fn in sorted(os.listdir(bdir))
                          if fn.endswith(".html")]
    idx["matches"].sort(key=lambda m: m["when"], reverse=True)
    idx["series"].sort(key=lambda s: s.get("started", 0), reverse=True)
    with open(os.path.join(lib, "index.json"), "w") as fh:
        json.dump(idx, fh, indent=1)
    return idx


if __name__ == "__main__":
    lib = sys.argv[1] if len(sys.argv) > 1 else os.path.expanduser("~/flotilla-library")
    idx = build_index(lib)
    print(f"{len(idx['matches'])} matches, {len(idx['series'])} series, "
          f"{len(idx['tournaments'])} tournaments, {len(idx['bundles'])} bundles")
