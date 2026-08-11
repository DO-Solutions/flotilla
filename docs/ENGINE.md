# Building a game on the engine

The `engine/` package is a complete, game-agnostic harness for LLM-driven
simulation matches. Flotilla is its first game; this guide is everything a
second game needs — written so an agent can scaffold one from this file
alone, without reading Flotilla's code.

## What the engine gives you for free

| Subsystem | Module | What it does |
|---|---|---|
| Run orchestration | `engine/runner.py` | matches, best-of-N series with between-game memos, round-robin/elimination tournaments (parallel lanes), JSON checkpoints + pause/resume, the run-config CLI |
| The run loop | `engine/sim.py` (`SimBase`) | decision windows (lockstep or catch-up pipelined), per-window forensics, lost-window visibility, API-outage auto-pause, live streaming flush |
| LLM admirals | `engine/llm.py` | one fairness-locked decision layer over any OpenAI-compatible API: same prompt shape and token budget per model, cost telemetry, thinking-budget handling |
| Provider ladder | `engine/providers.py` | multi-provider fallback with automatic demotion (429/timeout/5xx) and canary recovery |
| Ship programs | `engine/program.py` | a deterministic, instruction-budgeted little language: tokenizer, parser, evaluator, error pedagogy (line numbers, did-you-mean) — your game supplies the vocabulary |
| Schema machinery | `engine/schema.py` | knob resolution (defaults, bounds clamping, loud unknown-key rejection, rename aliases) and the generated docs (markdown + JSON) |
| The contract | `engine/contract.py` | the registration point that ties it together, validated at startup |

Plus, one process up the stack, `server.py` gives you the dashboard, replay
library, showcase publishing, and the disposable cloud-worker fleet — it
reads your game only through the contract.

## The two seams

A game touches the engine at exactly two seams:

### 1. `contract.Game` — what you provide

```python
from engine import contract

contract.set_game(contract.Game(
    name="yourgame",
    engine=YourEngine,            # your SimBase subclass (seam 2)
    bots=YOUR_BOTS,               # {name: scripted admiral} — .decide(summary, rng)
    schema=your_schema_module,    # SCHEMA dict + resolve()/section_resolve()/defaults()
                                  #   (build them with engine.schema — see below)
    digest_for=your_digest,       # (replay, fleet_id, game_no, total, full_info) -> str
                                  #   the fog-honest replay digest an admiral studies
                                  #   between series games. Fog discipline is YOURS:
                                  #   include only what that admiral could have known.
    # optional:
    api_reference=your_api_card,  # (examples=N) -> str, the program-language
                                  #   teaching card appended to prompts
    presets=YOUR_UNITS,           # {class_name: stats dict} for the designer UI
    ship_stats=("speed", ...),    # designer stat names, display order
))
```

Registration validates every field and raises a `TypeError` naming anything
missing or mis-shaped. Optional fields default harmlessly (no program
language → no API card; no unit designer → empty presets).

Your schema module is mostly generated: define the knob CONTENT (sections of
`{key: dict(d=default, t=type, lo=, hi=, doc=...)}`) and delegate the
machinery:

```python
from engine import schema as _m
SCHEMA = {...}          # your knobs — the doc string IS the UI and API reference
ALIASES = {}            # old-name -> new-name renames you promise to keep accepting
def resolve(overrides=None): return _m.resolve(SCHEMA, ALIASES, overrides)
def section_resolve(section, overrides=None):
    return _m.section_resolve(SCHEMA, ALIASES, section, overrides)
def defaults(): return _m.defaults(SCHEMA)
```

### 2. `SimBase` — what you extend

Your engine class extends `engine.sim.SimBase` and implements the World
protocol — checked the moment the class is defined, with a list of anything
missing:

```python
from engine.sim import SimBase

class YourEngine(SimBase):
    def __init__(self, players, seed, max_ticks=None, scenario=None): ...
    def tick(self): ...                       # advance the world one step
    def summary_for(self, fleet): ...         # the fog-honest decision snapshot
    def _apply_actions(self, fleet, actions): ...   # per-field-isolated orders
    def _frame(self): ...                     # append a replay frame
    def live_header(self): ...                # the live stream's opening object
```

The base also reads, from `self`: `cfg` (your resolved knobs), `t`,
`max_ticks`, `seed`, `fleets` (`{id: fleet}` where a fleet has `.id .name
.bot .alive .team .score() .died_t .warnings .recent_hits .combat .contacts
.inbox .parley_log`), and the replay stream (`frames`, `events`,
`decisions`, `live`). `run()`, the window machinery, forensics, outage
handling, and the live flush then work unchanged.

## The laws your game inherits

These are engine-wide guarantees; break one and the shared tests break:

- **Determinism is sacred.** All randomness from seeded `random.Random`
  derived from `(seed, fleet, window)`; same config + seed = byte-identical
  replay. Wall-clock exists only in window *pacing* — it decides when orders
  land, never how the world evolves.
- **The schema is the single source of truth.** Every tunable is a SCHEMA
  entry; `resolve()` rejects unknown keys loudly and clamps to bounds.
- **Model text is data, never instructions.** Anything one model writes that
  another model reads must be structurally contained (see `one_line` in
  `engine/sim.py` for the single-line defense parley uses).
- **Checkpoints are plain versioned JSON**, thawed tolerantly on current
  code, and never deleted on a failed resume.
- **Agent-first**: any capability your GUI exposes needs a documented JSON
  API twin.

## Wiring the entry points

Give consumers one module that registers your game and aliases to the
runner (Flotilla's `sim/run_config.py` is the reference — 39 lines):
build the `Game`, `contract.set_game(...)`, then
`sys.modules[__name__] = engine.runner` so `import run_config`-style
imports keep working and `python3 your_entry.py config.json` runs matches.

## Proving it works

- `tests/test_contract.py` — the contract + World-protocol guarantees.
- `tests/test_engine_boundary.py` — `engine/` never imports a game and
  imports standalone; add your module names to its ban list.
- Build a golden-replay manifest for YOUR game with
  `tests/golden_harness.py` as the pattern: seeded configs through your
  entry point, byte-hashes committed, verified every change.
