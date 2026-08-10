"""Batch-2 mechanics: 8-player games, per-window combat reports, memo
sentence-boundary trim, scuttle-anywhere discoverability text."""
import os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "sim"))
from core import Engine, cheb
from llm import LLMAdmiral


class Idle:
    name = "idle"

    def decide(self, summary, rng):
        return dict(thoughts=".")


def test_eight_players():
    names = list("ABCDEFGH")
    eng = Engine([(n, Idle()) for n in names], seed=3, max_ticks=50,
                 scenario={"width": 120, "height": 80})
    harbors = [(f.hx, f.hy) for f in eng.fleets.values()]
    assert len(set(harbors)) == 8, "harbors must be distinct"
    for (ax, ay) in harbors:
        assert sum(1 for (bx, by) in harbors if cheb(ax, ay, bx, by) < 12) == 1, \
            "harbors packed too close"
    eng.run()                              # 50 ticks, 8 fleets: no crash
    # teams work at 8 (4v4)
    eng2 = Engine([(n, Idle()) for n in names], seed=3, max_ticks=1,
                  scenario={"width": 120, "height": 80,
                            "teams": "0,1,2,3|4,5,6,7"})
    assert [eng2.fleets[i].team for i in range(8)] == [0] * 4 + [1] * 4
    try:
        Engine([(n, Idle()) for n in "ABCDEFGHI"], seed=3, max_ticks=1)
        raise AssertionError("9 fleets must be refused")
    except ValueError:
        pass


def test_combat_report():
    eng = Engine([("A", Idle()), ("B", Idle())], seed=7, max_ticks=1,
                 scenario={"role_fallback": True})
    s0 = next(s for s in eng.ships.values() if s.fleet == 0)
    s1 = next(s for s in eng.ships.values() if s.fleet == 1)
    # park two hostiles side by side mid-ocean, guns hot
    s0.x, s0.y = eng.W // 2, eng.H // 2
    s1.x, s1.y = s0.x + 1, s0.y
    s0.orders = dict(s0.orders, role="guard", aggression=3, rally=(s0.x, s0.y))
    s1.orders = dict(s1.orders, role="guard", aggression=3, rally=(s1.x, s1.y))
    for _ in range(12):
        eng.tick()
    su0 = eng.summary_for(eng.fleets[0])
    su1 = eng.summary_for(eng.fleets[1])
    c0, c1 = su0["you"]["combat"], su1["you"]["combat"]
    assert c0 and c1, f"both sides must file a combat report ({c0} / {c1})"
    e0 = next(e for e in c0 if e["ship"] == s0.id)
    assert e0["vs"] == "B" and (e0["dealt"] > 0 or e0["taken"] > 0)
    assert isinstance(e0["at"], list) and len(e0["at"]) == 2
    # the report clears at the decision window (same lifecycle as recent_hits)
    eng.fleets[0].combat = {}
    assert eng.summary_for(eng.fleets[0])["you"]["combat"] == []
    # rules text sells the scuttle escape hatch
    assert "ANYWHERE" in eng.cfg["rules"] and "escape hatch" in eng.cfg["rules"]


def test_memo_trim():
    a = LLMAdmiral("test-model", memo_chars=200)
    assert a.memo_chars == 200
    long = ("The first finding is clear. " * 6 +
            "Then a very long unbroken trailing clause that will be cut " * 4)
    a._chat = lambda msgs, **kw: (long, 10, 10, 5)
    out = a.debrief("digest")
    assert out["err"] is None
    memo = out["memo"]
    assert len(memo) <= 200
    assert "…[cut@limit]" in memo
    head = memo.replace(" …[cut@limit]", "")
    assert head.endswith("."), f"cut must land on a sentence boundary: …{memo[-40:]!r}"
    # defaults: the cap now matches the token budget
    assert LLMAdmiral("m").memo_chars == 2500


def main():
    test_eight_players()
    test_combat_report()
    test_memo_trim()
    print("PASS test_batch2")


if __name__ == "__main__":
    main()
