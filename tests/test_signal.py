"""Return-now vs return-safe: both mean TURN BACK IMMEDIATELY. NOW beelines
home even through danger; SAFE routes defensively — an enemy met en route is
evaded until clear, then homing resumes. Both are real hoists (charged,
cooldown); docking clears both flags; NOW always overrides SAFE."""
import os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "sim"))
from core import Engine, cheb


class Idle:
    name = "idle"

    def decide(self, summary, rng):
        return dict(thoughts=".")


def mk(scn=None):
    base = {"width": 64, "height": 36, "max_ticks": 1, "warmup": False,
            "role_fallback": True, "signal_cost": 2, "signal_cd": 3}
    return Engine([("A", Idle()), ("B", Idle())], seed=9,
                  scenario={**base, **(scn or {})})


def main():
    # --- return (NOW): immediate recall, straight home ---
    eng = mk()
    f = eng.fleets[0]
    f.cargo = 10
    s = next(s for s in eng.ships.values() if s.fleet == 0)
    s.x, s.y = 40, 25
    eng._apply_actions(f, {"signal": {"return": "all"}})
    assert f.cargo == 8 and f.signal_cd == 3, "NOW is a charged hoist"
    assert s.recall and not s.recall_safe
    d0 = cheb(s.x, s.y, f.hx, f.hy)
    for _ in range(8):
        eng.tick()
    assert cheb(s.x, s.y, f.hx, f.hy) < d0, "recalled ship heads home"

    # --- return_safe: ALSO turns back immediately (no finishing the trip) ---
    eng = mk()
    f = eng.fleets[0]
    f.cargo = 10
    s = next(s for s in eng.ships.values() if s.fleet == 0)
    s.x, s.y = 40, 25
    s.cargo = 1                              # half a trip in the hold: ignored
    eng._apply_actions(f, {"signal": {"return_safe": "all"}})
    assert f.cargo == 8, "SAFE is a charged hoist"
    assert s.recall_safe and not s.recall
    d0 = cheb(s.x, s.y, f.hx, f.hy)
    for _ in range(8):
        eng.tick()
    assert cheb(s.x, s.y, f.hx, f.hy) < d0, \
        "return-safe ship turns for home immediately"
    assert "return-safe" in s.intent

    # --- return_safe: an enemy on the route home is EVADED, not sailed into ---
    eng = mk()
    f, foe = eng.fleets[0], eng.fleets[1]
    f.cargo = 10
    s = next(s for s in eng.ships.values() if s.fleet == 0)
    s.x, s.y = 30, 18
    raider = eng._spawn(foe, "raider", "E")
    raider.x, raider.y = 26, 16              # sits on the way home, dist 4 < 6
    eng._apply_actions(f, {"signal": {"return_safe": "all"}})
    de0 = cheb(s.x, s.y, raider.x, raider.y)
    for _ in range(6):
        rx, ry = raider.x, raider.y
        eng.tick()
        raider.x, raider.y = rx, ry          # pin the raider in place
    assert cheb(s.x, s.y, raider.x, raider.y) > de0, \
        "return-safe ship opens the range from the threat"
    assert "evading" in s.intent
    # threat gone -> homing resumes
    raider.hull = 0
    eng.tick()
    d1 = cheb(s.x, s.y, f.hx, f.hy)
    for _ in range(10):
        eng.tick()
    assert cheb(s.x, s.y, f.hx, f.hy) < d1, "clear of danger: homing resumes"

    # by contrast a NOW recall beelines even with the raider in the way
    eng = mk()
    f, foe = eng.fleets[0], eng.fleets[1]
    f.cargo = 10
    s = next(s for s in eng.ships.values() if s.fleet == 0)
    s.x, s.y = 30, 18
    raider = eng._spawn(foe, "raider", "E")
    raider.x, raider.y = 26, 16
    eng._apply_actions(f, {"signal": {"return": "all"}})
    d0 = cheb(s.x, s.y, f.hx, f.hy)
    for _ in range(6):
        rx, ry = raider.x, raider.y
        eng.tick()
        raider.x, raider.y = rx, ry
    assert cheb(s.x, s.y, f.hx, f.hy) < d0, "NOW beelines regardless"

    # --- NOW overrides SAFE; SAFE never downgrades NOW ---
    eng = mk()
    f = eng.fleets[0]
    f.cargo = 20
    s = next(s for s in eng.ships.values() if s.fleet == 0)
    s.x, s.y = 40, 25
    eng._apply_actions(f, {"signal": {"return": "all"}})
    f.signal_cd = 0
    eng._apply_actions(f, {"signal": {"return_safe": "all"}})
    assert s.recall and not s.recall_safe, "NOW stays NOW"

    # --- docking clears both flags ---
    eng = mk()
    f = eng.fleets[0]
    f.cargo = 10
    s = next(s for s in eng.ships.values() if s.fleet == 0)
    s.x, s.y = 40, 25
    eng._apply_actions(f, {"signal": {"return_safe": "all"}})
    s.x, s.y = f.hx, f.hy                  # teleport home
    eng.tick()
    assert not s.recall and not s.recall_safe, "docking clears the flags"

    # rules document both urgencies
    assert "return_safe" in eng.cfg["rules"]

    print("PASS test_signal")


if __name__ == "__main__":
    main()
