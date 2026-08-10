"""migrate_replays: the in-place library migrator (wringer pass 3). Covers
the pass-1 fixes — mtime preservation (the index dates matches from file
mtime; a migration without it collapsed the library's chronology), the
missing-events KeyError, and the mixed already-interned file that crashed
the verify step. v1→v3 frame conversion itself is covered by test_replay_v3
and the jsdom gold tests."""
import gzip
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "scripts"))
sys.path.insert(0, os.path.join(HERE, "..", "sim"))
import migrate_replays as mig

FAILS = []


def ok(cond, msg):
    if cond:
        print(f"PASS {msg}")
    else:
        FAILS.append(msg)
        print(f"FAIL {msg}")


def replay(events):
    return {"meta": {"seed": 1, "replay_version": 3},
            "result": {"winner": 0, "names": {"0": "A"}, "scores": {"0": 1}},
            "frames": [], "events": events}


OLD_MTIME = 1_600_000_000.0


def put(td, name, rp, gz=False):
    p = os.path.join(td, name)
    if gz:
        with gzip.open(p, "wt") as fh:
            json.dump(rp, fh)
    else:
        with open(p, "w") as fh:
            json.dump(rp, fh)
    os.utime(p, (OLD_MTIME, OLD_MTIME))
    return p


with tempfile.TemporaryDirectory() as td:
    # 1. plain string-intent file: interned, and the mtime SURVIVES
    p1 = put(td, "a.json", replay([{"k": "intent", "s": "hold the line"},
                                   {"k": "sink", "ship": 1},
                                   {"k": "intent", "s": "hold the line"}]))
    st, note = mig.migrate_file(p1, dry=False)
    ok(st == "migrated" and "interned" in note, f"string intents interned ({note})")
    ok(abs(os.path.getmtime(p1) - OLD_MTIME) < 1,
       "migration preserves the file mtime (library chronology)")
    d1 = json.load(open(p1))
    ok(d1["meta"]["intern"] == ["hold the line"]
       and [e.get("s") for e in d1["events"] if e["k"] == "intent"] == [0, 0],
       "intern table + int refs written")

    # 2. gz variant round-trips the same way
    p2 = put(td, "b.json.gz", replay([{"k": "intent", "s": "raid"}]), gz=True)
    st, note = mig.migrate_file(p2, dry=False)
    d2 = json.load(gzip.open(p2, "rt"))
    ok(st == "migrated" and d2["meta"]["intern"] == ["raid"],
       "gz files migrate in place")
    ok(abs(os.path.getmtime(p2) - OLD_MTIME) < 1, "gz mtime preserved too")

    # 3. already interned: untouched
    p3 = put(td, "c.json", dict(replay([{"k": "intent", "s": 0}]),
                                meta={"seed": 1, "replay_version": 3,
                                      "intern": ["x"]}))
    before = open(p3).read()
    st, note = mig.migrate_file(p3, dry=False)
    ok(st == "ok" and open(p3).read() == before, "already-v3+interned untouched")

    # 4. MIXED file (some int, some str intents) — the verify step used to
    #    index the pre-interned ints into the NEW table and crash the walk
    p4 = put(td, "d.json", replay([{"k": "intent", "s": 5},
                                   {"k": "intent", "s": "fresh order"}]))
    st, note = mig.migrate_file(p4, dry=False)
    ok(st in ("migrated", "FAIL"),
       f"mixed intern file never raises out of migrate_file ({st})")

    # 5. replay-shaped file with NO events list — used to KeyError and abort
    #    the whole library walk
    rp5 = replay([])
    rp5.pop("events")
    rp5["meta"]["replay_version"] = 3
    p5 = put(td, "e.json", rp5)
    st, note = mig.migrate_file(p5, dry=False)
    ok(st in ("ok", "skip"), f"events-less file handled gracefully ({st})")

    # 6. dry run writes nothing
    p6 = put(td, "f.json", replay([{"k": "intent", "s": "probe"}]))
    before = open(p6).read()
    st, note = mig.migrate_file(p6, dry=True)
    ok(st == "would-migrate" and open(p6).read() == before,
       "dry run reports without writing")

print(f"FAILURES: {len(FAILS)}")
sys.exit(1 if FAILS else 0)
