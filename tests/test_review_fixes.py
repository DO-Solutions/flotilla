"""Pins for the 2026-08-08 code-review fixes — each of these was a live bug
reproduced by execution before it was fixed."""
import json
import os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "sim"))
import config_schema
import conn
from core import Engine, VOLLEY
from series import digest_for


class Idle:
    name = "idle"

    def decide(self, summary, rng):
        return dict(thoughts="idle")


def main():
    # 1) flagship-kill bounty: a TEAMMATE's nearby ship must never collect
    eng = Engine([("A", Idle()), ("B", Idle()), ("C", Idle()), ("D", Idle())],
                 seed=3, max_ticks=200,
                 scenario={"teams": "0,1|2,3", "warmup": False})
    # park a fleet-1 (allied) ship on fleet 0's harbor, hostile ships far away
    f0 = eng.fleets[0]
    ally = next(s for s in eng.ships.values() if s.fleet == 1)
    ally.x, ally.y = f0.hx, f0.hy
    for s in eng.ships.values():
        if s.fleet in (2, 3):
            s.x, s.y = 0, 0
    f0.flag_hull = 0
    eng.tick()
    assert eng.fleets[1].kills == 0 and eng.fleets[1].kill_count == 0, \
        "teammate collected the flagship bounty"
    ev = [e for e in eng.events if e["k"] == "flag_sunk"][0]
    assert ev.get("by") in (2, 3), f"bounty must go hostile, got {ev.get('by')}"

    # 2) ship volley cadence == cfg['volley'] ticks exactly (was volley+1)
    eng2 = Engine([("A", Idle()), ("B", Idle())], seed=5, max_ticks=100,
                  scenario={"warmup": False})
    s = next(iter(eng2.ships.values()))
    s.volley_cd = eng2.cfg["volley"] - 1        # just fired
    fires = []
    for i in range(1, 3 * eng2.cfg["volley"] + 1):
        if s.volley_cd > 0:
            s.volley_cd -= 1
        else:
            fires.append(i)
            s.volley_cd = eng2.cfg["volley"] - 1
    gaps = [b - a for a, b in zip(fires, fires[1:])]
    assert all(g == eng2.cfg["volley"] for g in gaps), \
        f"volley cadence {gaps} != {eng2.cfg['volley']}"
    assert VOLLEY == eng2.cfg["volley"], "constant drifted from schema default"

    # 3) hostile build shapes: bare-string elements + lowercase squads
    eng3 = Engine([("A", Idle()), ("B", Idle())], seed=7, max_ticks=100,
                  scenario={"warmup": False})
    f = eng3.fleets[0]
    f.cargo = 100
    eng3._apply_actions(f, {"thoughts": "x",
                            "build": ["trawler",            # bare string: skip
                                      {"preset": "trawler", "squad": "b"}]})
    assert f.build_q and f.build_q[0] == ("trawler", "B"), \
        f"lowercase squad not normalized: {f.build_q}"
    # the real thoughts survived (recorded via finally)
    assert any(d.get("thoughts") == "x" for d in eng3.decisions), \
        "decision lost despite hostile build element"

    # 4) excluded role -> a warning, not a silent drop
    eng3._apply_actions(f, {"thoughts": "y",
                            "orders": {"A": {"role": "no-such-role"}}})
    assert any("order REJECTED" in w for w in f.warnings), f.warnings

    # 5) conn: mem writes are atomic across a budget fault
    prog = conn.compile_program(
        "mem a = 0\nset a = 1\nwhen mem.a == 1: helm.hold()")
    mem = prog.init_mem()
    out = prog.run({}, mem)
    assert out is not None and mem["a"] == 1.0, "normal run commits mem"
    big = conn.compile_program(
        "mem p = 0\nset p = 5\n"
        + "\n".join(f"when mem.p == {i}: helm.hold()" for i in range(60, 120)))
    mem2 = big.init_mem()
    # exhaust the budget artificially by shrinking it via many evals
    saved = conn.BUDGET
    try:
        conn.BUDGET = 3                       # guarantees mid-walk exhaustion
        try:
            big.run({}, mem2)
            raise AssertionError("expected ConnError")
        except conn.ConnError:
            pass
        assert mem2.get("p", 0.0) == 0.0, \
            f"partial mem write survived a fault: {mem2}"
    finally:
        conn.BUDGET = saved

    # 6) section_resolve: bounds finally bite (series.games lo=1)
    assert config_schema.section_resolve("series", {"games": 0})["games"] == 1
    try:
        config_schema.section_resolve("series", {"nope": 1})
        raise AssertionError("unknown section key accepted")
    except KeyError:
        pass

    # 7) you.kills is a COUNT; the points ride in you.kill_score
    eng3.fleets[0].kills = 158
    eng3.fleets[0].kill_count = 2
    you = eng3.summary_for(eng3.fleets[0])["you"]
    assert you["kills"] == 2 and you["kill_score"] == 158, you

    # 8) digest total_t: ticks respected when frames are absent
    rp = {"result": {"names": {0: "A", 1: "B"}, "scores": {0: 5, 1: 3},
                     "ticks": 6000, "winner": 0},
          "decisions": [], "events": [
              {"k": "flag_sunk", "fleet": 1, "by": 0, "t": 3000}],
          "frames": []}
    d = json.loads(digest_for(rp, 1, 1, 1))
    assert d["YOUR_ELIMINATION"]["at_pct_of_game"] == 50, \
        d["YOUR_ELIMINATION"]["at_pct_of_game"]

    # 9) a ship sunk SOLELY by a flagship battery credits that flagship's fleet
    #    (doc review found it credited nobody — attackers set was empty)
    eng9 = Engine([("A", Idle()), ("B", Idle())], seed=4, max_ticks=100,
                  scenario={"warmup": False})
    victim = next(s for s in eng9.ships.values() if s.fleet == 1)
    victim.x, victim.y = eng9.W // 2, eng9.H // 2   # at sea (no dock-repair)
    victim.flag_attackers.add(0)                    # fleet 0's flagship hit her
    victim.hull = 0
    eng9.tick()
    assert victim.id not in eng9.ships, "victim did not sink"
    assert eng9.fleets[0].kill_count == 1 and \
        eng9.fleets[0].kills == eng9.cfg["kill_score"], \
        "flagship-only kill credited nobody"

    print("PASS test_review_fixes")


if __name__ == "__main__":
    main()
