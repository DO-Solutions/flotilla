"""helm.assault() lets a conn program destroy an enemy flagship — the
domination win condition, previously unreachable from the language."""
import os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "sim"))
import conn
from core import Engine


class Prog:
    """LLM-stand-in that installs one conn program on squad A, once."""
    name = "prog"

    def __init__(self, text):
        self.text = text
        self.sent = False

    def decide(self, summary, rng):
        if self.sent:
            return {"thoughts": "hold"}
        self.sent = True
        return {"thoughts": "assault", "programs": {"A": self.text}}


class Idle:
    name = "idle"

    def decide(self, summary, rng):
        return {"thoughts": "idle"}


def main():
    # the language exposes the action + sensors
    ref = conn.api_reference()
    assert "helm.assault(" in ref and "rival.flag_dist" in ref \
        and "rival.flag_hull" in ref, "assault API not documented"
    assert '"assault"' in repr(conn.ACTIONS) or "assault" in conn.ACTIONS

    # MECHANIC: a ship running helm.assault() adjacent to the enemy flag
    # damages it (this was impossible before — conn couldn't target flags)
    atk, victim = Prog("default: helm.assault()"), Idle()
    eng = Engine([("Atk", atk), ("Vic", victim)], seed=3, max_ticks=200,
                 scenario={"width": 40, "height": 24, "warmup": False,
                           "role_fallback": False, "programs": True,
                           "win": "domination", "flag_hull": 200})
    vic = eng.fleets[1]
    # arm an attacker ship with the assault program directly, next to the flag
    s = next(x for x in eng.ships.values() if x.fleet == 0)
    prog = conn.compile_program("default: helm.assault()")
    s.program = prog
    s.pmem = prog.init_mem()
    s.x, s.y = vic.hx - 1, vic.hy
    h0 = vic.flag_hull
    for _ in range(60):                    # a few volleys
        eng.tick()
    assert vic.flag_hull < h0, \
        f"helm.assault dealt no flag damage ({h0} -> {vic.flag_hull})"

    # full elimination with sustained force: build raider waves + assault
    class Waves:
        name = "waves"

        def decide(self, summary, rng):
            return {"thoughts": "build + assault",
                    "build": [{"preset": "raider", "squad": "A"}],
                    "programs": {"A": "default: helm.assault()"}}

    eng3 = Engine([("Atk", Waves()), ("Vic", Idle())], seed=3, max_ticks=8000,
                  scenario={"width": 40, "height": 24, "warmup": False,
                            "role_fallback": False, "programs": True,
                            "win": "domination", "flag_hull": 30,
                            "income_amount": 40, "income_period": 100})
    v3 = eng3.fleets[1]
    res = eng3.run()
    assert any(e["k"] == "flag_sunk" and e["fleet"] == v3.id
               for e in eng3.events), "sustained assault never killed the flag"
    assert res["winner"] == 0, f"attacker should win by elimination: {res}"

    # rival.flag_hull is fogged until a ship is close: a far ship reads -1
    eng2 = Engine([("A", Idle()), ("B", Idle())], seed=1, max_ticks=50,
                  scenario={"width": 60, "height": 34, "warmup": False,
                            "role_fallback": False, "programs": True})
    s = next(iter(eng2.ships.values()))
    enemy_fleet = 1 if s.fleet == 0 else 0
    ef = eng2.fleets[enemy_fleet]
    s.x, s.y = 0, 0                              # far from every flag
    ef.hx, ef.hy = 59, 33
    sen, _ = eng2._program_sensors(s)
    assert sen["rival.flag_hull"] == -1, "distant flag hull must be hidden"
    assert sen["rival.flag_dist"] < 9999, "flag position is public"
    s.x, s.y = ef.hx, ef.hy                      # right on it
    sen2, _ = eng2._program_sensors(s)
    assert sen2["rival.flag_hull"] >= 0, "close flag hull must be revealed"

    print("PASS test_assault")


if __name__ == "__main__":
    main()
