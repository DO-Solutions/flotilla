"""Territory mode: claim, flip-on-abandonment, scoring, determinism with scenario."""
import hashlib
import json
import os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "sim"))
from core import Engine


class Idle:
    name = "idle"

    def decide(self, summary, rng):
        return dict(thoughts=".")


def main():
    # instant flips (capture timer off) — the original claim/flip mechanics
    sc = {"win": "territory", "regions": 12, "region_capture_ticks": 0,
          "region_symmetry": False}
    eng = Engine([("A", Idle()), ("B", Idle())], seed=13, max_ticks=1, scenario=sc)
    assert eng.regions and len(eng.regions) == 12
    assert all(r["name"] for r in eng.regions)
    s = next(iter(eng.ships.values()))
    rid = eng._cellregion[s.x][s.y]
    eng._territory_tick()
    holder = eng.region_owner[rid]
    assert holder is not None, "sole-presence region should be claimed"
    rival = 1 - holder
    for sh in eng.ships.values():
        if sh.fleet == holder and eng._cellregion[sh.x][sh.y] == rid:
            alt = eng.regions[(rid + 1) % 12]
            sh.x, sh.y = alt["x"], alt["y"]
    r = eng.regions[rid]
    sh_r = next(sh for sh in eng.ships.values() if sh.fleet == rival)
    sh_r.x, sh_r.y = r["x"], r["y"]
    eng._territory_tick()
    assert eng.region_owner[rid] == rival, "abandoned region should flip to present rival"
    assert eng.fleets[rival].territory > 0
    assert eng.fleets[rival].score() == eng.fleets[rival].territory

    def h(seed):
        from bots import BOTS
        e = Engine([(n, BOTS[n]) for n in ["merchant", "corsair"]], seed=seed,
                   max_ticks=2000, scenario={**sc, "role_fallback": True})
        res = e.run()
        return hashlib.sha256(json.dumps(e.replay(res), sort_keys=True,
                                         separators=(",", ":")).encode()).hexdigest()
    assert h(21) == h(21), "territory determinism broke"
    # --- capture timer: sustained presence flips, interruptions reset ---
    # the clock counts TICKS: each evaluation credits territory_tick of
    # progress, so cap=50 at territory_tick=10 (the schema floor) = 5
    # evaluations — same shape as the old 5-evaluation fixture
    sc2 = {"win": "territory", "regions": 8, "region_capture_ticks": 50,
           "region_symmetry": False, "territory_tick": 10}
    e2 = Engine([("A", Idle()), ("B", Idle())], seed=13, max_ticks=1,
                scenario=sc2)
    s2 = next(iter(e2.ships.values()))
    rid2 = e2._cellregion[s2.x][s2.y]
    for _ in range(4):
        e2._territory_tick()
    assert e2.region_owner[rid2] is None, "must not flip before the timer"
    assert e2._contest.get(rid2, [None, 0])[1] == 40, "contest clock ticking"
    st = e2.summary_for(e2.fleets[s2.fleet])
    rv = next(r for r in st["regions"] if r["id"] == rid2)
    assert rv.get("contested_by") and rv.get("capture_pct") == 80, \
        f"admirals must see the contest ({rv})"
    e2._frame()
    row = next(r for r in e2.frames[-1]["r"] if r[0] == rid2)
    assert len(row) == 4 and row[2] == s2.fleet and row[3] == 80, \
        f"frame r row must carry the contest ({row})"
    e2._territory_tick()
    assert e2.region_owner[rid2] == s2.fleet, "5th tick flips the region"
    assert rid2 not in e2._contest, "contest clears after the flip"
    # interruption: rival claims, then leaves -> clock lapses
    e3 = Engine([("A", Idle()), ("B", Idle())], seed=13, max_ticks=1,
                scenario=sc2)
    s3 = next(iter(e3.ships.values()))
    rid3 = e3._cellregion[s3.x][s3.y]
    e3._territory_tick()
    e3._territory_tick()
    # an ENEMY entering pauses the clock where it is (no reset, no progress)
    foe = next(sh for sh in e3.ships.values() if sh.fleet != s3.fleet)
    fx, fy = foe.x, foe.y
    foe.x, foe.y = s3.x, s3.y              # enemy sails into the region
    before = e3._contest[rid3][1]
    e3._territory_tick()
    e3._territory_tick()
    assert e3._contest[rid3][1] == before, "enemy presence must PAUSE, not reset"
    foe.x, foe.y = fx, fy                  # enemy driven off: clock resumes
    e3._territory_tick()
    assert e3._contest[rid3][1] == before + 10, \
        "clock resumes where it paused (+1 evaluation = +territory_tick)"
    moved = []
    far = next((rr for rr in e3.regions if rr["id"] != rid3))
    for sh in e3.ships.values():           # the WHOLE claimant fleet leaves
        if sh.fleet == s3.fleet and e3._cellregion[sh.x][sh.y] == rid3:
            moved.append((sh, sh.x, sh.y))
            sh.x, sh.y = far["x"], far["y"]
    e3._territory_tick()
    assert rid3 not in e3._contest, "abandoning the claim resets the clock"
    for sh, ox, oy in moved:
        sh.x, sh.y = ox, oy
    # --- symmetric generation: every seed has its 180° mirror ---
    e4 = Engine([("A", Idle()), ("B", Idle())], seed=21, max_ticks=1,
                scenario={"win": "territory", "regions": 12,
                          "region_symmetry": True})
    ctrs = {(r["x"], r["y"]) for r in e4.regions}
    W, H = e4.W, e4.H
    unmirrored = [c for c in ctrs if (W - 1 - c[0], H - 1 - c[1]) not in ctrs
                  and c != (W // 2, H // 2)]
    assert not unmirrored, f"unmirrored region seeds: {unmirrored}"
    # --- region_size drives the count ---
    e5 = Engine([("A", Idle()), ("B", Idle())], seed=21, max_ticks=1,
                scenario={"win": "territory", "region_size": 180,
                          "width": 72, "height": 40})
    assert len(e5.regions) == 16, f"72x40/180 should be 16 ({len(e5.regions)})"
    # --- symmetry must never short the configured count (wringer 2026-08-09:
    # an early self-symmetric center seed broke pair parity — 9 of the first
    # 40 seeds shipped n-1 regions) ---
    for sd in range(40):
        eN = Engine([("A", Idle()), ("B", Idle())], seed=sd, max_ticks=1,
                    scenario={"win": "territory", "regions": 16,
                              "region_symmetry": True})
        assert len(eN.regions) == 16, \
            f"seed {sd}: symmetry shorted the count ({len(eN.regions)})"
    # --- real cadence: the clock counts TICKS, not evaluations (the 50×-slow
    # bug made a default territory game end 0-0) ---
    sc6 = {"win": "territory", "regions": 8, "region_capture_ticks": 100,
           "region_symmetry": False, "territory_tick": 50}
    e6 = Engine([("A", Idle()), ("B", Idle())], seed=13, max_ticks=1,
                scenario=sc6)
    s6 = next(iter(e6.ships.values()))
    rid6 = e6._cellregion[s6.x][s6.y]
    e6._territory_tick()
    assert e6.region_owner[rid6] is None and \
        e6._contest.get(rid6, [None, 0])[1] == 50, \
        "one evaluation = territory_tick ticks of progress"
    e6._territory_tick()
    assert e6.region_owner[rid6] == s6.fleet, \
        "cap=100 at territory_tick=50 flips on the second evaluation"
    # --- an eliminated claimant's contest dies immediately, not at the next
    # territory evaluation ---
    e7 = Engine([("A", Idle()), ("B", Idle())], seed=13, max_ticks=1,
                scenario=sc2)
    s7 = next(iter(e7.ships.values()))
    rid7 = e7._cellregion[s7.x][s7.y]
    e7._territory_tick()
    assert e7._contest.get(rid7, [None])[0] == s7.fleet
    e7.fleets[s7.fleet].flag_hull = 0
    e7.tick()
    assert rid7 not in e7._contest, \
        "elimination must reap the dead fleet's contests at once"
    print("PASS territory capture timer + symmetry + region_size")
    print("PASS test_territory")


if __name__ == "__main__":
    main()
