"""Config schema: knobs bite, unknown keys fail loudly, rules digest reflects values."""
import os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "sim"))
from core import Engine
from config_schema import resolve, defaults


class Idle:
    name = "idle"

    def decide(self, summary, rng):
        return dict(thoughts=".")


def main():
    assert resolve()["ship_cost"] == defaults()["ship_cost"] == 15
    try:
        resolve({"shp_cost": 5})
        raise SystemExit("FAIL: unknown key accepted")
    except KeyError:
        pass
    assert resolve({"ship_cost": 99999})["ship_cost"] == 100, "clamp to hi"
    eng = Engine([("a", Idle()), ("b", Idle())], seed=3, max_ticks=1,
                 scenario={"width": 60, "height": 34, "ship_cost": 5})
    assert eng.W == 60 and eng.H == 34
    assert all(n.x < 60 and n.y < 34 for n in eng.nodes.values())
    assert "cost 5 cargo" in eng.cfg["rules"] and "60x34" in eng.cfg["rules"]
    f = eng.fleets[0]
    f.cargo = 14
    eng._apply_actions(f, {"build": [{"preset": "trawler", "squad": "A"}] * 3})
    assert len(f.build_q) == 2 and f.cargo == 4
    eng3 = Engine([("a", Idle()), ("b", Idle())], seed=1, max_ticks=1,
                  scenario={"signal_cost": 2, "signal_cd": 7})
    f3 = eng3.fleets[0]
    f3.cargo = 10
    # default mode is return_only: signal:true is not a valid flag there (no charge)…
    eng3._apply_actions(f3, {"signal": True})
    assert f3.cargo == 10 and f3.signal_cd == 0
    # …but the RETURN flag is, and charging/cooldown work
    eng3._apply_actions(f3, {"signal": {"return": "all"}})
    assert f3.cargo == 8 and f3.signal_cd == 7

    # the committed docs (CONFIG.md + config-schema.json) must match what the
    # schema generates — they're checked in so GitHub/bundles can find them,
    # so this guards them from drifting silently out of date
    import config_schema
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    md = config_schema.config_md()
    sj = config_schema.schema_json()
    sj = sj if isinstance(sj, str) else sj.decode()
    # tolerate a trailing newline so both `--md > CONFIG.md` (adds one) and a
    # direct stdout.write (none) pass
    with open(os.path.join(root, "CONFIG.md")) as fh:
        assert fh.read().rstrip("\n") == md.rstrip("\n"), \
            "CONFIG.md is stale — regenerate it (see CONTRIBUTING)"
    with open(os.path.join(root, "config-schema.json")) as fh:
        assert fh.read().rstrip("\n") == sj.rstrip("\n"), \
            "config-schema.json is stale — regenerate it"
    print("PASS test_config")


if __name__ == "__main__":
    main()
