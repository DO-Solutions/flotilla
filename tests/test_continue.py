"""Game-boundary continuation: cfg["continue"] resumes a series at game N with
inherited rows + memos and NO checkpoint — the recovery path when a worker died
between checkpoints (born from the age-cap reaping of domination-5 take 3).
Also: the memos_saved marker must print AFTER a game file gains its memos."""
import json
import os
import subprocess
import sys
import tempfile
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "sim"))
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def run_cfg(cfg, outdir):
    cfgpath = os.path.join(outdir, "cfg.json")
    json.dump(dict(cfg, outdir=outdir), open(cfgpath, "w"))
    argv = [sys.executable, os.path.join(HERE, "sim", "run_config.py"), cfgpath]
    return subprocess.run(argv, capture_output=True, text=True, timeout=300)


BASE = {"mode": "series", "seed": 91, "bots": ["merchant", "corsair"],
        "scenario": {"width": 64, "height": 36, "max_ticks": 1200,
                     "role_fallback": True, "warmup": False}}


def main():
    # control: a full 3-game series in one run
    a = tempfile.mkdtemp(prefix="flotilla-cont-a-")
    r = run_cfg(dict(BASE, series={"games": 3, "memos": False}), a)
    assert r.returncode == 0, r.stdout[-500:] + r.stderr[-500:]
    ser_a = json.load(open(os.path.join(a, "series.json")))
    assert [g["game"] for g in ser_a["games"]] == [1, 2, 3]

    # experiment: continue at game 3 with games 1-2's rows inherited
    rows = []
    for line in r.stdout.splitlines():
        if '"winner"' in line:
            row = json.loads(line)
            if row.get("game") in (None, 1, 2) and len(rows) < 2:
                rows.append(dict(row, game=len(rows) + 1))
    assert len(rows) == 2, f"expected 2 result rows, got {len(rows)}"
    b = tempfile.mkdtemp(prefix="flotilla-cont-b-")
    cont = dict(BASE, series={"games": 3, "memos": False},
                **{"continue": {"game": 3, "rows": rows}})
    r2 = run_cfg(cont, b)
    assert r2.returncode == 0, r2.stdout[-500:] + r2.stderr[-500:]
    assert os.path.isfile(os.path.join(b, "g3.json")), "continuation g3 missing"
    assert not os.path.exists(os.path.join(b, "g1.json")), \
        "continuation must NOT replay finished games"
    ser_b = json.load(open(os.path.join(b, "series.json")))
    assert [g["game"] for g in ser_b["games"]] == [1, 2, 3]
    assert [g["winner"] for g in ser_b["games"]] == \
        [g["winner"] for g in ser_a["games"]], "inherited rows corrupted"

    # determinism: scripted bots are stateless, so the continuation's g3 must
    # equal the control's g3 frame-for-frame
    g3a = json.load(open(os.path.join(a, "g3.json")))
    g3b = json.load(open(os.path.join(b, "g3.json")))
    for k in ("frames", "events", "scores"):
        assert json.dumps(g3a.get(k), sort_keys=True) == \
            json.dumps(g3b.get(k), sort_keys=True), f"g3 {k} diverged"

    # memos_saved marker: prints once per game, AFTER the file write
    c = tempfile.mkdtemp(prefix="flotilla-cont-c-")
    r3 = run_cfg(dict(BASE, series={"games": 2, "memos": True}), c)
    assert r3.returncode == 0, r3.stdout[-500:] + r3.stderr[-500:]
    marks = [json.loads(x) for x in r3.stdout.splitlines()
             if '"memos_saved"' in x]
    assert [m["memos_saved"] for m in marks] == [1, 2], marks
    for m in marks:
        assert "memos" in json.load(open(m["file"])), \
            "marker printed but file has no memos"

    # debrief_full_info is DEATH-LIFTED: FULL_PICTURE appears only for an
    # admiral that was ELIMINATED, and only from its death onward. Build a
    # tiny replay with a known death to check both the present + absent cases.
    from series import digest_for
    rp = {"result": {"names": {0: "A", 1: "B"}, "scores": {0: 5, 1: 9},
                     "ticks": 6000, "winner": 1},
          "meta": {"config": {"window": 100}},
          "decisions": [],
          "events": [{"k": "flag_sunk", "fleet": 0, "by": 1, "t": 3050},
                     {"k": "spawn", "fleet": 1, "preset": "raider", "t": 10}],
          "frames": [{"t": t, "f": [[0, 100, 10, 0, 5, t < 3050],
                                    [1, 100, 20, 40, 9, 1]]}
                     for t in range(0, 6001, 1000)]}
    dead = json.loads(digest_for(rp, 0, 1, 1, full_info=True))     # fleet 0 died
    fp = dead.get("FULL_PICTURE")
    assert fp and set(fp["fleets"]) == {"A", "B"}, fp
    # timeline starts at the kill window (3000), not before — pre-death stays fogged
    assert fp["economy_timeline"] and fp["economy_timeline"][0]["t"] == 3000, \
        [p["t"] for p in fp["economy_timeline"]]
    survivor = json.loads(digest_for(rp, 1, 1, 1, full_info=True))  # fleet 1 lived
    assert "FULL_PICTURE" not in survivor, "survivor's fog must stay down"
    assert "FULL_PICTURE" not in json.loads(digest_for(rp, 0, 1, 1)), \
        "default digest must stay fogged"

    print("continuation OK: inherited rows + fresh g3 identical to control; "
          "memos_saved markers correct")


if __name__ == "__main__":
    main()
