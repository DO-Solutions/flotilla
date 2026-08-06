"""Parley routing: delivery next window, per-window cap, truncation, self-exclusion."""
import os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "sim"))
from core import Engine, WINDOW


class Talker:
    name = "talker"

    def __init__(self):
        self.got = []

    def decide(self, summary, rng):
        self.got.append(summary["messages"])
        return dict(thoughts="talk", parley=[
            dict(to="all", text="parley to everyone " + "x" * 400),
            dict(to=1, text="direct to fleet 1"),
            dict(to=1, text="third message must be DROPPED (cap 2)")])


class Quiet:
    name = "quiet"

    def __init__(self):
        self.got = []

    def decide(self, summary, rng):
        self.got.append(summary["messages"])
        return dict(thoughts="...")


def main():
    a, b = Talker(), Quiet()
    eng = Engine([("talker", a), ("quiet", b)], seed=9, max_ticks=WINDOW * 2 + 1)
    eng.run()
    assert a.got[0] == [] and b.got[0] == [], "window 0 must start empty"
    w1 = b.got[1]
    assert len(w1) == 2, f"quiet should get exactly 2 messages (cap), got {len(w1)}"
    assert all(m["sender"] == 0 for m in w1)
    assert len(w1[0]["text"]) == 280, "broadcast must truncate to 280"
    assert w1[1]["text"] == "direct to fleet 1"
    assert a.got[1] == [], "talker must not receive its own messages"
    pev = [e for e in eng.events if e["k"] == "parley"]
    assert len(pev) == 6, f"2 msgs x 3 windows recorded, got {len(pev)}"
    assert pev[0]["to"] == "all" and pev[1]["to"] == 1
    print("PASS test_parley")


if __name__ == "__main__":
    main()
