"""Pause/resume: a checkpointed run must finish HASH-IDENTICAL to an
uninterrupted one — the spot-instance workflow depends on lossless freezing."""
import hashlib
import json
import os
import subprocess
import sys
import tempfile
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "sim"))
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def run_cfg(cfg, outdir, resume=False):
    cfgpath = os.path.join(outdir, "cfg.json")
    if not resume:
        json.dump(dict(cfg, outdir=outdir), open(cfgpath, "w"))
    argv = [sys.executable, os.path.join(HERE, "sim", "run_config.py")] + \
        (["--resume", outdir] if resume else [cfgpath])
    return subprocess.run(argv, capture_output=True, text=True, timeout=300)


def series_hash(outdir, games):
    h = hashlib.sha256()
    for g in range(1, games + 1):
        rp = json.load(open(os.path.join(outdir, f"g{g}.json")))
        rp["meta"]["config"].pop("rules", None)
        h.update(json.dumps(rp, sort_keys=True,
                            separators=(",", ":")).encode())
    return h.hexdigest()


def main():
    cfg = {"mode": "series", "seed": 33, "bots": ["merchant", "corsair"],
           "scenario": {"width": 64, "height": 36, "max_ticks": 1200,
                        "role_fallback": True, "warmup": False},
           "series": {"games": 2, "memos": False}}

    # control: uninterrupted
    a = tempfile.mkdtemp(prefix="flotilla-pause-a-")
    r = run_cfg(cfg, a)
    assert r.returncode == 0, r.stdout[-500:] + r.stderr[-500:]
    ha = series_hash(a, 2)

    # experiment: pause the run mid-game-1 (flag pre-planted: the engine hits
    # it at its first window boundary), then resume to completion
    b = tempfile.mkdtemp(prefix="flotilla-pause-b-")
    os.makedirs(b, exist_ok=True)
    open(os.path.join(b, "pause.flag"), "w").write("1")
    r = run_cfg(cfg, b)
    assert r.returncode == 75, f"pause must exit 75, got {r.returncode}: " \
        + r.stdout[-300:] + r.stderr[-300:]
    ckpath = os.path.join(b, "checkpoint.json")
    assert os.path.isfile(ckpath), "no checkpoint"
    json.load(open(ckpath))                   # the format IS plain JSON
    assert not os.path.exists(os.path.join(b, "pause.flag")), "flag not consumed"
    paused = json.loads([x for x in r.stdout.splitlines()
                         if '"paused"' in x][-1])
    assert paused["game"] == 1 and paused["t"] > 0

    # VERSION TOLERANCE, forward: fields from a build this code has never
    # heard of must be ignored without complaint — and change nothing
    ck = json.load(open(ckpath))
    ck["engine"]["from_the_future"] = {"quantum": 1}
    ck["engine"]["ships"][0]["future_field"] = 9
    ck["engine"]["fleets"][0]["wormhole"] = True
    json.dump(ck, open(ckpath, "w"), separators=(",", ":"))

    r = run_cfg(cfg, b, resume=True)
    assert r.returncode == 0, r.stdout[-500:] + r.stderr[-500:]
    assert not os.path.exists(ckpath), \
        "checkpoint must be consumed on completion"
    hb = series_hash(b, 2)
    assert ha == hb, "resumed run diverged from the uninterrupted control"

    # VERSION TOLERANCE, backward: a checkpoint MISSING fields (older build)
    # must still thaw — absent fields keep current-code defaults
    d = tempfile.mkdtemp(prefix="flotilla-pause-d-")
    open(os.path.join(d, "pause.flag"), "w").write("1")
    r = run_cfg(cfg, d)
    assert r.returncode == 75
    ckd = os.path.join(d, "checkpoint.json")
    ck = json.load(open(ckd))
    for s in ck["engine"]["ships"]:
        s.pop("moved_t", None)                # ship field an old build lacked
        s.pop("assault_flag", None)
    for f in ck["engine"]["fleets"]:
        f.pop("recent_hits", None)            # fleet field
        f.pop("combat", None)
    ck["engine"].pop("last_snap", None)       # engine field
    ck["engine"].pop("island_specs", None)
    for bt in ck["bots"]:
        if bt.get("state"):
            bt["state"].pop("memo_cut", None)  # bot field
    json.dump(ck, open(ckd, "w"), separators=(",", ":"))
    r = run_cfg(cfg, d, resume=True)
    assert r.returncode == 0, "old-build checkpoint must still resume: " \
        + r.stdout[-400:] + r.stderr[-400:]
    for g in (1, 2):
        assert os.path.isfile(os.path.join(d, f"g{g}.json")), \
            "tolerant resume must still finish the series"

    # double pause: pause again after resume start also works (re-freeze)
    c = tempfile.mkdtemp(prefix="flotilla-pause-c-")
    open(os.path.join(c, "pause.flag"), "w").write("1")
    r = run_cfg(cfg, c)
    assert r.returncode == 75
    open(os.path.join(c, "pause.flag"), "w").write("1")
    r = run_cfg(cfg, c, resume=True)
    assert r.returncode == 75, "re-pause of a resumed run must checkpoint again"
    r = run_cfg(cfg, c, resume=True)
    assert r.returncode == 0
    assert series_hash(c, 2) == ha, "twice-paused run diverged"

    # a CORRUPT checkpoint must fail the resume WITHOUT eating the file —
    # the runner only removes checkpoint.json after the run completes
    e = tempfile.mkdtemp(prefix="flotilla-pause-e-")
    open(os.path.join(e, "pause.flag"), "w").write("1")
    r = run_cfg(cfg, e)
    assert r.returncode == 75
    cke = os.path.join(e, "checkpoint.json")
    with open(cke, "w") as fh:
        fh.write('{"truncated": ')
    r = run_cfg(cfg, e, resume=True)
    assert r.returncode not in (0, 75), "corrupt checkpoint must fail loudly"
    assert os.path.isfile(cke), \
        "a FAILED resume must never delete the checkpoint"

    # rich-state round-trip, in process: territory + islands + contacts +
    # combat + a conn program mid-flight, frozen to a JSON STRING and thawed,
    # must finish hash-identical to the uninterrupted control
    from core import Engine
    import conn
    from bots import BOTS
    scen = {"width": 72, "height": 40, "max_ticks": 2400, "win": "territory",
            "island_coverage": 6, "role_fallback": True, "warmup": False}
    players = lambda: [("Redbeard", BOTS["corsair"]),
                       ("Kidd", BOTS["corsair"])]
    prog = conn.compile_program(
        "when self.hull_pct < 30: helm.home()\ndefault: helm.gather()")

    def arm(engine):                       # identical mid-game state, both runs
        s = next(iter(engine.ships.values()))
        s.program = prog
        s.pmem = prog.init_mem()
        return engine

    e1 = arm(Engine(players(), seed=91, scenario=scen))
    h1 = hashlib.sha256(json.dumps(
        e1.replay(e1.run()), sort_keys=True,
        separators=(",", ":")).encode()).hexdigest()

    e2 = arm(Engine(players(), seed=91, scenario=scen))
    assert e2.run(pause_check=lambda: e2.t >= 800) is None, "must pause"
    assert any(f.contacts for f in e2.fleets.values()), \
        "coverage: freeze must happen with live contact plots"
    frozen = json.loads(json.dumps(e2.freeze()))     # a REAL json round-trip
    e3 = Engine.thaw(frozen, players())
    h2 = hashlib.sha256(json.dumps(
        e3.replay(e3.run()), sort_keys=True,
        separators=(",", ":")).encode()).hexdigest()
    assert h1 == h2, "freeze->JSON->thaw diverged from the control run"
    # the pipelined flag must derive identically for fresh AND resumed runs
    # (a resumed pipelined run once silently dropped to the 3-strike streak)
    import run_config
    os.environ.pop("FLOTILLA_PIPELINED", None)
    run_config._pipelined_env({"pipeline_depth": 0})
    assert "FLOTILLA_PIPELINED" not in os.environ, "depth 0 must not flag"
    run_config._pipelined_env({"pipeline_depth": 2})
    assert os.environ.get("FLOTILLA_PIPELINED") == "1", \
        "pipelined scenario must set the flag (resume path uses this helper)"
    os.environ.pop("FLOTILLA_PIPELINED", None)
    print("PASS test_pause")


if __name__ == "__main__":
    main()
