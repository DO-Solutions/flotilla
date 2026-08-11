#!/usr/bin/env python3
"""The engine/game boundary (docs/ENGINE_SPLIT.md).

Two guarantees, both mechanical so they can't rot into folklore:

1. Nothing under engine/ imports the game. The ban list is by MODULE NAME —
   the game's packages plus the legacy sim/ game modules — so a new engine
   file can't quietly reach for flotilla presets or bot classes.
2. The engine package imports standalone: a clean interpreter with ONLY
   engine/ on the path can `import engine`, proving completeness (a game
   author gets a working tool, not a tool with hidden Flotilla tendrils).

Before Stage 1 lands there is no engine/ yet — both checks report SKIPPED
(and say so), then harden automatically the moment the package appears.
"""
import ast
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
ENGINE = os.path.join(ROOT, "engine")

# game-side module names the engine must never import
BANNED = {"flotilla", "core", "bots", "replay_codec"}

fails = 0


def ok(cond, msg):
    global fails
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        fails += 1


def main():
    if not os.path.isdir(ENGINE):
        print("SKIPPED: engine/ does not exist yet — this gate goes live at "
              "Stage 1 (see docs/ENGINE_SPLIT.md)")
        return 0

    offenders = []
    for dirpath, _dirs, files in os.walk(ENGINE):
        for fn in files:
            if not fn.endswith(".py"):
                continue
            p = os.path.join(dirpath, fn)
            with open(p, encoding="utf-8") as fh:
                tree = ast.parse(fh.read(), filename=p)
            for node in ast.walk(tree):
                mods = []
                if isinstance(node, ast.Import):
                    mods = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    mods = [node.module]
                for m in mods:
                    if m.split(".")[0] in BANNED:
                        offenders.append(
                            f"{os.path.relpath(p, ROOT)}:{node.lineno} "
                            f"imports {m}")
    ok(not offenders,
       "engine/ never imports the game" + ("" if not offenders else
                                           " — " + "; ".join(offenders)))

    r = subprocess.run(
        [sys.executable, "-c", "import engine"],
        cwd=ROOT, capture_output=True, text=True, timeout=60,
        env={**os.environ, "PYTHONPATH": ROOT})
    ok(r.returncode == 0,
       "engine imports standalone"
       + ("" if r.returncode == 0 else f" — {r.stderr.strip()[-200:]}"))

    print("FAILURES:", fails)
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
