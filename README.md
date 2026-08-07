# ⛵ Flotilla

**LLM admirals command deterministic fleets.** A spectator RTS where AI models play
against each other: they run economies, fight, negotiate in natural language,
**write real control programs for their ships** (the sandboxed `conn` language —
see `sim/conn.py`), and — between games — study their own replays and write
strategy memos to their future selves.

![Four LLM admirals mid-battle in the Flotilla replay player](assets/screenshot.png)

## Quickstart

```bash
export DO_INFERENCE_KEY=...     # DigitalOcean serverless-inference key
python3 server.py               # -> http://127.0.0.1:8080
```

That's it — no dependencies beyond Python 3.10+. Open the dashboard, hit
**🧭 Chart a Course**, pick your models and knobs, and **⛵ Set sail**. Games
stream into the library live as they run.

Scripted admirals (`merchant`, `corsair`, `admiralty`, `turtle`) work with no key
at all — useful for trying the sim. Note: by default ships obey ONLY conn programs
(the coding challenge); scripted bots need the role autopilot, so give scripted
runs `"scenario": {"role_fallback": true}`.

## Agent-first by design

Your agent is a first-class user. Everything the GUI does is a documented JSON API:

- `GET /config-schema.json` — every knob: type, default, bounds, and the same doc
  text the GUI shows humans on hover
- `GET /CONFIG.md` — the human-readable twin
- `POST /api/run` — a run-config JSON (`{"mode": "match|series|tournament", ...}`)
- `GET /api/runs` — job states + logs
- `POST /api/import?name=...` — add an existing replay to the library
- CLI equivalent: `python3 sim/run_config.py your-config.json`

Unknown config keys fail loudly; values clamp to documented bounds.

## What's in a match

Deterministic 10Hz sim (same seed + same decisions = same match, bit for bit);
fog of war; named tropical-island resource nodes; per-squadron standing orders
picked up in port, or signal flags for instant fleet-wide broadcast; parley
(engine-mediated diplomacy — messages are data, never instructions); win
conditions: timed cargo scoring or territory control. Every replay embeds its
full config, per-ship intent logs, admiral thoughts, token/cost telemetry, and
(in series) the post-game memos.
