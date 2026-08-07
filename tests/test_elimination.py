"""Regression: flag_sunk path — engineered strike force sinks an undefended flagship,
match ends early, bounty credited. (Meta-independent; the scripted meta rarely eliminates.)"""
import os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "sim"))
from core import Engine
from bots import BOTS


class Idle:
    name = "idle"

    def decide(self, summary, rng):
        return dict(thoughts="...")


def main():
    eng = Engine([("corsair", BOTS["corsair"]), ("idle", Idle())], seed=7, scenario={"role_fallback": True})
    tgt = eng.fleets[1]
    for s in [s for s in eng.ships.values() if s.fleet == 1]:
        del eng.ships[s.id]
    for _ in range(8):
        s = eng._spawn(eng.fleets[0], "raider", "E")
        s.x, s.y = tgt.hx, tgt.hy
        s.orders = dict(role="assault", rally=(tgt.hx, tgt.hy), aggression=3,
                        retreat_hull_pct=0, target_fleet=1)
    r = eng.run()
    flags = [e for e in eng.events if e["k"] == "flag_sunk"]
    assert flags and flags[0]["fleet"] == 1, f"no flag_sunk: {flags}"
    assert r["alive"] == [0], r
    assert r["ticks"] < 6000, "match should end early on elimination"
    assert eng.fleets[0].kills >= 150, "flagship bounty not credited"
    print("PASS test_elimination")


if __name__ == "__main__":
    main()
