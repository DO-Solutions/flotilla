"""Provider ladder: 429 demotes immediately, timeout/error streaks demote at
their thresholds, canary probes always target the BEST serving rung (normally
the primary — demotion is one rung at a time, recovery jumps to the top),
rungs that don't serve a model are skipped, and a model with no serving
fallback stays put."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "sim"))
from providers import Ladder

FAILS = []


def ok(cond, msg):
    if cond:
        print(f"PASS {msg}")
    else:
        FAILS.append(msg)
        print(f"FAIL {msg}")


PROVS = [
    {"id": "do", "label": "DO", "base_url": "u1", "key": "k1",
     "builtin": True},
    {"id": "bt", "label": "BT", "base_url": "u2", "key": "k2",
     "model_map": {"kimi-k3": "moonshotai/Kimi-K3"}},
    {"id": "t3", "label": "T3", "base_url": "u3", "key": "k3",
     "models": ["kimi-k3"]},
]
FB = {"timeout_streak": 3, "timeout_streak_pipelined": 5,
      "error_streak": 2, "canary_minutes": 10}


def fresh():
    return Ladder([dict(p) for p in PROVS], dict(FB))


# 1. 429 demotes immediately + the fallback maps the model id
lad = fresh()
i, p, m, c = lad.resolve("kimi-k3")
ok(i == 0 and m == "kimi-k3" and not c, "starts on the primary")
note = lad.report("kimi-k3", 0, False, "429")
ok(note and "demoted" in note, f"429 demotes immediately ({note})")
i, p, m, c = lad.resolve("kimi-k3")
ok(i == 1 and m == "moonshotai/Kimi-K3",
   f"fallback rung maps the model id ({i}, {m})")

# 2. timeouts demote only at the streak threshold (3)
lad2 = fresh()
ok(lad2.report("kimi-k3", 0, False, "timeout") is None and
   lad2.report("kimi-k3", 0, False, "timeout") is None,
   "two timeouts stay on the primary")
ok(lad2.report("kimi-k3", 0, False, "timeout") is not None,
   "third timeout in a row demotes")
ok(lad2.resolve("kimi-k3")[0] == 1, "now on rung 2")

# 3. a success resets the streak
lad3 = fresh()
lad3.report("kimi-k3", 0, False, "timeout")
lad3.report("kimi-k3", 0, False, "timeout")
lad3.report("kimi-k3", 0, False, "ok")
lad3.report("kimi-k3", 0, False, "timeout")
lad3.report("kimi-k3", 0, False, "timeout")
ok(lad3.resolve("kimi-k3")[0] == 0, "a success resets the timeout streak")

# 4. 5xx/transport errors demote at their own threshold (2)
lad4 = fresh()
lad4.report("kimi-k3", 0, False, "error")
ok(lad4.resolve("kimi-k3")[0] == 0, "one 5xx stays")
lad4.report("kimi-k3", 0, False, "error")
ok(lad4.resolve("kimi-k3")[0] == 1, "two 5xx in a row demote")

# 5. a model no fallback serves stays on the primary
lad5 = fresh()
note = lad5.report("glm-5.2", 0, False, "429")
ok(note is None and lad5.resolve("glm-5.2")[0] == 0,
   "unserved model stays on the primary")

# 6. a due canary probes the best serving rung; success promotes, failure stays
lad6 = fresh()
lad6.report("kimi-k3", 0, False, "429")            # -> rung 1
lad6._st("kimi-k3")["canary_at"] = 0               # due now
i, p, m, c = lad6.resolve("kimi-k3")
ok(i == 0 and c, "due canary probes the primary")
lad6.report("kimi-k3", 0, True, "timeout")         # probe fails
ok(lad6.resolve("kimi-k3")[0] == 1, "failed canary stays demoted")
lad6._st("kimi-k3")["canary_at"] = 0
i, p, m, c = lad6.resolve("kimi-k3")
note = lad6.report("kimi-k3", i, True, "ok")
ok("promoted" in (note or "") and lad6.resolve("kimi-k3")[0] == 0,
   "successful canary falls forward")

# 7. double demotion skips nothing: rung1 (map) then rung2 (models list)
lad7 = fresh()
lad7.report("kimi-k3", 0, False, "429")
lad7.report("kimi-k3", 1, False, "429")
i, p, m, c = lad7.resolve("kimi-k3")
ok(i == 2 and m == "kimi-k3", "second demotion lands on rung 3 (models list)")
# ...and from the bottom, the canary probes the PRIMARY directly — recovery
# jumps to the top, only demotion is one-rung-at-a-time
lad7._st("kimi-k3")["canary_at"] = 0
i, p, m, c = lad7.resolve("kimi-k3")
ok(i == 0 and c, f"canary from the bottom probes the primary ({i})")
note = lad7.report("kimi-k3", i, True, "ok")
ok(lad7.resolve("kimi-k3")[0] == 0, "recovery jumps straight back to the top")

# 8. pipelined mode uses its own (longer) timeout streak
os.environ["FLOTILLA_PIPELINED"] = "1"
lad8 = fresh()
os.environ.pop("FLOTILLA_PIPELINED")
for _ in range(4):
    lad8.report("kimi-k3", 0, False, "timeout")
ok(lad8.resolve("kimi-k3")[0] == 0, "4 timeouts stay when pipelined (limit 5)")
lad8.report("kimi-k3", 0, False, "timeout")
ok(lad8.resolve("kimi-k3")[0] == 1, "5th timeout demotes when pipelined")

# 9. stale reports after a rung change are ignored
lad9 = fresh()
lad9.report("kimi-k3", 0, False, "429")            # -> rung 1
ok(lad9.report("kimi-k3", 0, False, "429") is None,
   "a stale report against the old rung is ignored")
ok(lad9.resolve("kimi-k3")[0] == 1, "rung unchanged by the stale report")

# 10. a stale SUCCESS must not clear the current rung's streaks (wringer
#     2026-08-09: a slow request from an abandoned rung landing "ok" was
#     resetting the fallback rung's error count, postponing demotion forever)
lad10 = fresh()
lad10.report("kimi-k3", 0, False, "429")           # -> rung 1
lad10.report("kimi-k3", 1, False, "error")         # 1 of 2 on rung 1
lad10.report("kimi-k3", 0, False, "ok")            # stale ok from rung 0
note = lad10.report("kimi-k3", 1, False, "error")  # 2 of 2 -> must demote
ok(note is not None and lad10.resolve("kimi-k3")[0] == 2,
   "stale ok does not clear the current rung's streaks")

# 11. no builtin (operator disabled it): a model starts on the highest rung
#     that actually SERVES it, and canaries probe that rung — never a
#     third-party rung 0 that would 404 the raw id
lad11 = Ladder([dict(p) for p in PROVS[1:]], dict(FB))
i, p, m, c = lad11.resolve("kimi-k3")
ok(i == 0 and m == "moonshotai/Kimi-K3",
   f"no builtin: starts on the first serving rung ({i}, {m})")
lad11b = Ladder([{"id": "x", "label": "X", "base_url": "u", "key": "k"},
                 dict(PROVS[1])], dict(FB))
ok(lad11b.resolve("kimi-k3")[0] == 1,
   "an unmapped non-builtin rung 0 is skipped at start")

# 12. FLOTILLA_PROVIDERS env ingestion (_load): the enabled + key filters and
#     the order sort are the ONLY things honoring the operator's Server-tab
#     toggles — the server ships disabled entries into the env intact
import json as _json
os.environ["FLOTILLA_PROVIDERS"] = _json.dumps({
    "providers": [
        {"id": "a", "label": "A", "base_url": "u", "key": "k", "order": 2,
         "enabled": False},
        {"id": "b", "label": "B", "base_url": "u", "key": "", "order": 1},
        {"id": "c", "label": "C", "base_url": "u", "key": "k", "order": 3},
        {"id": "d", "label": "D", "base_url": "u", "key": "k", "order": 0},
    ],
    "fallback": {"error_streak": 7}})
lad12 = Ladder()
ok([p["id"] for p in lad12.providers] == ["d", "c"],
   f"env ingestion: disabled + keyless dropped, order honored "
   f"({[p['id'] for p in lad12.providers]})")
ok(lad12.error_streak == 7, "env fallback thresholds apply")
os.environ["FLOTILLA_PROVIDERS"] = "{not json"
os.environ["DO_INFERENCE_KEY"] = "envkey"
lad13 = Ladder()
ok(len(lad13.providers) == 1 and lad13.providers[0]["id"] == "digitalocean"
   and lad13.providers[0]["key"] == "envkey"
   and lad13.providers[0].get("builtin"),
   "malformed env falls back to the builtin DO provider with the env key")
os.environ.pop("FLOTILLA_PROVIDERS", None)
os.environ.pop("DO_INFERENCE_KEY", None)

print(f"FAILURES: {len(FAILS)}")
sys.exit(1 if FAILS else 0)
