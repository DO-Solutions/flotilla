# Inference providers — the fallback ladder

Flotilla talks to LLM admirals through an **ordered ladder** of inference
providers with automatic fallback and automatic recovery. Out of the box the
ladder is a single provider: DigitalOcean's serverless inference, keyed by
`DO_INFERENCE_KEY`. Add more from the dashboard's **🖥 Server** tab and the
engine will fall back to them when the primary struggles, then climb back the
moment it recovers.

## The ladder, in one paragraph

Every model has a current rung. From that rung:

- a **429** (rate limit) demotes it one rung immediately;
- **N timeouts in a row** demote it (`timeout_streak`, default 3 — or
  `timeout_streak_pipelined`, default 5, when the run is pipelined, because
  in-flight windows tolerate a slow reply);
- **M transport/5xx errors in a row** demote it (`error_streak`, default 2).

Demotion walks **down one rung at a time**. Recovery is different: while a
model is demoted, roughly every `canary_minutes` (default 10) one real request
is sent as a **canary** to the *best* rung that serves the model — normally the
primary. If the canary succeeds the model jumps **straight back to the top**; if
it fails it stays put and costs nothing extra. So a brief outage costs one rung
for a few minutes, and a total primary failure walks down the ladder but snaps
back the instant the primary answers again.

A provider **serves** a model when the model id appears in its `model_map` (an
explicit `{admiral-id: upstream-id}` mapping) or in its discovered `models`
list. The **builtin** DigitalOcean provider serves everything — it is where the
admiral ids come from. A model starts on the highest rung that serves it.

## The Server tab

- **Add a key** — give a label, an `https://` base URL, and the key. On add the
  server verifies the key, discovers the provider's models, and auto-maps them
  onto the admiral ids (matching `moonshotai/Kimi-K3` ↔ `kimi-k3`, etc.). You
  never enter a model map by hand; a manual entry, if you add one, wins over the
  auto-map.
- **⟳ re-check** re-verifies a key and refreshes its model list.
- **↑ / ↓** reorder rungs; **disable** keeps a key but drops it from the live
  ladder; **🗑** removes it. The builtin primary can be disabled (via the
  environment) but not deleted.
- **Fallback & canary** — the four thresholds above, editable in one place.

Keys are stored in `<library>/server-keys.json`, mode `0600`, and are **never**
committed to git, written to a log, or returned to the browser (the tab shows
only a masked hint). `POST /api/providers` returns the masked ladder;
`/api/providers-op` applies an add/remove/toggle/move/fallback change;
`/api/provider-check` re-verifies one key.

## How it reaches a run

The server serializes the enabled ladder (with real keys) into the
`FLOTILLA_PROVIDERS` environment variable for a local run, and into a worker's
`job.json` for a cloud auxiliary. A worker receives only the rungs that serve
**its** job's models plus the builtin key — a seized disposable droplet then
yields at most the keys that job used, not the whole ladder. A local run, on the
same box as the key store, gets the full ladder.

Set `FLOTILLA_PROVIDERS` yourself only for a headless run with no dashboard; the
shape is `{"providers": [{id, label, base_url, key, model_map?, models?,
enabled?, order?, builtin?}], "fallback": {timeout_streak, ...}}`. An absent or
malformed value falls back to the single builtin DO provider — byte-for-byte the
original single-provider behavior.

## Provider flavours

Model discovery is OpenAI-compatible by default (`GET /models`, `Bearer` auth):
Baseten, Fireworks, Together, Groq, OpenRouter, DeepInfra, Mistral,
DigitalOcean. Two hosts differ and are detected by their **hostname**:
`api.anthropic.com` (uses `x-api-key` + a version header) and
`generativelanguage.googleapis.com` (uses the `x-goog-api-key` header). A
base URL must be `https://` and resolve to a public address — the server
refuses loopback, link-local, and private ranges so a provider entry can't be
turned into a request-forgery probe of the host's own network.
