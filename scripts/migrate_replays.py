#!/usr/bin/env python3
"""One-time library migration to replay v3 (REPLAY_FORMAT.md).

Walks a library directory, finds every replay JSON whose meta.replay_version
is < 3, encodes its frame stream with sim/replay_codec, VERIFIES the encoded
stream decodes byte-identical to the original frames, and only then writes the
file back (atomic replace). A file that fails verification is left untouched
and reported — corruption is impossible by construction.

Skips: index/series/state metadata, checkpoints, bundles (self-contained
pages stay v1 and still load — the viewer dual-reads), anything unparseable.

Usage: python3 scripts/migrate_replays.py <library-dir> [--dry-run]
"""
import gzip
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "sim"))
import replay_codec

SKIP_NAMES = {"index.json", "series.json", "state.json", "job.json",
              "checkpoint.json", "reproduce.json"}


def is_replay(obj):
    return isinstance(obj, dict) and "frames" in obj and "result" in obj \
        and isinstance(obj.get("meta"), dict)


def migrate_file(path, dry):
    gz = path.endswith(".gz")
    opener = gzip.open if gz else open
    try:
        with opener(path, "rt") as fh:
            rp = json.load(fh)
    except Exception as e:
        return ("skip", f"unreadable ({e})")
    if not is_replay(rp):
        return ("skip", "not a replay")
    ver = rp["meta"].get("replay_version", 1)
    # v1 files also predate intent interning (~44% of raw bytes) — intern
    # whenever the table is absent, even on an already-v3 file
    # tolerate replay-shaped files without an events list — is_replay() only
    # requires frames/result/meta, and a bare rp["events"] aborted the whole
    # library walk with a KeyError
    needs_intern = "intern" not in rp["meta"] and any(
        e.get("k") == "intent" and isinstance(e.get("s"), str)
        for e in rp.get("events") or [])
    if ver >= 3 and not needs_intern:
        return ("ok", "already v3")
    changes = []
    if ver < 3:
        frames = rp["frames"]
        try:
            sf = replay_codec.ship_fleet_map(rp["events"])
            enc = replay_codec.encode_frames(frames)
            dec = replay_codec.decode_frames(enc, sf)
            a = json.dumps(frames, sort_keys=True, separators=(",", ":"))
            b = json.dumps(dec, sort_keys=True, separators=(",", ":"))
            if a != b:
                return ("FAIL", "round-trip diverged — file left untouched")
        except Exception as e:
            return ("FAIL", f"verify error ({type(e).__name__}: {e}) — untouched")
        changes.append(f"v{ver} -> v3, frames {len(a)} -> "
                       f"{len(json.dumps(enc, separators=(',', ':')))} bytes")
    if needs_intern:
        intern, imap, ev_out = [], {}, []
        for e in rp["events"]:
            if e.get("k") == "intent" and isinstance(e.get("s"), str):
                i = imap.get(e["s"])
                if i is None:
                    i = imap[e["s"]] = len(intern)
                    intern.append(e["s"])
                ev_out.append({**e, "s": i})
            else:
                ev_out.append(e)
        # verify: de-referencing reproduces the original events exactly.
        # Only de-reference events THIS run rewrote (identity check) — a
        # mixed file whose intent events already carried int s crashed the
        # walk indexing them into the new table
        back = [{**e1, "s": intern[e1["s"]]}
                if e1 is not e0 and e1.get("k") == "intent"
                and isinstance(e1.get("s"), int)
                else e1
                for e0, e1 in zip(rp["events"], ev_out)]
        if json.dumps(back, sort_keys=True) != json.dumps(rp["events"],
                                                          sort_keys=True):
            return ("FAIL", "intern round-trip diverged — untouched")
        changes.append(f"interned {len(intern)} intent strings")
    if dry:
        return ("would-migrate", "; ".join(changes))
    if ver < 3:
        rp["frames"] = enc
        rp["meta"]["replay_version"] = 3
    if needs_intern:
        rp["events"] = ev_out
        rp["meta"]["intern"] = intern
    tmp = path + ".tmp"
    st0 = os.stat(path)
    with (gzip.open(tmp, "wt") if gz else open(tmp, "w")) as fh:
        json.dump(rp, fh, separators=(",", ":"))
    os.replace(tmp, path)                          # atomic vs readers
    # keep the original mtime: the library index dates every match from file
    # mtime, so a migration without this collapsed the whole library's
    # chronology onto the migration timestamp
    os.utime(path, (st0.st_atime, st0.st_mtime))
    return ("migrated", "; ".join(changes))


def main():
    args = [a for a in sys.argv[1:] if a != "--dry-run"]
    dry = "--dry-run" in sys.argv
    if len(args) != 1:
        print(__doc__)
        sys.exit(2)
    root = args[0]
    counts = {}
    fails = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames
                       if d not in ("bundles", "_work", "showcase")]
        for fn in sorted(filenames):
            if fn in SKIP_NAMES or not (fn.endswith(".json")
                                        or fn.endswith(".json.gz")):
                continue
            path = os.path.join(dirpath, fn)
            state, msg = migrate_file(path, dry)
            counts[state] = counts.get(state, 0) + 1
            rel = os.path.relpath(path, root)
            if state in ("migrated", "would-migrate", "FAIL"):
                print(f"  {state}: {rel} — {msg}")
            if state == "FAIL":
                fails.append(rel)
    print("summary:", ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))
          or "nothing found")
    if fails:
        print(f"{len(fails)} file(s) FAILED verification and were left "
              "untouched — investigate before rerunning")
        sys.exit(1)


if __name__ == "__main__":
    main()
