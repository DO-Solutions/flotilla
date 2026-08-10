"""API-outage circuit breaker: a run where EVERY LLM admiral errors for
outage_pause_windows straight windows freezes itself (pause_reason set, run()
returns None) instead of burning game time; healthy or partially-healthy games
never trip; the streak survives freeze/thaw so a still-in-outage resume
re-pauses after ONE window; knob 0 disables; the checkpoint carries
auto_pause for the server's auto-resume prober."""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "sim"))
from core import Engine

FAILS = []


def ok(cond, msg):
    if cond:
        print(f"PASS {msg}")
    else:
        FAILS.append(msg)
        print(f"FAIL {msg}")


class FakeAdmiral:
    """Quacks like an LLM admiral to the engine: decide() attaches _usage, so
    its decisions land in eng.decisions with a u record (scripted bots don't)."""
    def __init__(self, err=None):
        self.err = err

    def decide(self, summary, rng):
        u = dict(tin=0, tout=0, ms=5, cost=0, err=self.err)
        th = f"(missed the window — {self.err})" if self.err else "holding"
        return {"thoughts": th, "_usage": u}


SCN = {"width": 48, "height": 32, "max_ticks": 3000, "window": 100,
       "warmup": False, "role_fallback": True}


def run_eng(bots, scn=None):
    eng = Engine(bots, seed=5, scenario=scn or SCN)
    r = eng.run()
    return eng, r


# 1. full outage trips at the default threshold (2 windows)
eng, r = run_eng([("A", FakeAdmiral("URLError: connection refused")),
                  ("B", FakeAdmiral("HTTP 503"))])
ok(r is None, "all-admirals-erroring run auto-pauses")
ok(eng.pause_reason and "api-outage" in eng.pause_reason,
   f"pause_reason set ({eng.pause_reason!r})")
ok(eng.t <= 100 * (eng.cfg["outage_pause_windows"] + 1),
   f"paused within K+1 windows (t={eng.t})")

# 2. one healthy admiral = no outage
eng2, r2 = run_eng([("A", FakeAdmiral("HTTP 503")), ("B", FakeAdmiral(None))])
ok(r2 is not None, "one healthy admiral -> no auto-pause")
ok(eng2.pause_reason is None, "no pause_reason on a mixed run")

# 3. scripted-only game: no signal, no pause
from bots import BOTS
eng3, r3 = run_eng([("A", BOTS["merchant"]), ("B", BOTS["merchant"])])
ok(r3 is not None and eng3.pause_reason is None,
   "scripted-only game never trips the breaker")

# 4. knob 0 disables
eng4, r4 = run_eng([("A", FakeAdmiral("boom")), ("B", FakeAdmiral("boom"))],
                   {**SCN, "outage_pause_windows": 0})
ok(r4 is not None, "outage_pause_windows=0 disables auto-pause")

# 5. freeze/thaw keeps the streak: a still-in-outage resume re-pauses after
#    ONE more window (the probe costs one window, not K)
frozen = eng.freeze()
players = [("A", FakeAdmiral("still down")), ("B", FakeAdmiral("still down"))]
eng5 = Engine.thaw(frozen, players)
ok(eng5._outage_streak >= eng5.cfg["outage_pause_windows"],
   f"streak survives thaw ({eng5._outage_streak})")
t0 = eng5.t
r5 = eng5.run()
ok(r5 is None and 0 < eng5.t - t0 <= 100,
   f"still-in-outage resume PLAYS one probe window then re-pauses "
   f"(+{eng5.t - t0} ticks)")

# 6. healthy resume: first good window resets the streak and the game finishes
eng6 = Engine.thaw(frozen, [("A", FakeAdmiral(None)), ("B", FakeAdmiral(None))])
r6 = eng6.run()
ok(r6 is not None and eng6._outage_streak == 0,
   "healthy resume resets the streak and runs to the bell")

# 7. the checkpoint carries auto_pause for the server's prober
from run_config import write_checkpoint
with tempfile.TemporaryDirectory() as td:
    write_checkpoint(td, dict(kind="match", engine=eng, seed=5,
                              scenario=SCN, prov=None))
    with open(os.path.join(td, "checkpoint.json")) as fh:
        ck = json.load(fh)
    ok(ck.get("auto_pause", {}).get("reason") == eng.pause_reason,
       "checkpoint carries auto_pause reason")
    ok(ck["engine"].get("outage_streak", 0) >= 2,
       "frozen engine carries the streak")

# 8. server side: _autopause_info reads the auto_pause field (and only that)
#    from a paused job's checkpoint — the prober's trigger
with tempfile.TemporaryDirectory() as td:
    os.environ["FLOTILLA_LIBRARY"] = td
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
    import server as srv
    wd = os.path.join(td, "_work", "j1")
    os.makedirs(wd)
    write_checkpoint(wd, dict(kind="match", engine=eng, seed=5,
                              scenario=SCN, prov=None))
    info = srv._autopause_info("j1")
    ok(info and "api-outage" in info["reason"],
       "server _autopause_info reads the outage reason")
    wd2 = os.path.join(td, "_work", "j2")     # human pause: no auto_pause
    os.makedirs(wd2)
    write_checkpoint(wd2, dict(kind="match", engine=eng2, seed=5,
                               scenario=SCN, prov=None))
    ok(srv._autopause_info("j2") is None,
       "human pause (no reason) is invisible to the prober")
    ok(srv._autopause_info("j3") is None, "no checkpoint -> no probe")
    # a payload built from an OLD checkpoint (dict(ck, engine=…)) carries a
    # stale auto_pause; write_checkpoint must scrub it or the prober keeps
    # un-pausing a run the operator deliberately paused
    wd4 = os.path.join(td, "_work", "j4")
    os.makedirs(wd4)
    write_checkpoint(wd4, dict(kind="match", engine=eng2, seed=5,
                               scenario=SCN, prov=None,
                               auto_pause={"reason": "api-outage: stale",
                                           "at": 1.0}))
    ok(srv._autopause_info("j4") is None,
       "a stale auto_pause is scrubbed when the engine pause is human")

# 8b. the auto-resume prober (one factored pass): dispatches an
#     outage-paused job once its window has aged, rate-limits the next pass,
#     and never touches a human pause
with tempfile.TemporaryDirectory() as td2:
    import server as srv2
    old_lib, old_dr = srv2.LIB, srv2._dispatch_resume
    old_jobs = list(srv2.JOBS)
    try:
        srv2.LIB = td2
        wa = os.path.join(td2, "_work", "a1")
        os.makedirs(wa)
        write_checkpoint(wa, dict(kind="match", engine=eng, seed=5,
                                  scenario=SCN, prov=None))
        # age the pause stamp past the probe window (a FRESH pause must wait)
        with open(os.path.join(wa, "checkpoint.json")) as fh:
            ckd = json.load(fh)
        ckd["auto_pause"]["at"] = 1.0
        with open(os.path.join(wa, "checkpoint.json"), "w") as fh:
            json.dump(ckd, fh)
        wh = os.path.join(td2, "_work", "a2")
        os.makedirs(wh)
        write_checkpoint(wh, dict(kind="match", engine=eng2, seed=5,
                                  scenario=SCN, prov=None))   # human pause
        j1 = dict(id="a1", name="a1", state="paused", log=[])
        j2 = dict(id="a2", name="a2", state="paused", log=[])
        srv2.JOBS[:] = [j1, j2]
        calls = []
        srv2._dispatch_resume = lambda j, where=None: (
            calls.append(j["id"]), (200, {"ok": True}))[1]
        srv2._AUTORESUME_LAST.clear()
        srv2._auto_resume_pass()
        ok(calls == ["a1"],
           f"prober dispatches ONLY the aged outage pause ({calls})")
        srv2._auto_resume_pass()
        ok(calls == ["a1"], "an immediate second pass is rate-limited")
    finally:
        srv2.LIB, srv2._dispatch_resume = old_lib, old_dr
        srv2.JOBS[:] = old_jobs
        srv2._AUTORESUME_LAST.clear()

# 9. err KIND gating: reply-shaped failures (truncation, unparseable JSON)
#    mean the API is UP — they must never trip the outage breaker
class FakeAdmiralReply(FakeAdmiral):
    def decide(self, summary, rng):
        u = dict(tin=0, tout=0, ms=5, cost=0, err=self.err, ek="reply")
        return {"thoughts": "(missed)", "_usage": u}


eng9, r9 = run_eng([("A", FakeAdmiralReply("TruncatedReply: hit limit")),
                    ("B", FakeAdmiralReply("ValueError: bad JSON"))])
ok(r9 is not None, "reply-kind errors never auto-pause")
ok(eng9.pause_reason is None, "no pause_reason on reply-kind errors")

# 10. a PARTIAL window (an admiral still in flight under pipelining) is no
#     signal — the errored half of the field must not pause the whole run
eng10 = Engine([("A", FakeAdmiral("x")), ("B", FakeAdmiral(None))], seed=5,
               scenario=dict(SCN, outage_pause_windows=2))
eng10._u_fleets = {0, 1}
eng10.t = 200
eng10._outage_eval_t = 0
eng10.decisions = [dict(t=100, fleet=0, u=dict(err="HTTP 503"))]
ok(eng10._outage_check() is False and eng10._outage_streak == 0,
   "one landed error + one in flight advances nothing")
eng10.decisions.append(dict(t=100, fleet=1, u=dict(err="HTTP 503")))
eng10._outage_eval_t = 0
eng10._outage_check()
ok(eng10._outage_streak == 1, "the FULL all-errored window advances the streak")

print(f"FAILURES: {len(FAILS)}")
sys.exit(1 if FAILS else 0)
