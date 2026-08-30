"""Provider request dialects: z.ai speaks OpenAI-style reasoning_effort, not
vLLM's chat_template_kwargs — and its GLM serving cannot fully disable
thinking, so think-off maps to reasoning_effort=low there. The adjustment is
per-attempt: a fallback hop to a non-z.ai rung must send the vanilla body."""
import io
import json
import os
import sys
import urllib.error

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "sim"))
import llm                                # noqa: E402
import providers                          # noqa: E402
from providers import Ladder              # noqa: E402

FAILS = []


def ok(cond, msg):
    if cond:
        print(f"PASS {msg}")
    else:
        FAILS.append(msg)
        print(f"FAIL {msg}")


PROVS = [
    {"id": "z-ai", "label": "Z.ai", "base_url": "https://api.z.ai/api/paas/v4",
     "key": "zk", "model_map": {"glm-9": "glm-9"}},
    {"id": "do", "label": "DO", "base_url": "https://inference.example/v1",
     "key": "dk", "builtin": True},
]
FB = {"timeout_streak": 3, "timeout_streak_pipelined": 5,
      "error_streak": 2, "canary_minutes": 10}

SENT = []                                 # bodies as dicts, in call order
PLAN = []                                 # per-call: "ok" or an HTTP code


class _Resp:
    def __init__(self):
        self.status = 200

    def read(self):
        return json.dumps({
            "choices": [{"message": {"content": "{\"thoughts\": \"t\"}"},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1}}).encode()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def fake_urlopen(req, timeout=None):
    SENT.append(json.loads(req.data))
    step = PLAN.pop(0) if PLAN else "ok"
    if step != "ok":
        raise urllib.error.HTTPError(req.full_url, step, "err", {},
                                     io.BytesIO(b""))
    return _Resp()


_real = llm.urllib.request.urlopen
llm.urllib.request.urlopen = fake_urlopen


def admiral(model, think):
    return llm.LLMAdmiral(model, label="T", temperature=0.2, max_tokens=100,
                          timeout=5, think=think, history_chars=1000,
                          memo_chars=1000, scratchpad=False,
                          scratchpad_chars=100, warmup_timeout_s=5,
                          base_prompt="b")


def fresh_ladder():
    with providers._LADDER_LOCK:
        providers._LADDER = Ladder([dict(p) for p in PROVS], dict(FB))


try:
    # 1. think-off on z.ai: reasoning_effort=low, no chat_template_kwargs
    fresh_ladder()
    SENT.clear()
    admiral("glm-9", think=False)._chat([{"role": "user", "content": "hi"}])
    b = SENT[-1]
    ok(b.get("reasoning_effort") == "low",
       "z.ai think-off sends reasoning_effort=low")
    ok("chat_template_kwargs" not in b,
       "z.ai body drops chat_template_kwargs")

    # 2. think-on on z.ai: default effort, still no template kwargs
    fresh_ladder()
    SENT.clear()
    admiral("glm-9", think=True)._chat([{"role": "user", "content": "hi"}])
    b = SENT[-1]
    ok("reasoning_effort" not in b, "z.ai think-on leaves effort at default")
    ok("chat_template_kwargs" not in b,
       "z.ai think-on also drops chat_template_kwargs")

    # 3. a model z.ai does not serve goes to the builtin with the vanilla body
    fresh_ladder()
    SENT.clear()
    admiral("qwen-9", think=False)._chat([{"role": "user", "content": "hi"}])
    b = SENT[-1]
    ok(b.get("chat_template_kwargs") == {"enable_thinking": False},
       "non-z.ai rung keeps chat_template_kwargs")
    ok("reasoning_effort" not in b,
       "non-z.ai open-model body gains no reasoning_effort")

    # 4. per-attempt copy: a 429 on z.ai falls to the builtin, whose body must
    #    be vanilla again (no leaked reasoning_effort, kwargs restored)
    fresh_ladder()
    SENT.clear()
    PLAN[:] = [429, "ok"]
    admiral("glm-9", think=False)._chat([{"role": "user", "content": "hi"}])
    ok(len(SENT) == 2, "429 retried on the fallback rung")
    ok(SENT[0].get("reasoning_effort") == "low" and
       "chat_template_kwargs" not in SENT[0],
       "first attempt used the z.ai dialect")
    ok("reasoning_effort" not in SENT[1] and
       SENT[1].get("chat_template_kwargs") == {"enable_thinking": False},
       "fallback attempt reverted to the vanilla body")

    # 5. think-on parity: every model the OFF branch can silence, the ON
    #    branch must explicitly wake — deepseek was missing from the ON list
    #    and silently played direct in a thinking field (2026-08-30)
    fresh_ladder()
    SENT.clear()
    admiral("deepseek-9", think=True)._chat([{"role": "user", "content": "hi"}])
    ok(SENT[-1].get("chat_template_kwargs") == {"enable_thinking": True},
       "think-on sends enable_thinking=true for deepseek")
    fresh_ladder()
    SENT.clear()
    admiral("deepseek-9", think=False)._chat([{"role": "user", "content": "hi"}])
    ok(SENT[-1].get("chat_template_kwargs") == {"enable_thinking": False},
       "think-off still disables deepseek")
finally:
    llm.urllib.request.urlopen = _real
    with providers._LADDER_LOCK:
        providers._LADDER = None

print(f"\n{len(FAILS)} failures")
sys.exit(1 if FAILS else 0)
