"""Compat shim — the module moved to keelspring/providers.py (split Stage 1).
sys.modules aliasing keeps BOTH import paths the same module object, so
module-level state (ladders, demotion counters) can never fork."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from keelspring import providers as _mod          # noqa: E402
sys.modules[__name__] = _mod
