#!/usr/bin/env python3
"""Stage gate for the engine/game split: every golden config must reproduce
its committed byte-hashes exactly (tests/golden/manifest.json). This is the
"invisible at every interface" requirement of docs/ENGINE_SPLIT.md made
executable — run it after every restructuring commit.

Heavier than the unit suites (it plays ~6 real games, ~1-2 min): kept as its
own file so CI can group it, but it is part of the standard sweep like any
other test_*.py. The manifest regenerates ONLY via
`python3 tests/golden_harness.py generate`, which double-runs everything and
refuses to bless a nondeterministic baseline. Regenerating is a deliberate
act reserved for INTENDED behavior changes — never part of a split stage.
"""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

r = subprocess.run([sys.executable, os.path.join(HERE, "golden_harness.py"),
                    "verify"])
sys.exit(r.returncode)
