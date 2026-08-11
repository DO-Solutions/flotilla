#!/usr/bin/env python3
"""The Game contract + World protocol (split Stage 4).

The engine's promise to a second game: incompleteness fails LOUDLY at
registration or class definition — with a list of what is missing — never as
an AttributeError three subsystems deep. These pin that promise.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "sim"))
sys.path.insert(0, os.path.join(HERE, ".."))

from engine import contract                    # noqa: E402
from engine.sim import SimBase                 # noqa: E402
import run_config                              # noqa: E402,F401 — Flotilla registers

fails = 0


def ok(cond, msg):
    global fails
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        fails += 1


flotilla = contract.game()

# ---- registration is validated, field by field ----
ok(flotilla.name == "flotilla" and callable(flotilla.engine),
   "Flotilla registered through the contract")
ok(flotilla.presets and "trawler" in flotilla.presets,
   "presets ride the contract")
ok(flotilla.ship_stats == ("speed", "hold", "guns", "armor", "hull", "lookout"),
   "designer stat names ride the contract")

try:
    contract.Game(name="halfgame", engine=lambda: None)
    ok(False, "incomplete registration must raise")
except TypeError as e:
    ok("missing" in str(e) and "schema" in str(e) and "digest_for" in str(e),
       f"incomplete registration names every missing piece ({str(e)[:80]}…)")

class _FakeSchema:                      # lacks resolve()
    SCHEMA = {}
    section_resolve = defaults = staticmethod(lambda *a: {})

try:
    contract.Game(name="x", engine=lambda: None, bots={},
                  schema=_FakeSchema, digest_for=lambda *a, **k: "")
    ok(False, "a schema without resolve() must be refused")
except TypeError as e:
    ok("resolve" in str(e), "schema shape is validated")

g2 = contract.Game(name="min", engine=lambda: None, bots={},
                   schema=type("S", (), {"SCHEMA": {}, "resolve": lambda *a: {},
                                         "section_resolve": lambda *a: {},
                                         "defaults": lambda *a: {}}),
                   digest_for=lambda *a, **k: "")
ok(g2.api_reference is None and g2.presets == {} and g2.ship_stats == (),
   "optional pieces default harmlessly")

# on_set binders fire immediately when a game is already registered
seen = []
contract.on_set(lambda g: seen.append(g.name))
ok(seen and seen[-1] == flotilla.name,
   "a late binder is called with the already-registered game")
# restore Flotilla as the registered game (g2 was never set)
contract.set_game(flotilla)

# ---- the World protocol is enforced the moment a subclass is created ----
try:
    class _Incomplete(SimBase):
        def tick(self):
            pass
    ok(False, "an incomplete SimBase subclass must fail at class definition")
except TypeError as e:
    ok("summary_for" in str(e) and "_apply_actions" in str(e),
       f"World-protocol violation lists what is missing ({str(e)[:80]}…)")


class _Complete(SimBase):
    def tick(self): pass
    def summary_for(self, fleet): return {}
    def _apply_actions(self, fleet, actions): pass
    def _frame(self): pass
    def live_header(self): return {}


ok(True, "a complete subclass defines cleanly")

print("FAILURES:", fails)
sys.exit(1 if fails else 0)
