"""Shipyards: builds/refits/repairs share slots; repairs cost money + time
scaled by damage and always beat building a replacement; wrecks are named for
what sank, not for islands; rival.yard_busy is scouting-gated; legacy mode
(shipyard_slots=0) preserves the old behavior; checkpoints carry yard state."""
import hashlib
import json
import os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "sim"))
from core import Engine
import conn


class Idle:
    name = "idle"

    def decide(self, summary, rng):
        return dict(thoughts=".")


def mk(scn=None, seed=5):
    base = {"width": 64, "height": 36, "max_ticks": 1, "warmup": False,
            "role_fallback": True}
    return Engine([("A", Idle()), ("B", Idle())], seed=seed,
                  scenario={**base, **(scn or {})})


def run_ticks(eng, n):
    for _ in range(n):
        eng.tick()


def main():
    # --- slots gate concurrent builds ---
    eng = mk({"shipyard_slots": 1, "build_ticks": 50})
    f = eng.fleets[0]
    f.cargo = 100
    eng._apply_actions(f, {"build": [{"preset": "trawler", "squad": "A"},
                                     {"preset": "trawler", "squad": "A"}]})
    assert len(f.build_q) == 2
    before = sum(1 for s in eng.ships.values() if s.fleet == 0)
    run_ticks(eng, 30)
    assert len(f.builds) == 1 and len(f.build_q) == 1, \
        "1 slot => only one build active at a time"
    run_ticks(eng, 25)
    assert sum(1 for s in eng.ships.values() if s.fleet == 0) == before + 1, \
        "first build done, second still in the yard"
    run_ticks(eng, 55)
    assert sum(1 for s in eng.ships.values() if s.fleet == 0) == before + 2, \
        "second build follows once the slot frees"

    # 2 slots: both builds run together
    eng = mk({"shipyard_slots": 2, "build_ticks": 50})
    f = eng.fleets[0]
    f.cargo = 100
    eng._apply_actions(f, {"build": [{"preset": "trawler", "squad": "A"},
                                     {"preset": "trawler", "squad": "A"}]})
    before = sum(1 for s in eng.ships.values() if s.fleet == 0)
    run_ticks(eng, 55)
    assert sum(1 for s in eng.ships.values() if s.fleet == 0) == before + 2, \
        "2 slots => both builds complete together"

    # --- repair: cost + time scale with damage, always beat a new build ---
    eng = mk({"shipyard_slots": 2, "build_ticks": 100, "ship_cost": 15})
    f = eng.fleets[0]
    f.cargo = 100
    s = next(s for s in eng.ships.values() if s.fleet == 0)
    s.hull = 1                                # nearly dead
    run_ticks(eng, 2)
    assert s.repair_at > 0, "docked damaged ship enters the yard"
    ev = [e for e in eng.events if e.get("k") == "repair_start"
          and e["ship"] == s.id][0]
    cost = ev["cost"]
    assert cost < 15, f"near-dead repair ({cost}) must cost less than a build"
    assert 100 - f.cargo == cost, "repair charged up front"
    rt = s.repair_at
    assert rt - eng.t <= 40 + 1, \
        "near-dead repair must take under repair_ticks_pct of build_ticks"
    run_ticks(eng, 45)
    assert s.repair_at == 0 and s.hull == s.hull_max, "repair completes full"
    assert any(e.get("k") == "repaired" and e["ship"] == s.id
               for e in eng.events)

    # light damage: cheaper + faster than heavy damage
    eng = mk({"shipyard_slots": 2, "build_ticks": 100, "ship_cost": 15})
    f = eng.fleets[0]
    f.cargo = 100
    s = next(s for s in eng.ships.values() if s.fleet == 0)
    s.hull = s.hull_max - 4                   # a scratch
    run_ticks(eng, 2)
    ev2 = [e for e in eng.events if e.get("k") == "repair_start"][0]
    assert ev2["cost"] < cost, "light damage must cost less than heavy"

    # a repair OCCUPIES a slot: with 1 slot, a queued build waits for it
    eng = mk({"shipyard_slots": 1, "build_ticks": 50})
    f = eng.fleets[0]
    f.cargo = 100
    s = next(s for s in eng.ships.values() if s.fleet == 0)
    s.hull = 1
    run_ticks(eng, 2)
    assert s.repair_at > 0
    eng._apply_actions(f, {"build": [{"preset": "trawler", "squad": "A"}]})
    run_ticks(eng, 3)
    assert len(f.builds) == 0, "build must wait while the repair holds the slot"
    run_ticks(eng, 60)
    assert len(f.builds) == 1 or \
        sum(1 for s2 in eng.ships.values() if s2.fleet == 0) > 3, \
        "build starts after the repair frees the slot"

    # unaffordable repair: no charge, a warning, ship waits
    eng = mk({"shipyard_slots": 2})
    f = eng.fleets[0]
    f.cargo = 0
    s = next(s for s in eng.ships.values() if s.fleet == 0)
    s.hull = 1
    run_ticks(eng, 2)
    assert s.repair_at == 0 and f.cargo == 0
    assert any("repair" in w for w in f.warnings), "admiral told she waits"

    # repairing ship holds position (drydock)
    eng = mk({"shipyard_slots": 2})
    f = eng.fleets[0]
    f.cargo = 100
    s = next(s for s in eng.ships.values() if s.fleet == 0)
    s.hull = 1
    run_ticks(eng, 2)
    x0, y0 = s.x, s.y
    run_ticks(eng, 10)
    assert (s.x, s.y) == (x0, y0) and "repair" in s.intent, \
        "under repair = held in the yard"

    # --- legacy mode: slots=0 restores free trickle + serialized builds ---
    eng = mk({"shipyard_slots": 0, "repair_period": 5})
    f = eng.fleets[0]
    f.cargo = 7
    s = next(s for s in eng.ships.values() if s.fleet == 0)
    s.hull = s.hull_max - 6
    run_ticks(eng, 12)
    assert s.hull > s.hull_max - 6 and s.repair_at == 0, \
        "legacy trickle repair heals for free"
    assert f.cargo == 7, "legacy repair never charges"
    assert "docked repair 2 hull" in eng.cfg["rules"]

    # rules text documents the yard in the default mode (1 starting slot)
    eng = mk()
    assert "SHIPYARD" in eng.cfg["rules"] and "REPAIRS" in eng.cfg["rules"]
    assert "build_yard" in eng.cfg["rules"], "expansion is documented"
    assert eng.summary_for(eng.fleets[0])["you"]["yard"]["slots"] == 1

    # --- yard expansion: costs money, takes time, raises capacity ---
    eng = mk({"shipyard_slots": 1, "shipyard_cost": 45, "shipyard_ticks": 50,
              "shipyard_max": 2, "build_ticks": 40})
    f = eng.fleets[0]
    f.cargo = 100
    eng._apply_actions(f, {"build_yard": True})
    assert f.cargo == 55 and f.yard_done_t > 0, "expansion charged up front"
    ysum = eng.summary_for(f)["you"]["yard"]
    assert ysum["expanding"] and ysum["slots"] == 1
    # a second expansion while one is building is refused
    eng._apply_actions(f, {"build_yard": True})
    assert f.cargo == 55 and any("already" in w for w in f.warnings)
    run_ticks(eng, 55)
    assert f.yards == 2 and f.yard_done_t == 0, "expansion completes"
    assert any(e.get("k") == "yard_built" for e in eng.events)
    # at max: refused with a warning
    f.warnings.clear()
    eng._apply_actions(f, {"build_yard": True})
    assert f.yards == 2 and any("maximum" in w for w in f.warnings)
    # the new slot is REAL: two builds now run concurrently
    eng._apply_actions(f, {"build": [{"preset": "trawler", "squad": "A"},
                                     {"preset": "trawler", "squad": "A"}]})
    before = sum(1 for s in eng.ships.values() if s.fleet == 0)
    run_ticks(eng, 45)
    assert sum(1 for s in eng.ships.values() if s.fleet == 0) == before + 2, \
        "expanded yard runs both builds at once"
    # unaffordable expansion: warned, not charged
    eng2 = mk({"shipyard_slots": 1})
    f2 = eng2.fleets[0]
    f2.cargo = 3
    eng2._apply_actions(f2, {"build_yard": True})
    assert f2.cargo == 3 and any("insufficient" in w for w in f2.warnings)

    # --- wreck naming: sunk ships name their wreck; world-gen keeps islands ---
    eng = mk({"shipyard_slots": 2})
    worldgen_wrecks = [n for n in eng.nodes.values() if n.kind == "wreck"]
    assert worldgen_wrecks and all("wreck" not in n.name
                                   for n in worldgen_wrecks), \
        "world-gen wrecks keep island names"
    s = next(s for s in eng.ships.values() if s.fleet == 0)
    s.x, s.y = 30, 20                         # at sea: cargo sinks with her
    s.cargo = 3
    s.hull = 0
    eng.tick()
    named = [n for n in eng.nodes.values() if n.name.endswith("wreck")]
    assert named and named[0].name == f"A {s.preset} wreck", \
        f"sunk-ship wreck named for its owner+class, got {named!r}"

    # scuttle wreck naming
    eng = mk({"shipyard_slots": 2, "scuttle": True})
    f = eng.fleets[0]
    s = next(s for s in eng.ships.values() if s.fleet == 0)
    s.cargo = 2
    sid = s.id
    eng._apply_actions(f, {"scuttle": [sid]})
    assert any(n.name == f"A {'trawler'} wreck" or n.name.endswith("wreck")
               for n in eng.nodes.values()), "scuttle wreck named"

    # --- rival.yard_busy: gated on close scouting, like flag_hull ---
    eng = mk({"shipyard_slots": 2, "programs": True})
    f0, f1 = eng.fleets[0], eng.fleets[1]
    f1.cargo = 100
    eng._apply_actions(f1, {"build": [{"preset": "trawler", "squad": "A"}]})
    eng.tick()                                # build occupies rival's yard
    s = next(s for s in eng.ships.values() if s.fleet == 0)
    s.x, s.y = 5, 5                           # far from B's harbor
    sen, _ = eng._program_sensors(s)
    assert sen["rival.yard_busy"] == -1.0, "far away: yard is hidden"
    s.x, s.y = f1.hx - 2, f1.hy - 2           # scouting their harbor
    sen, _ = eng._program_sensors(s)
    assert sen["rival.yard_busy"] >= 1.0, "close in: yard activity revealed"

    # --- checkpoints carry yard state (active build + repair) ---
    eng = mk({"shipyard_slots": 2, "build_ticks": 60, "max_ticks": 400})
    f = eng.fleets[0]
    f.cargo = 100
    eng._apply_actions(f, {"build": [{"preset": "scout", "squad": "B"}]})
    s = next(s for s in eng.ships.values() if s.fleet == 0)
    s.hull = 5
    run_ticks(eng, 5)
    assert len(f.builds) == 1 and s.repair_at > 0
    frozen = json.loads(json.dumps(eng.freeze()))
    eng2 = Engine.thaw(frozen, [("A", Idle()), ("B", Idle())])
    f2 = eng2.fleets[0]
    s2 = eng2.ships[s.id]
    assert f2.builds == f.builds and s2.repair_at == s.repair_at, \
        "yard jobs survive freeze/thaw"
    while eng.t < eng.max_ticks:
        eng.tick()
    while eng2.t < eng2.max_ticks:
        eng2.tick()
    h = lambda e: hashlib.sha256(json.dumps(
        dict(ev=e.events, fr=e.frames), sort_keys=True,
        separators=(",", ":")).encode()).hexdigest()
    assert h(eng) == h(eng2), "thawed yard state diverged"

    print("PASS test_shipyard")


if __name__ == "__main__":
    main()
