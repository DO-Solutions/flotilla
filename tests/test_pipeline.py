"""Catch-up pipelining: ONE in-flight call per admiral, fast admirals decide
every window, slow ones miss windows and rejoin with a CATCH_UP note; the sim
blocks only at pipeline_depth. Defaults keep classic lockstep."""
import hashlib
import json
import os
import sys
import time
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "sim"))
from core import Engine

FAST = {"window": 100, "pipeline_depth": 2, "window_wait_s": 1,
        "hold_full_window": False}


class Recorder:
    """Instant bot that logs the snapshots it was shown."""
    name = "rec"

    def __init__(self):
        self.windows = []
        self.catchups = []
        self.in_flight = 0
        self.max_in_flight = 0

    def decide(self, summary, rng):
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        self.windows.append(summary["window"])
        if "CATCH_UP" in summary:
            self.catchups.append((summary["window"],
                                  summary["CATCH_UP"]["windows_missed"]))
        out = dict(thoughts=f"w{summary['window']}")
        self.in_flight -= 1
        return out


class Slow(Recorder):
    name = "slow"

    def __init__(self, delay):
        super().__init__()
        self.delay = delay

    def decide(self, summary, rng):
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        time.sleep(self.delay)
        self.windows.append(summary["window"])
        if "CATCH_UP" in summary:
            self.catchups.append((summary["window"],
                                  summary["CATCH_UP"]["windows_missed"]))
        out = dict(thoughts=f"w{summary['window']}")
        self.in_flight -= 1
        return out


def main():
    # 1) instant bots: replies land the NEXT boundary (ot stamp = 1 window),
    #    every window is decided, no CATCH_UP ever
    a, b = Recorder(), Recorder()
    eng = Engine([("A", a), ("B", b)], seed=5, max_ticks=400, scenario=FAST)
    assert "CATCH-UP WINDOWS" in eng.cfg["rules"], "admirals must be told"
    eng.run()
    ours = [d for d in eng.decisions if d["fleet"] == 0 and "thoughts" in d
            and d["thoughts"].startswith("w")]
    assert ours, "no pipelined decisions applied"
    for d in ours:
        assert "ot" in d and d["t"] - d["ot"] == 100, \
            f"instant reply must land next window: {d}"
    assert a.windows == sorted(a.windows) and not a.catchups, \
        "instant bot should never need to catch up"

    # 2) ONE in-flight call per admiral, ever — the whole point of catch-up
    #    (the old depth mode ran parallel stale calls per admiral)
    s = Slow(2.5)                         # >> window_wait_s: guaranteed to miss
    eng2 = Engine([("A", s), ("B", Recorder())], seed=5, max_ticks=800,
                  scenario=FAST)
    t0 = time.time()
    eng2.run()
    assert time.time() - t0 < 30, "pipelined run must not serialize sleeps"
    assert s.max_in_flight == 1, \
        f"an admiral must never have parallel calls (saw {s.max_in_flight})"
    assert len(s.windows) >= 2, "slow bot still gets windows"

    # 3) a slow bot gets a CATCH_UP note when it rejoins after missing windows
    #    (scripted windows advance ~instantly against a 0.35s think)
    assert s.catchups, "slow bot never saw a CATCH_UP note"
    assert all(m >= 1 for _, m in s.catchups), s.catchups

    # 4) determinism with instant bots (same seed, same code)
    def h(seed):
        e = Engine([("A", Recorder()), ("B", Recorder())], seed=seed,
                   max_ticks=400, scenario=FAST)
        r = e.run()
        return hashlib.sha256(json.dumps(e.replay(r), sort_keys=True,
                                         separators=(",", ":")).encode()).hexdigest()
    assert h(21) == h(21), "pipelined determinism broke (instant bots)"

    # 5) orders that arrive after elimination are recorded, not applied
    eng3 = Engine([("A", Slow(2.0)), ("B", Recorder())], seed=5, max_ticks=400,
                  scenario=FAST)
    for _ in range(150):
        eng3.tick()
    eng3.fleets[0].alive = False           # dies with a call in flight
    time.sleep(2.2)                        # let the doomed reply land...
    for _ in range(250):
        eng3.tick()                        # ...so the next boundary harvests it
    assert any("eliminated" in d.get("thoughts", "") for d in eng3.decisions
               if d["fleet"] == 0), "late orders of a dead fleet must be noted"

    # 6) depth 0 (the default) has no pipe state at all
    eng4 = Engine([("A", Recorder()), ("B", Recorder())], seed=5, max_ticks=200)
    eng4.run()
    assert eng4._pipe == {} and not any("ot" in d for d in eng4.decisions)
    print("PASS test_pipeline (catch-up)")


if __name__ == "__main__":
    main()
