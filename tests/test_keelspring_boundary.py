#!/usr/bin/env python3
"""The engine/game boundary (docs/ENGINE_SPLIT.md).

Three guarantees, all mechanical so they can't rot into folklore:

1. Nothing under engine/ imports the game. The ban list is by MODULE NAME —
   the game's packages plus the legacy sim/ game modules — so a new engine
   file can't quietly reach for flotilla presets or bot classes.
2. The engine package imports standalone: a clean interpreter with ONLY
   engine/ on the path can `import keelspring`, proving completeness (a game
   author gets a working tool, not a tool with hidden Flotilla tendrils).
3. No Flotilla WORLD VOCABULARY in engine runtime strings (M0 of the
   Windfall arc, 2026-09-01: the admiral briefing, memo guidance, and
   narration vocabulary all moved behind the Game contract — this check
   keeps them out). Docstrings and comments are documentation and exempt;
   what reaches prompts and behavior is string literals, and those are
   scanned. The legacy FLOTILLA_* env-var names are exempted by pattern
   (renaming the engine's env prefix is its own project).

Before Stage 1 lands there is no engine/ yet — both checks report SKIPPED
(and say so), then harden automatically the moment the package appears.
"""
import ast
import re
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
ENGINE = os.path.join(ROOT, "keelspring")

# game-side module names the engine must never import
BANNED = {"flotilla", "core", "bots", "replay_codec", "series",
          "config_schema", "conn", "run_config"}

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
       "keelspring/ never imports the game" + ("" if not offenders else
                                           " — " + "; ".join(offenders)))

    r = subprocess.run(
        [sys.executable, "-c", "import keelspring"],
        cwd=ROOT, capture_output=True, text=True, timeout=60,
        env={**os.environ, "PYTHONPATH": ROOT})
    ok(r.returncode == 0,
       "keelspring imports standalone"
       + ("" if r.returncode == 0 else f" — {r.stderr.strip()[-200:]}"))

    # 3. world-vocabulary ban in runtime strings
    LORE = ("flotilla", "trawler", "frigate", "raider", "corvette", "shoal",
            "squadron", "scuttl", "forage", "hoist")
    ENV_OK = re.compile(r"^FLOTILLA_[A-Z_]+$")

    def runtime_strings(tree):
        """Every string constant EXCEPT docstrings (module/class/def first
        statements) — the strings that can reach prompts and behavior."""
        docs = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                                 ast.AsyncFunctionDef)):
                body = getattr(node, "body", [])
                if body and isinstance(body[0], ast.Expr) and \
                        isinstance(body[0].value, ast.Constant) and \
                        isinstance(body[0].value.value, str):
                    docs.add(id(body[0].value))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and \
                    isinstance(node.value, str) and id(node) not in docs:
                yield node

    lore_hits = []
    for dirpath, _dirs, files in os.walk(ENGINE):
        for fn in files:
            if not fn.endswith(".py"):
                continue
            p = os.path.join(dirpath, fn)
            with open(p, encoding="utf-8") as fh:
                tree = ast.parse(fh.read(), filename=p)
            for node in runtime_strings(tree):
                s = node.value
                if ENV_OK.match(s):
                    continue
                low = s.lower()
                for w in LORE:
                    if w in low:
                        lore_hits.append(
                            f"{os.path.relpath(p, ROOT)}:{node.lineno} "
                            f"{w!r} in {s[:50]!r}")
                        break
    ok(not lore_hits,
       "no Flotilla world vocabulary in engine runtime strings"
       + ("" if not lore_hits else " — " + "; ".join(lore_hits[:6])))

    print("FAILURES:", fails)
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
