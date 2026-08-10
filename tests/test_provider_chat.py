"""LLMAdmiral._chat ↔ provider-ladder integration (wringer pass 3): outcome
classification happens HERE, not in Ladder — a connection refusal must count
against the error streak (2), not the timeout streak (3/5), and a failed
CANARY probe must neither consume a retry attempt nor fail the call."""
import io
import json
import os
import sys
import urllib.error

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "sim"))
import providers
from providers import Ladder
from llm import LLMAdmiral

FAILS = []


def ok(cond, msg):
    if cond:
        print(f"PASS {msg}")
    else:
        FAILS.append(msg)
        print(f"FAIL {msg}")


PROVS = [
    {"id": "do", "label": "DO", "base_url": "https://prim", "key": "k1",
     "builtin": True},
    {"id": "bt", "label": "BT", "base_url": "https://fall", "key": "k2",
     "model_map": {"m1": "BT/m1"}},
]
FB = {"timeout_streak": 3, "timeout_streak_pipelined": 5,
      "error_streak": 2, "canary_minutes": 10}


class SpyLadder(Ladder):
    def __init__(self):
        super().__init__([dict(p) for p in PROVS], dict(FB))
        self.reports = []

    def report(self, model, idx, is_canary, outcome):
        self.reports.append((idx, is_canary, outcome))
        return super().report(model, idx, is_canary, outcome)


GOOD = {"choices": [{"message": {"content": "hello"},
                     "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1}}


class Seq:
    """Scripted urlopen: each entry is an Exception to raise or a dict body."""
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.urls = []

    def __call__(self, req, timeout=None):
        self.urls.append(req.full_url)
        o = self.outcomes.pop(0)
        if isinstance(o, Exception):
            raise o
        body = json.dumps(o).encode() if isinstance(o, dict) else o

        class R:
            def __enter__(self2): return self2
            def __exit__(self2, *a): return False
            def read(self2): return body
        return R()


def adm():
    a = LLMAdmiral("m1")
    a.timeout = 5
    return a


def http_err(code):
    return urllib.error.HTTPError("u", code, "boom", {}, io.BytesIO(b""))


import urllib.request as _ur
MSGS = [{"role": "system", "content": "s"}, {"role": "user", "content": "u"}]

# 1. connection refused = ERROR streak, not timeout streak
lad = SpyLadder()
providers._LADDER = lad
_ur.urlopen = Seq([urllib.error.URLError(ConnectionRefusedError(111))])
try:
    adm()._chat(MSGS)
    ok(False, "connection refusal should raise")
except Exception:
    pass
st = lad._st("m1")
ok(lad.reports[-1] == (0, False, "error"),
   f"connection refusal reports 'error' ({lad.reports[-1]})")
ok(st["errs"] == 1 and st["touts"] == 0,
   f"…and lands on the error streak (errs={st['errs']} touts={st['touts']})")

# 2. a genuine timeout still reports 'timeout'
lad2 = SpyLadder()
providers._LADDER = lad2
_ur.urlopen = Seq([TimeoutError("t")])
try:
    adm()._chat(MSGS)
except Exception:
    pass
ok(lad2.reports[-1] == (0, False, "timeout"),
   f"TimeoutError reports 'timeout' ({lad2.reports[-1]})")

# 3. a failed canary probe neither consumes an attempt nor fails the call:
#    demoted to rung 1, canary due -> probe (rung 0) 500s -> same call still
#    answers from rung 1
lad3 = SpyLadder()
providers._LADDER = lad3
lad3.report("m1", 0, False, "429")           # -> rung 1
lad3._st("m1")["canary_at"] = 0              # canary due NOW
seq = Seq([http_err(500), GOOD])
_ur.urlopen = seq
text, tin, tout, ms = adm()._chat(MSGS)
ok(text == "hello", "the call succeeds despite the failed probe")
ok(seq.urls == ["https://prim/chat/completions",
                "https://fall/chat/completions"],
   f"probe hit the primary, the answer came from the fallback ({seq.urls})")
ok(lad3._st("m1")["idx"] == 1, "a failed probe never promotes")
ok((0, True, "error") in lad3.reports, "the probe was reported as a canary")

# 4. a 200 with an unparseable body is reported (a degenerating stack must
#    demote, not stay 'healthy') and the call retries through
lad4 = SpyLadder()
providers._LADDER = lad4
_ur.urlopen = Seq([b"<html>gateway melted</html>", GOOD])
text, *_ = adm()._chat(MSGS)
ok(text == "hello", "garbage 200 retries to a good reply")
ok((0, False, "error") in lad4.reports,
   f"the garbage body was reported as an error ({lad4.reports})")

providers._LADDER = None                     # leave no global state behind
print(f"FAILURES: {len(FAILS)}")
sys.exit(1 if FAILS else 0)
