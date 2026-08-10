# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Flotilla: LLM admirals command deterministic fleets in a spectator RTS. **Pure Python 3.10+ stdlib, zero dependencies** — a contribution that adds a dependency needs a very good reason. One HTML file each for the replay player (`viewer/index.html`) and the dashboard (`dash/dashboard.html`).

## Commands

```bash
python3 server.py                      # dashboard + player + run executor on :8080
export DO_INFERENCE_KEY=...            # needed for LLM admirals; scripted bots need none

# Run a game from a config JSON (the one entrypoint for match/series/tournament):
python3 sim/run_config.py config.json
python3 sim/run_config.py --resume <outdir>   # resume a paused run

# Scripted-bot smoke game, no key needed:
python3 sim/run_config.py <(echo '{"mode":"match","bots":["merchant","corsair"],"scenario":{"role_fallback":true}}')
```

### Tests

No pytest — each test file is a standalone stdlib script. Sim tests run from `sim/` as the working directory; `test_server.py` runs from the repo root:

```bash
cd sim && python3 ../tests/test_territory.py    # single sim test
python3 tests/test_server.py                    # HTTP endpoint tests (in-process server)

# Whole suite, as CI (Python 3.12) runs it — CI collects failures rather than
# stopping at the first, so one broken file can't hide the rest:
cd sim && for t in ../tests/test_*.py; do [ "$(basename "$t")" = test_server.py ] || python3 "$t"; done; cd .. && python3 tests/test_server.py
```

Fast smoke set before a PR: `test_config.py`, `test_elimination.py`, `test_parley.py`, `test_territory.py`, `test_server.py`.

`test_viewer_replay.py` is the one test needing **node** (test-only): it runs the replay-canonicalization JS extracted from `viewer/index.html`, gating the viewer half of the replay contract. No node = `SKIPPED` locally; CI sets `FLOTILLA_REQUIRE_NODE=1` so a missing node fails instead of silently skipping.

### Regenerating generated docs

`test_config.py` fails if the committed `CONFIG.md` / `config-schema.json` drift from the schema:

```bash
python3 sim/config_schema.py > CONFIG.md
python3 sim/config_schema.py --json > config-schema.json
```

Balance/determinism harness: `python3 sim/harness.py` (N-seed FFA, double-run hash check, winrate/snowball stats).

## Hard rules

- **Determinism is sacred** in the engine: seeded `random.Random`, integer math only, ships iterated in id order. Same config + seed = byte-identical replay (the harness verifies via double-run hash). Wall-clock exists in exactly one place: pipelined window pacing, which affects only when decisions land, never how they replay.
- **The schema is the single source of truth.** Every tunable lives in `SCHEMA` in `sim/config_schema.py`. Engine defaults, the dashboard Configure form + hover text, `CONFIG.md`, `config-schema.json`, the per-replay settings stamp, and the rules digest shown to LLM admirals are all generated from it. To add a knob: one `SCHEMA` entry (the `doc` string IS the UI and API reference), consume it via `self.cfg["your_knob"]`, add a test. Never hardcode a game number elsewhere; `resolve()` rejects unknown keys loudly.
- **Replays are the contract** between sim and viewer — versioned (v3), encoded by `sim/replay_codec.py`. A frame-shape change touches: the codec both directions **and** the 8-vs-4 fleet-row length discriminator, the viewer's `ingestFrame` + accessors, and a round-trip case in `tests/test_replay_v3.py`. The viewer canonicalizes v1/v2/live to v3 at load, so readers never branch on version. See `docs/REPLAY_FORMAT.md`.
- **Agent-first**: anything a human can do in the GUI must be possible via documented JSON API. A new UI affordance needs its API twin. The full route table lives in the `server.py` module docstring.
- Style: ~100-col Python, no type-annotation ceremony, comments only for constraints the code can't express.

## Architecture

- **`sim/core.py`** — the deterministic 10Hz tick engine (`Engine`). Balance tunables (tick rates, ship presets, costs) live at the top of this file; config-schema knobs arrive via `resolve()`. Admirals (scripted or LLM) implement the same contract: `decide(summary, rng) -> actions dict`; a slow/broken admiral is treated as a lazy one (orders stand).
- **`sim/conn.py`** — the sandboxed ship-programming language admirals write (purpose-built interpreter: deterministic, instruction-budgeted, no host access). Its SENSORS/ACTIONS tables are the single source of truth for the interpreter, the prompt-injected API reference, and the docs. By default ships obey ONLY conn programs; scripted bots need `"scenario": {"role_fallback": true}`.
- **`sim/llm.py`** — LLM admiral decision layer over OpenAI-compatible chat completions (DO serverless inference by default). Fairness rule: every model gets the same system prompt, summary shape, and token budget. Price table for cost telemetry lives here (`FLOTILLA_PRICES` overrides).
- **`sim/providers.py`** — the provider ladder: ordered inference providers with automatic demotion on 429/timeouts/5xx and canary fall-forward recovery. Configured via `FLOTILLA_PROVIDERS` (injected from the dashboard Server tab's key store); absent = classic single-provider `DO_INFERENCE_KEY` behavior.
- **`sim/run_config.py`** — the one entrypoint for any run; consumes the config JSON the dashboard's Configure tab produces. `sim/series.py` adds between-game learning: each admiral studies its own replay and writes a memo carried into later games (fog discipline: a digest contains only what that admiral could know). Tournaments layer on top (round_robin / random_pairs / single_elim).
- **`server.py`** — the whole package in one process: serves dashboard + viewer + library, executes runs (local FIFO queue), live tailing, pause/resume (model-API outages auto-pause; a prober auto-resumes), the provider ladder API, bundles, and the fleet-auxiliary callback lane (`scripts/aux_agent.py` runs jobs on disposable cloud workers, pushing results home over HTTPS — see `docs/FLEET_AUXILIARIES.md`).
- **Library layout** (`FLOTILLA_LIBRARY`, default `./library` — the thing to back up): `matches/*.json`, `series/<name>/g*.json + series.json`, `tournaments/<name>/`, `bundles/*.html`; indexed by `scripts/libindex.py`.

## Docs map

`docs/ACTIONS.md` (admiral decision schema) · `docs/CONN.md` (ship language) · `docs/PROVIDERS.md` (provider ladder) · `docs/REPLAY_FORMAT.md` (v3 codec + `scripts/migrate_replays.py`) · `docs/FLEET_AUXILIARIES.md` (cloud workers, showcase) · `CONFIG.md` (generated — never hand-edit).
