# The engine/game split — design (2026-08-10)

Goal: the agent-sim engine becomes a standalone tool any game can slot into;
Flotilla becomes its first game. Constraints, in priority order:

1. **Invisible at every interface.** The dashboard, viewer, HTTP API, CLI
   (`python3 sim/run_config.py …` — the aux workers call it), config schema
   output, and replay format behave byte-for-byte as they do today, and
   performance does not change. Anything that cannot be made identical stops
   the work for discussion.
2. **The engine is complete on its own.** A new game imports the engine and
   provides only game things; it never reaches into Flotilla.
3. **Net simplification** — especially for someone building the second game.
4. **Agent-first, robust, built for scale** — the contract is documented and
   machine-checkable, not folklore.

## What the audit of the current coupling found

- `server.py` (3,309 lines) is already game-agnostic except **three call
  sites**, all `core.PRESETS` (the ship-designer endpoints), plus cosmetic
  strings. It runs games as subprocesses of `sim/run_config.py` — a process
  boundary, not an import boundary.
- `sim/run_config.py` (698) is the run orchestrator (match/series/tournament,
  memos, parallelism). Game enters via `Engine`, `BOTS`, and the schema.
- `sim/providers.py`, `sim/llm.py`, `sim/series.py` are generic already: the
  game reaches the LLM layer only as engine-provided TEXT (rules digest,
  summaries, fog digests).
- `sim/conn.py` (549) = a generic interpreter (parser, evaluator, instruction
  budget, error pedagogy) + Flotilla's SENSORS/ACTIONS tables and examples.
- `sim/config_schema.py` = generic schema machinery (resolve, aliases,
  show_if, doc generation) + Flotilla's knob content.
- `sim/core.py` (2,933) is the interleave: game rules (movement, combat,
  economy, territory, shipyards, parley) woven through engine machinery
  (decision windows, action isolation + warnings, program execution,
  freeze/thaw checkpoints, frame emission, determinism discipline).
- `sim/replay_codec.py`: the frame SHAPE (s/f/n/r rows) is Flotilla's; the
  envelope (versioning, live stream, canonicalization contract) is engine.
- `dash/` + `viewer/` are Flotilla's UI. They speak HTTP + the replay format
  only — no Python coupling. They stay with the game.

## Target layout (same repo, two packages + a compat surface)

```
engine/                # game-agnostic; a boundary test enforces that it
  __init__.py          # never imports flotilla (or legacy sim game modules)
  sim.py               # the run loop scaffolding: decision windows +
                       # pipelining, action isolation + warnings/feedback,
                       # program scheduling, freeze/thaw, frame emission,
                       # seeded-RNG determinism discipline
  program.py           # the interpreter core (parser/eval/budget/errors);
                       # sensor + verb TABLES are injected by the game
  runner.py            # match/series/tournament orchestration, memos,
                       # parallel lanes, checkpoints, cost telemetry
  llm.py               # the LLM admiral layer (fairness rule intact)
  providers.py         # the provider fallback ladder
  schema.py            # schema machinery: resolve/aliases/show_if/doc-gen
  contract.py          # the Game contract (below) — documented AND
                       # runtime-validated at game registration
flotilla/              # the game: rules (from core.py), sensors/actions
                       # tables + conn examples, bots, presets, frame codec,
                       # schema knob content, briefing/digest text
sim/                   # COMPAT SURFACE — kept forever-cheap, not deprecated:
                       # each file re-exports from engine/ or flotilla/ so
                       # every documented entry point, test import, and the
                       # aux workers' `sim/run_config.py` CLI keep working
server.py              # unchanged surface; its 3 PRESETS sites go through
                       # the game registration instead of `import core`
dash/, viewer/         # untouched
```

Not a separate repo yet: a second repo is real ongoing friction (versioning,
release sync) purchased before any second game exists. The boundary is
enforced by a test instead; cleaving the repo later is mechanical because the
import graph will already be clean.

## The Game contract (`engine/contract.py`)

A game registers one object providing:

| Provides | Flotilla's implementation |
|---|---|
| `name`, `flavor` strings (UI copy, log vocabulary) | "flotilla", nautical |
| schema knob content (engine machinery renders/resolves it) | today's 72 knobs |
| the World: `tick()`, action appliers, summaries/briefings | core.py rules |
| program tables: sensors, verbs, functions, teaching examples | conn tables |
| scripted bots | bots.py |
| unit presets + designer stat names | PRESETS, SHIP_STATS |
| frame shape: encode/decode + the canonicalization rule | replay_codec |
| fog digest for memos (what an admiral could have known) | series.digest_for |

Everything else — server, library, showcase, aux fleet, live streaming,
pause/resume, tournaments, providers, cost ceilings, determinism harness —
is the engine, and a new game gets it all for free.

## The invisibility gate (built FIRST, run at every stage)

`tests/test_split_identity.py` + a golden harness:

1. **Golden replays**: N seeded configs spanning modes (match, series w/
   memos off, territory, domination, designs/refits, parley) are run at the
   pre-split HEAD and their replay bytes hashed. Every stage must reproduce
   the hashes exactly. Byte-identity is the whole point — it subsumes every
   subtler compatibility claim.
2. **Schema identity**: `CONFIG.md` + `config-schema.json` regenerate
   byte-identical.
3. **Perf floor**: a scripted-bot benchmark (ticks/sec, fixed seed) must not
   regress beyond noise (±5%); measured per stage, recorded in the PR.
4. The full existing suite (35 files) + the jsdom dash/viewer harnesses.

## Stages (each an ordinary public PR, each gated green)

- **Stage 0** — the gate itself: golden-replay harness + perf benchmark +
  the engine-boundary import test (asserting a boundary that doesn't exist
  yet is trivially green; it hardens as modules move).
- **Stage 1** — move the already-generic modules: providers, llm,
  schema machinery, interpreter core → `engine/`; game tables stay behind
  and are injected. `sim/*.py` become re-export shims. (1a: providers ✓.
  1b finding: `series.py` is ONLY the fog digest + its CLI — game-side per
  the contract, so it stays put; the memo-carry scaffolding lives in
  run_config and moves at Stage 2.)
- **Stage 2** — `run_config` → `engine/runner.py` with the game plugged via
  the contract; `sim/run_config.py` stays as the CLI shim (aux unaffected).
- **Stage 3** — the core.py split (the careful one): engine machinery
  (windows, action isolation, program scheduling, freeze/thaw, frame
  emission) → `engine/sim.py` as a base the game extends; Flotilla's rules
  remain in `flotilla/rules.py`. Mechanical extraction, no behavior edits;
  golden hashes are the proof. Anything that can't move without changing
  bytes stays put and is flagged for discussion.
- **Stage 4** — server.py's 3 PRESETS sites → contract lookup; the runtime
  contract validator; `docs/ENGINE.md` (the "build a game" guide — written
  agent-first: complete enough that an agent can scaffold a game from it
  without reading Flotilla); wringer over the whole split.

## Explicitly out of scope

New tools, new features, renames of user-visible anything, changes to the
replay format or config schema, and the engine's public NAME (chosen after
the split, per Darian).

## Epilogue (2026-08-12)

All five stages shipped, every one byte-identical. The engine is named
**Keelspring** (`keelspring/`), its fairness subsystem the **remontoire** —
see docs/KEELSPRING.md for the build-a-game guide.
