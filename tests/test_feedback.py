"""The feedback loop: every rejection/failure an admiral causes must come back
to it as a warning or an explicit prompt notice — errors you can learn from."""
import os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "sim"))
from core import Engine
from llm import LLMAdmiral, TruncatedReply
import conn


class Idle:
    name = "idle"

    def decide(self, summary, rng):
        return dict(thoughts=".")


def warnings_of(eng, fid):
    return eng.summary_for(eng.fleets[fid])["you"]["warnings"]


def test_engine_feedback():
    eng = Engine([("A", Idle()), ("B", Idle())], seed=11, max_ticks=1,
                 scenario={"programs": True, "allow_designs": True,
                           "flag_move": True})
    f = eng.fleets[0]
    # compile error -> warning with the teaching hint
    eng._apply_actions(f, {"programs": {"A": "when tx > 3: helm.hold()"}})
    w = warnings_of(eng, 0)
    assert any("COMPILE ERROR" in x and "mem tx = 0" in x for x in w), w
    # oversize program -> warning
    eng._apply_actions(f, {"programs": {"B": "x" * 99999}})
    assert any("chars" in x and "REJECTED" in x for x in warnings_of(eng, 0))
    # design junk -> warning
    eng._apply_actions(f, {"designs": {"bad": {"speed": 99}}})
    assert any("design" in x and "REJECTED" in x for x in warnings_of(eng, 0))
    # reassign/scuttle/relocate junk -> warnings
    eng._apply_actions(f, {"reassign": {"nope": "B"}, "scuttle": ["xx"],
                           "relocate": [99999, -3]})
    w = warnings_of(eng, 0)
    assert any("reassign IGNORED" in x for x in w), w
    assert any("scuttle IGNORED" in x for x in w), w
    assert any("relocate IGNORED" in x for x in w), w

    # runtime fault -> ONE deduped warning even though it fires every tick.
    # (conn defines x/0 = 0, so genuine runtime faults are budget exhaustion —
    # stub a program whose run() raises to cover the fault path directly.)
    eng2 = Engine([("A", Idle()), ("B", Idle())], seed=11, max_ticks=1,
                  scenario={"programs": True})
    f2 = eng2.fleets[0]

    class Boom:
        text = "boom"

        def run(self, sensors, mem):
            raise conn.ConnError("instruction budget (1500) exhausted")

        def init_mem(self):
            return {}

    boom = Boom()
    for sq in ("A", "B", "C"):
        f2.pending_programs[sq] = boom
    for s in eng2.ships.values():
        if s.fleet == 0:
            s.program = boom
            s.pmem = {}
    for _ in range(10):
        eng2.tick()
    faults = [x for x in warnings_of(eng2, 0) if "RUNTIME FAULT" in x]
    assert 1 <= len(faults) <= 2, faults   # one per squadron at most, deduped


def test_llm_truncation_feedback():
    a = LLMAdmiral("test-model", memo_chars=120)
    # a truncated decide -> the NEXT prompt carries the feedback line
    def boom(msgs, **kw):
        raise TruncatedReply("reply truncated at max_tokens", "partial")
    a._chat = boom
    out = a.decide({"window": 1, "you": {}, "scenario": {}}, None)
    assert "(missed the window" in out["thoughts"]
    captured = {}
    def capture(msgs, **kw):
        captured["user"] = msgs[1]["content"]
        return ('{"thoughts": "ok"}', 5, 5, 5)
    a._chat = capture
    a.decide({"window": 2, "you": {}, "scenario": {}}, None)
    assert "PREVIOUS reply was cut off" in captured["user"], captured["user"][:200]
    # and the notice does NOT repeat once acknowledged
    a.decide({"window": 3, "you": {}, "scenario": {}}, None)
    assert "PREVIOUS reply was cut off" not in captured["user"]

    # a cut memo -> marked inline in the next game's prompt + the next debrief
    a._chat = lambda msgs, **kw: ("All my secrets. " * 40, 5, 5, 5)
    out = a.debrief("digest")
    assert "…[cut@limit]" in out["memo"]
    a._chat = capture
    a.decide({"window": 1, "you": {}, "scenario": {}}, None)
    assert "CUT at the character limit" in captured["user"]
    sysmsgs = {}
    def capture_sys(msgs, **kw):
        sysmsgs["system"] = msgs[0]["content"]
        return ("short memo.", 5, 5, 5)
    a._chat = capture_sys
    a.debrief("digest")
    assert "PREVIOUS memo exceeded the character limit" in sysmsgs["system"]


def main():
    test_engine_feedback()
    test_llm_truncation_feedback()
    print("PASS test_feedback")


if __name__ == "__main__":
    main()
