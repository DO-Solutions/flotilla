"""Compat shim — the module moved to engine/llm.py (split Stage 1d).

The game's contribution is the ADMIRAL DEFAULTS (the schema's admirals
section) — installed here, at the same import every consumer already uses.
sys.modules aliasing keeps both paths the same module object."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from engine import llm as _mod                     # noqa: E402
import config_schema as _cs                        # noqa: E402

_mod.ADMIRAL_DEFAULTS.update(
    {k: v["d"] for k, v in _cs.SCHEMA["admirals"].items()})
sys.modules[__name__] = _mod
