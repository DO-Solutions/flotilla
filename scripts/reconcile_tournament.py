#!/usr/bin/env python3
"""Rebuild a tournament's bracket records from the game replays on disk.

tournament.json is written after every matchup, so when a worker dies
mid-tournament the record lags the library: games exist in matchup dirs that
no matchup record mentions (champions-cup-2 shipped m03 and m08 games home
whose records never landed). This walks the matchup dirs, rebuilds every
record from the replays themselves, and recomputes the standings.

Deliberately conservative:
- an existing record that already covers its on-disk games is kept verbatim;
- a rebuilt matchup gets a winner ONLY if the games mathematically decide it
  (the same clinch rule the runner uses) — otherwise winner stays null and
  the record is marked partial;
- the champion field is never invented. It survives if present; deciding a
  champion from a reconciled bracket is a human call.

Importable (server.py's /api/reconcile-tournament uses reconcile) or CLI:
  reconcile_tournament.py <tournament-dir> [--write]
Without --write it prints what would change and touches nothing.

To stitch an externally re-run series into a bracket: copy its g*.json into
the matchup dir (tournaments/<t>/mNN_A_v_B/), then reconcile.
"""
import argparse
import json
import os
import re


def _rows_from_dir(mdir, reldir):
    rows = []
    for fn in sorted(os.listdir(mdir)):
        m = re.match(r"g(\d+)\.json$", fn)
        if not m:
            continue
        try:
            with open(os.path.join(mdir, fn)) as fh:
                rp = json.load(fh)
            res = rp["result"]
            names = res["names"]
            scores = {names[k]: v for k, v in res["scores"].items()}
            w = res.get("winner")
            winner = names.get(str(w)) if w is not None else None
            rows.append(dict(game=int(m.group(1)),
                             seed=rp.get("meta", {}).get("seed"),
                             file=os.path.join(reldir, fn),
                             scores=scores, winner=winner))
        except (OSError, ValueError, KeyError):
            continue                     # an unreadable replay is skipped, not fatal
    rows.sort(key=lambda r: r["game"])
    return rows


def _decided(rows, players, games_per_match):
    """The runner's clinch rule: a winner only when the lead can no longer be
    caught OR TIED with every remaining game. A full series decides by
    (game wins, total score)."""
    wins = {n: sum(1 for r in rows if r["winner"] == n) for n in players}
    totals = {n: sum(r["scores"].get(n, 0) for r in rows) for n in players}
    remaining = max(0, games_per_match - len(rows))
    ranked = sorted(wins.values(), reverse=True) or [0]
    lead = ranked[0]
    second = ranked[1] if len(ranked) > 1 else 0
    if lead > second + remaining:
        return max(players, key=lambda n: (wins[n], totals[n]))
    return None


def reconcile(tdir):
    """Return (tournament_dict, changes) — changes is a list of human-readable
    strings; empty means the record already matches the library."""
    tj_path = os.path.join(tdir, "tournament.json")
    tj = {}
    if os.path.isfile(tj_path):
        try:
            with open(tj_path) as fh:
                tj = json.load(fh)
        except (OSError, ValueError):
            tj = {}
    cfg = tj.get("config") or {}
    gpm = int(((cfg.get("tournament") or {}).get("games_per_match")) or 1)
    recs = {m.get("dir"): m for m in tj.get("matchups", []) if m.get("dir")}
    changes = []
    out = []
    for d in sorted(os.listdir(tdir) if os.path.isdir(tdir) else []):
        mdir = os.path.join(tdir, d)
        if not (os.path.isdir(mdir) and re.match(r"m\d+_", d)):
            continue
        rows = _rows_from_dir(mdir, d)
        old = recs.pop(d, None)
        if not rows:
            if old:
                out.append(old)
            continue
        if old and len(old.get("games", [])) >= len(rows):
            out.append(old)              # record already covers the disk
            continue
        players = old.get("players") if old else None
        if not players:
            # every fleet name seen across this matchup's replays
            seen = []
            for r in rows:
                for n in r["scores"]:
                    if n not in seen:
                        seen.append(n)
            players = seen
        winner = _decided(rows, players, gpm)
        rec = {"round": (old or {}).get("round", 1), "players": players,
               "dir": d, "games": rows, "winner": winner, "rebuilt": True}
        if winner is None:
            rec["partial"] = True
        out.append(rec)
        changes.append(f"{d}: {'rebuilt' if not old else 'extended'} from "
                       f"{len(rows)} on-disk games"
                       + ("" if winner else " (undecided — no winner set)"))
    for d, old in recs.items():          # records whose dirs vanished: keep
        out.append(old)
    out.sort(key=lambda m: m.get("dir", ""))
    names = sorted({n for m in out for n in m.get("players", [])})
    standings = {n: {"series_wins": 0, "wins": 0, "games": 0, "score": 0}
                 for n in names}
    for m in out:
        if m.get("winner"):
            standings[m["winner"]]["series_wins"] += 1
        for n in m.get("players", []):
            rows = m.get("games", [])
            standings[n]["games"] += len(rows)
            standings[n]["wins"] += sum(1 for r in rows if r["winner"] == n)
            standings[n]["score"] += sum(r["scores"].get(n, 0) for r in rows)
    new = dict(tj, matchups=out, standings=standings)
    if tj.get("standings") != standings or \
            [m.get("dir") for m in tj.get("matchups", [])] != \
            [m.get("dir") for m in out] or changes:
        if not changes and tj.get("standings") != standings:
            changes.append("standings recomputed from the records")
    return new, changes


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("tdir", help="library tournament dir "
                    "(holds tournament.json + m*/ matchup dirs)")
    ap.add_argument("--write", action="store_true",
                    help="write the reconciled tournament.json "
                    "(default: dry-run, print only)")
    a = ap.parse_args()
    new, changes = reconcile(a.tdir)
    for c in changes:
        print(c)
    if not changes:
        print("record already matches the library — nothing to do")
        return
    if a.write:
        tmp = os.path.join(a.tdir, "tournament.json.tmp")
        with open(tmp, "w") as fh:
            json.dump(new, fh, indent=1)
        os.replace(tmp, os.path.join(a.tdir, "tournament.json"))
        print("tournament.json written")
    else:
        print("(dry-run — pass --write to apply)")


if __name__ == "__main__":
    main()
