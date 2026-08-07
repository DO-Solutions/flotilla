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
    sc = {"win": "territory", "regions": 12}
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
    print("PASS test_territory")


if __name__ == "__main__":
    main()
