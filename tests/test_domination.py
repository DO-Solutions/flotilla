"""Domination mode: no clock (cap = safety net), last admiral standing wins,
elimination ends the game early, kill score is only the cap tiebreak."""
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
    sc = {"win": "domination", "domination_cap": 4000}
    # the cap replaces max_ticks when the caller doesn't force one
    eng = Engine([("A", Idle()), ("B", Idle())], seed=5, scenario=sc)
    assert eng.max_ticks == 4000, f"cap should set max_ticks, got {eng.max_ticks}"
    assert "last admiral" in eng.cfg["description"].lower() \
        or "DOMINATION" in eng.cfg["description"], "admirals must be told the win rule"
    assert eng.fleets[0].score() == 0, "domination score starts at kill score 0"

    # elimination ends the game before the cap: sink B's flagship directly
    eng2 = Engine([("A", Idle()), ("B", Idle())], seed=5, scenario=sc)
    eng2.fleets[1].alive = False          # simulate flagship destroyed
    res = eng2.run()
    assert res["ticks"] < 4000, "game must end at elimination, not run to cap"
    assert res["winner"] == 0, "last admiral standing wins"

    # cap reached with both alive -> highest kill score wins
    eng3 = Engine([("A", Idle()), ("B", Idle())], seed=5, max_ticks=50, scenario=sc)
    eng3.fleets[1].kills = 99
    res3 = eng3.run()
    assert res3["winner"] == 1, "at the cap, kill score is the tiebreak"
    assert eng3.fleets[1].score() == 99, "domination score() == kills"

    # determinism with the scenario active (full replay hash)
    def h(seed):
        from bots import BOTS
        e = Engine([(n, BOTS[n]) for n in ["merchant", "corsair"]], seed=seed,
                   max_ticks=2000,
                   scenario={**sc, "role_fallback": True})
        r = e.run()
        return hashlib.sha256(json.dumps(e.replay(r), sort_keys=True,
                                         separators=(",", ":")).encode()).hexdigest()
    assert h(31) == h(31), "domination determinism broke"
    print("PASS test_domination")


if __name__ == "__main__":
    main()
