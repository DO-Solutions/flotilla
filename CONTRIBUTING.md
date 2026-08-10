# Contributing to Flotilla

Thanks for taking an interest! Flotilla is deliberately small: **pure Python 3.10+
standard library, zero dependencies**, one HTML file each for the player and the
dashboard. Keep it that way — a contribution that adds a dependency needs a very
good reason.

## Getting started

```bash
python3 server.py            # dashboard + player + run executor on :8080
export DO_INFERENCE_KEY=...  # any OpenAI-compatible endpoint key (see sim/llm.py)
```

Scripted-bot games run with no key at all:

```bash
python3 sim/run_config.py <(echo '{"mode":"match","bots":["merchant","corsair"],"scenario":{"role_fallback":true}}')
```

## Tests — run them all before a PR

```bash
cd sim
python3 ../tests/test_config.py        # schema/resolve contract
python3 ../tests/test_elimination.py   # combat + elimination paths
python3 ../tests/test_parley.py        # message routing + limits
python3 ../tests/test_territory.py     # territory win condition
cd ..
python3 tests/test_server.py           # HTTP endpoints (in-process server)
```

All stdlib, no pytest needed — the five above are a fast smoke set. **CI runs the
whole `tests/test_*.py` suite** (on Python 3.12), so run the rest before a PR too;
`test_config.py` also fails if the committed `CONFIG.md` / `config-schema.json` have
drifted from the schema — regenerate them with `python3 sim/config_schema.py >
CONFIG.md` and `python3 sim/config_schema.py --json > config-schema.json`
(markdown is the default output; `--json` switches it).

One test — `tests/test_viewer_replay.py` — needs **node**, which is the only
test-time tool outside the stdlib. It runs the replay-canonicalization JS
*extracted from `viewer/index.html` itself*, so the viewer half of the replay
contract is gated instead of trusted. Without node it prints `SKIPPED` and says
plainly that nothing was verified; CI sets `FLOTILLA_REQUIRE_NODE=1`, which turns
that skip into a failure so the gate can't quietly stop checking. The shipped
product is unchanged: pure-stdlib Python, one self-contained viewer HTML file.

CI **collects** failures instead of stopping at the first one, and the server step
runs even when a sim test fails. That is deliberate: the workflow used to abort on
the earliest failing file, and a `CONFIG.md` that was never committed hid the other
28 sim files *and* the whole server step for 12 pushes. One red X should tell you
everything that is broken, not just the alphabetically-first thing.

## The schema is the single source of truth

Every tunable lives in `sim/config_schema.py` (`SCHEMA`). The engine defaults,
the dashboard's Configure form, the ⓘ hover text, `CONFIG.md`,
`config-schema.json`, the per-replay settings stamp, and the rules digest the
LLM admirals read are **all generated from it**. To add a knob:

1. Add one entry to `SCHEMA` (with `d`/`t`/bounds and a `doc` string an agent
   can act on — the doc IS the UI and the API reference).
2. Consume it in the engine via `self.cfg["your_knob"]`.
3. Add or extend a test.

Never hardcode a game number anywhere else; `resolve()` rejects unknown keys
loudly, by design.

## Other conventions

- **Determinism is sacred** in the engine: seeded PRNG, integer math,
  insertion-order iteration. Same config + seed = identical replay, byte for
  byte. Anything that breaks that breaks replays, series, and tests.
- **Replays are the contract** between the sim and the viewer — versioned (v3),
  encoded by `sim/replay_codec.py`. A frame-shape change touches the codec (both
  directions **and** the 8-vs-4 fleet-row length discriminator), the viewer's
  `ingestFrame` + accessors, and a `tests/test_replay_v3.py` round-trip case;
  the viewer canonicalizes v1/v2/live to v3 at load so readers never branch on
  version. See `docs/REPLAY_FORMAT.md`.
- **Agent-first**: anything a human can do in the GUI must be possible for an
  agent via documented JSON (`/api/run`, `config-schema.json`). If you add a UI
  affordance, add its API twin.
- Match the existing style (~100-col Python, no type-annotation ceremony,
  comments only for constraints the code can't express).

## Reporting issues

A great bug report includes the replay JSON (or the config + seed — that
regenerates it exactly) and what you expected vs saw.
