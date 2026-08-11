"""conn — the ship-programming language. Naval fact: the officer directing a
ship's movements "has the conn"; the helm executes. Exactly so here — admirals
write CONN PROGRAMS (per squadron) that have the conn and drive each ship's helm. This is the real
coding challenge: not toggling preset behaviors, but writing the control loop.

Design constraints (why not Python/Lua):
- DETERMINISTIC: pure expression evaluation, hard instruction budget per tick —
  replays stay byte-reproducible.
- SAFE: a purpose-built interpreter, no host access of any kind — LLM-authored
  code runs on any self-hosted server without a sandbox escape surface.
- LEGIBLE: line-numbered errors, and every action a ship takes records
  "program L<n>: <action>" as its intent — debuggable from the replay.

Grammar (one statement per line, evaluated TOP-DOWN every tick per ship):
  # comment
  mem <name> = <number>         persistent per-ship variable, initialized once
  set <name> = <expr>           assignment — executes whenever the line is reached
  when <expr>: <action>         first true `when` fires its action; run ends
  when <expr>: set <n> = <e>; …[; <action>]   compound body: sets run, then the
                                optional action fires (ends the run if present)
  default: <action>             same as `when 1:`

Actions:  helm.goto(x, y) · helm.gather() · helm.attack() · helm.flee()
          · helm.home() · helm.hold()
Exprs:    numbers · sensors · mem.<name> · + - * / % · == != < <= > >= ·
          and or not · ( ) · min(a,b) max(a,b) abs(a) sign(a) ·
          dist(x1,y1,x2,y2)

The SENSORS/ACTIONS tables below are the single source of truth: the interpreter,
the API reference injected into admiral prompts, and the docs all derive from them.
"""
import difflib
import math
import re

SENSORS = {
    "self.x": "your ship's x", "self.y": "your ship's y",
    "self.hull_pct": "hull % (0-100)",
    "self.cargo": "cargo aboard", "self.hold_cap": "cargo capacity",
    "self.power": "your combat power",
    "self.speed": "your speed stat",
    "self.docked": "1 inside your harbor circle else 0",
    "self.tick": "current game tick",
    "self.full": "1 if your hold is full (cargo >= capacity)",
    "self.rank": "your index among your squadron's LIVING ships (0,1,2… by age) — split lanes with it",
    "self.count": "how many living ships your squadron has",
    "self.stuck": "ticks wedged AT SEA (unable to move); 0 while docked",
    "self.idle": "ticks AT SEA without gaining cargo; 0 while docked",
    "harbor.x": "your harbor x", "harbor.y": "your harbor y",
    "harbor.dist": "distance to your harbor",
    "terr.id": "territory games: the id (state.regions) of the territory your ship stands in; -2 when the match has no territories. EDGE PLAY: sail toward a seat and `when terr.id == <target>: helm.hold()` stops the ship one step inside the border — claim a territory from its rim without sailing to the middle",
    "terr.owner": "territory games: fleet index holding the territory your ship stands in; -1 unclaimed, -2 when the match has no territories. Every cell belongs to its NEAREST territory seat (Chebyshev distance; ties by straight-line distance, then the lower id)",
    "terr.mine": "1 if the territory you stand in is held by you or a teammate — 'hold this point until it is ours' is `when terr.mine: helm.hold()`",
    "terr.capture": "capture progress % (0-99) of the claim underway in the territory you stand in; 0 when no capture is running",
    "enemy.found": "1 if an enemy ship is in YOUR ship's sight",
    "enemy.x": "nearest visible enemy x", "enemy.y": "nearest visible enemy y",
    "enemy.dist": "distance to that enemy",
    "enemy.laden": "1 if it is carrying cargo",
    "enemy.power": "its combat power",
    "enemy.stronger": "1 if it outguns you",
    "enemy.count": "how many enemy ships are in your sight right now",
    "ally.found": "1 if a fleet-mate is in sight",
    "ally.dist": "distance to the nearest fleet-mate",
    "node.found": "1 if your charts show a stocked island",
    "node.x": "nearest believed-stocked island x", "node.y": "…y",
    "node.dist": "distance to it", "node.stock": "believed stock there",
    "node.kind": "0 = fish shoal (regenerates), 1 = wreck (finite)",
    "rival.found": "1 if orders.target_fleet names a living rival",
    "rival.x": "that rival's harbor x", "rival.y": "that rival's harbor y",
    "rival.flag_x": "nearest hostile FLAGSHIP x (harbors are on the map)",
    "rival.flag_y": "nearest hostile flagship y",
    "rival.flag_dist": "distance to the nearest hostile flagship (destroy it to eliminate that fleet)",
    "rival.flag_hull": "that flagship's hull — revealed ONLY when THIS ship is close enough to scout it, else -1 (each ship reads its own sightings; a scout cannot share it — get in close and risk its guns)",
    "rival.yard_busy": "that fleet's shipyard works in progress (builds+refits+repairs) — revealed at the same close scouting range as flag_hull, and likewise only to THIS ship, else -1 (read their production tempo up close)",
    "orders.rally_x": "rally x from your squadron's standing orders",
    "orders.rally_y": "rally y from standing orders",
    "orders.aggression": "aggression from standing orders (0-3)",
    "orders.retreat": "retreat_hull_pct from standing orders",
}

ACTIONS = {
    "goto": (2, "sail toward (x, y)"),
    "gather": (0, "gather if ON a stocked island (else holds)"),
    "attack": (0, "close on the nearest visible enemy SHIP (guns fire automatically in range)"),
    "assault": (0, "close on the nearest hostile FLAGSHIP and batter it — the only way to eliminate a fleet. Flag damage needs adjacency (cheb<=1); the flag's own guns reach 2 cells, so expect to trade hulls. Send mass."),
    "flee": (0, "run directly away from the nearest visible enemy"),
    "home": (0, "make for your harbor (deposits/repairs/new orders in the circle)"),
    "hold": (0, "hold position"),
}


import os as _os
import sys as _sys

_sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
import engine.program as _prog           # noqa: E402

# install Flotilla's vocabulary into the interpreter (same dict objects, so
# conn.SENSORS and the machinery's view can never diverge), then re-export
# the machinery names every consumer already uses
_prog.SENSORS = SENSORS
_prog.ACTIONS = ACTIONS
from engine.program import (                              # noqa: E402,F401
    BUDGET, ConnError, FUNCS, MAX_LINES, Program, _fin, compile_program)


def api_reference(examples=5):
    """The API card admirals receive — generated from the same tables the
    interpreter runs on, so documentation can never drift from behavior.
    examples (2-5) = how many worked examples to include: the first two are
    always shown; the PHASE MACHINE / FLAGSHIP HUNT / PACK HUNTER walkthroughs
    are the difficulty ladder (rank presets trim them — an Admiral gets the
    bare tables, a Captain gets the full tutorial)."""
    s = "\n".join(f"  {k:<18} {v}" for k, v in SENSORS.items())
    a = "\n".join(f"  helm.{k}({', '.join('xy'[i] for i in range(n)) if n else ''})"
                  f"  — {d}" for k, (n, d) in ACTIONS.items())
    ladder = ""
    if examples >= 3:
        ladder += """
Example (PHASE MACHINE — explicit state beats clever conditions; declare a
mem, branch on it, advance it):
  mem phase = 0
  when mem.phase == 0 and self.full: set phase = 1
  when mem.phase == 0 and node.found and node.dist == 0: helm.gather()
  when mem.phase == 0 and node.found: helm.goto(node.x, node.y)
  when mem.phase == 1 and self.docked: set phase = 0
  when mem.phase == 1: helm.home()
  when self.idle > 300: helm.home()
  default: helm.goto(orders.rally_x, orders.rally_y)
"""
    if examples >= 4:
        ladder += """
Example (FLAGSHIP HUNT — domination is won at the enemy flag; mass first,
press the flag, break off before you sink):
  when self.hull_pct < 30: helm.home()
  when self.count < 3: helm.goto(orders.rally_x, orders.rally_y)
  when enemy.found and enemy.dist <= 1 and rival.flag_dist > 3: helm.attack()
  default: helm.assault()
"""
    if examples >= 5:
        ladder += """
Example (PACK HUNTER — fight only with local superiority; alone, fall back
to the rally and wait for the pack):
  mem hot = 0
  when self.hull_pct < orders.retreat: set hot = 0; helm.home()
  when enemy.found and not enemy.stronger and ally.dist <= 4: set hot = 1; helm.attack()
  when enemy.found and enemy.stronger and enemy.dist < 6: helm.flee()
  when mem.hot == 1 and enemy.found: helm.attack()
  default: helm.goto(orders.rally_x, orders.rally_y)
"""
    return f"""
SHIP PROGRAMMING (the conn language) — each ship is a machine; your program HAS THE
CONN and drives its helm. Send "programs": {{"A": "<script>"}} alongside orders — same
delivery physics as orders (harbor circle / signal). One program per squadron;
every ship runs its own instance with its own mem. An empty string removes the
program (ship reverts to its standing-orders role).

Statements (one per line, run TOP-DOWN every tick):
  mem <name> = <number>      persistent per-ship variable (initialized once)
  set <name> = <expr>        assignment, executes when reached
  when <expr>: <body>        body = semicolon-separated `set`s with an OPTIONAL
                             final action, e.g.  when c: set a = 1; helm.home()
                             sets run and evaluation CONTINUES; the first ACTION
                             that fires ends the tick
  default: <body>            fallback (put it last)
  # comment

Expressions: + - * / %  · == != < <= > >=  · and or not · ( ) ·
  min(a,b) max(a,b) abs(a) sign(a) dist(x1,y1,x2,y2) — grid (Chebyshev) distance,
  the same metric every range in the game uses

SENSORS (read-only):
{s}
  mem.<name>          your own variables

ACTIONS (exactly one fires per tick):
{a}

Notes: deposits, repairs, and order pickup happen automatically in your harbor
circle; guns fire automatically at enemies in range. orders.* lets you retune a
running program's parameters each window WITHOUT rewriting code. A program that
fails to parse is REJECTED (you'll see program_rejected in events); a runtime
error makes that ship fall back to its standing orders for the tick. Budget:
{MAX_LINES} lines, {BUDGET} expression-steps per tick.

Example (a self-preserving forager):
  mem lowfuel = 0
  when self.hull_pct < orders.retreat: helm.home()
  when self.full: helm.home()
  when enemy.found and enemy.stronger and enemy.dist < 8: helm.flee()
  when node.found and node.dist == 0: helm.gather()

Example (LANE SPLIT — stop the whole squadron stacking on one shoal;
self.rank spreads ships WITHOUT signals):
  when self.full: helm.home()
  when self.stuck > 150: helm.home()
  when node.found and self.rank % 2 == 0 and node.dist == 0: helm.gather()
  when node.found and self.rank % 2 == 0: helm.goto(node.x, node.y)
  default: helm.goto(orders.rally_x, orders.rally_y)
{ladder}"""
