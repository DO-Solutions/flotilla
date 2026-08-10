"""Squad reassignment, impassable islands, flagship relocation, team games."""
import hashlib
import json
import os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "sim"))
from core import Engine, cheb


class Idle:
    name = "idle"

    def decide(self, summary, rng):
        return dict(thoughts=".")


def mk(players=2, **sc):
    names = ["A", "B", "C", "D"][:players]
    return Engine([(n, Idle()) for n in names], seed=9, max_ticks=1,
                  scenario={**sc})


def test_reassign():
    eng = mk()
    f = eng.fleets[0]
    docked = next(s for s in eng.ships.values() if s.fleet == 0)
    assert cheb(docked.x, docked.y, f.hx, f.hy) <= eng.hr
    old = docked.squad
    eng._apply_actions(f, {"reassign": {str(docked.id): "d"}})
    assert f.pending_reassign[docked.id] == "D"
    eng.tick()
    assert docked.squad == "D" != old, "docked ship must transfer immediately"
    assert any(e["k"] == "reassign" and e["ship"] == docked.id for e in eng.events)
    # at sea: pending until dock
    eng2 = mk()
    f2 = eng2.fleets[0]
    s2 = next(s for s in eng2.ships.values() if s.fleet == 0)
    s2.x, s2.y = eng2.W // 2, eng2.H // 2
    eng2._apply_actions(f2, {"reassign": {s2.id: "E"}})
    eng2.tick()
    assert s2.squad != "E" and f2.pending_reassign.get(s2.id) == "E"
    # hostile input: junk ids/squads never crash, never land
    eng._apply_actions(f, {"reassign": {"xx": "B", "999": "B", str(docked.id): 7}})
    assert 999 not in f.pending_reassign


def test_islands():
    eng = mk(island_coverage=5)
    assert eng.blocked, "islands on -> blocked cells exist"
    assert eng.island_specs, "charted specs recorded"
    assert "ISLANDS" in eng.cfg["rules"] and "ISLANDS" in eng.scenario["rules"]
    for f in eng.fleets.values():
        for (bx, by) in eng.blocked:
            assert cheb(bx, by, f.hx, f.hy) > eng.hr + 1, "island in a harbor circle"
    for n in eng.nodes.values():
        assert (n.x, n.y) not in eng.blocked, "island covers a resource node"
    # steering: sail a ship straight through a charted island — it must arrive
    # without ever standing on land
    cx, cy, r = eng.island_specs[0]
    s = next(iter(eng.ships.values()))
    s.x, s.y = max(0, cx - r - 3), cy
    tx, ty = min(eng.W - 1, cx + r + 3), cy
    s.stats = dict(s.stats, speed=3)
    for _ in range(600):
        assert (s.x, s.y) not in eng.blocked, "ship sailed onto land"
        if (s.x, s.y) == (tx, ty):
            break
        eng._move(s, tx, ty)
    assert (s.x, s.y) == (tx, ty), f"ship never rounded the island ({s.x},{s.y})"
    # off by default
    assert mk().blocked == set()
    # the slider IS the density: high coverage builds a real archipelago,
    # low coverage scatters dots — and lanes stay >= 3 Manhattan cells wide
    lo_e = mk(island_coverage=2)
    hi_e = mk(island_coverage=15)
    area = lo_e.W * lo_e.H
    assert len(lo_e.blocked) < len(hi_e.blocked), "coverage must scale density"
    assert len(hi_e.blocked) >= area * 8 // 100, \
        f"15% asked, {len(hi_e.blocked) * 100 // area}% delivered"
    assert len(lo_e.blocked) <= area * 4 // 100, "2% should stay sparse"
    msg = "islands too close — a ship could be walled in"
    for (ax, ay, ar) in hi_e.island_specs:
        for (bx, by, br) in hi_e.island_specs:
            if (ax, ay, ar) != (bx, by, br):
                assert abs(ax - bx) + abs(ay - by) >= ar + br + 3, msg


def test_flag_relocation():
    eng = mk(flag_move=True, flag_speed=4)
    f = eng.fleets[0]
    ox, oy = f.hx, f.hy
    tgt = (ox + 12, oy + 9)
    eng._apply_actions(f, {"relocate": list(tgt)})
    assert f.flag_target == tgt
    assert any(e["k"] == "relocate_order" for e in eng.events)
    for _ in range(400):
        if f.flag_target is None:
            break
        eng.tick()
    assert (f.hx, f.hy) == tgt, "flagship never arrived"
    assert any(e["k"] == "flag_arrive" for e in eng.events)
    # the command circle moved with it: a ship at the NEW anchorage is docked
    s = next(s for s in eng.ships.values() if s.fleet == 0)
    s.x, s.y = f.hx, f.hy
    s.cargo = 3
    bank0 = f.bank
    eng.tick()
    assert f.bank == bank0 + 3, "deposit must follow the relocated flagship"
    # disabled by default: action ignored
    eng2 = mk()
    eng2._apply_actions(eng2.fleets[0], {"relocate": [30, 30]})
    assert eng2.fleets[0].flag_target is None
    # hostile input survives
    eng._apply_actions(f, {"relocate": ["x", None]})
    eng._apply_actions(f, {"relocate": [9999, -4]})
    assert f.flag_target is None


def test_teams():
    eng = mk(players=4, teams="0,1|2,3")
    assert [eng.fleets[i].team for i in range(4)] == [0, 0, 1, 1]
    assert "TEAM MATCH" in eng.scenario["rules"]
    # no friendly fire: an ally next door is not a target
    s0 = next(s for s in eng.ships.values() if s.fleet == 0)
    s1 = next(s for s in eng.ships.values() if s.fleet == 1)
    s1.x, s1.y = s0.x + 1, s0.y
    tgt, _ = eng._nearest_enemy(s0, 3)
    assert tgt is None or tgt.fleet not in (0, 1), "ally targeted"
    # shared vision: fleet 0 sees through fleet 1's lookouts
    s1.x, s1.y = eng.W // 2, eng.H // 2
    assert eng._fleet_sees(eng.fleets[0], s1.x, s1.y), "allied vision not shared"
    # summary: team_mates listed, allies never in enemies
    summ = eng.summary_for(eng.fleets[0])
    assert summ["you"].get("team_mates") == [1]
    assert all(e["fleet"] not in (0, 1) for e in summ["enemies"])
    # last TEAM standing ends the game + team result fields
    eng.fleets[2].alive = False
    eng.fleets[3].alive = False
    res = eng.run()
    assert res["ticks"] <= 1, "one team left -> game over"
    assert res["teams"] == {0: 0, 1: 0, 2: 1, 3: 1}
    assert set(res["team_scores"]) == {0, 1}
    assert res["winner"] in (0, 1)
    # bad team spec fails loud
    try:
        mk(players=2, teams="0,7")
        raise AssertionError("bad team index must raise")
    except ValueError:
        pass


def main():
    test_reassign()
    test_islands()
    test_flag_relocation()
    test_teams()
    # determinism with everything on at once
    def h(seed):
        from bots import BOTS
        e = Engine([(n, BOTS[n]) for n in ["merchant", "corsair", "merchant", "corsair"]],
                   seed=seed, max_ticks=1500,
                   scenario={"teams": "0,1|2,3", "island_coverage": 6, "flag_move": True,
                             "role_fallback": True})
        r = e.run()
        return hashlib.sha256(json.dumps(e.replay(r), sort_keys=True,
                                         separators=(",", ":")).encode()).hexdigest()
    assert h(41) == h(41), "expansion determinism broke"
    print("PASS test_expansion")


if __name__ == "__main__":
    main()
