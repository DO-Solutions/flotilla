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
python3 sim/run_config.py <(echo '{"mode":"match","bots":["merchant","corsair"]}')
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

All stdlib, no pytest needed. The CI workflow runs exactly these (note: Actions
may be disabled on the upstream repo by org policy — it runs fine on forks, and
every upstream release is gated on the same suite before it ships).

## The one rule that matters: the schema is the single source of truth

Every tunable lives in `sim/config_schema.py` (`SCHEMA`). The engine defaults,
the dashboard's Configure form, the ⓘ hover text, `CONFIG.md`,
`config-schema.json`, the per-replay settings stamp, and the rules digest the
LLM admirals read are **all generated from it**. To add a knob:

1. Add one entry to `SCHEMA` (with `d`/`t`/bounds and a `doc` string an agent
   can act on — the doc IS the UI and the API reference).
2. Consume it in the engine via `self.cfg["your_knob"]`.
3. Add or extend a test.

Never hardcode a game number anywhere else; `resolve()` rejects unknown keys
loudly, and that's a feature.

## Other conventions

- **Determinism is sacred** in the engine: seeded PRNG, integer math,
  insertion-order iteration. Same config + seed = identical replay, byte for
  byte. Anything that breaks that breaks replays, series, and tests.
- **Replays are the contract** between the sim and the viewer. If you add a
  frame/event field, keep old replays loading (feature-detect, don't assume).
- **Agent-first**: anything a human can do in the GUI must be possible for an
  agent via documented JSON (`/api/run`, `config-schema.json`). If you add a UI
  affordance, add its API twin.
- Match the existing style (~100-col Python, no type-annotation ceremony,
  comments only for constraints the code can't express).

## Reporting issues

A great bug report includes the replay JSON (or the config + seed — that
regenerates it exactly) and what you expected vs saw.
