# Fleet Auxiliaries — worker servers for running sims

*Status: BUILT 2026-08-07 (same day as the design — see "As built" below).*

## Why

Today the web droplet runs matches itself. That works (matches are I/O-bound), but
it couples three things that want to be independent:

1. **Deploys kill runs.** Updating the app restarts the server, which kills any
   in-flight series and strands its games in the private work dir. Every feature
   shipped this week queued behind a running series.
2. **Box sizing is a compromise** between "cheap always-on website" and "big
   enough for the heaviest sim."
3. **Concurrency is capped** by one box's memory.

The fix is the nautical org chart: the web droplet is the *flagship* (harbor,
library, spectators); sims run on *fleet auxiliaries* — disposable worker
droplets provisioned per job and scuttled when it lands.

## Architecture

```
dashboard / agent
      │  POST /api/run {…, "executor": "auxiliary"}
      ▼
flagship (server.py)
      │  1. DO API: create droplet (smallest that fits, tagged flotilla-aux)
      │  2. user_data: install app tarball + the job's run-config + a callback token
      ▼
auxiliary droplet
      │  runs sim/run_config.py exactly as the flagship would
      │  streams: POST /api/aux/<job>/live   (the live.jsonl lines, bearer-authed)
      │  results: POST /api/aux/<job>/game   (each finished game -> live publish)
      │  final:   POST /api/aux/<job>/done   (full artifacts)
      ▼
flagship files results into the library (same _normalize path), then
      │  3. DO API: destroy the auxiliary (also a reaper sweep for orphans:
      │     any flotilla-aux droplet older than max_age with no live job dies)
```

- **Push, not pull**: the auxiliary calls home over HTTPS with a per-job bearer
  minted at provision time. The flagship never SSHes anywhere; auxiliaries need
  no inbound ports at all.
- **Live watch just works**: the aux streams the same live.jsonl lines the local
  executor writes; `/api/live/<job>` serves them identically.
- **Failure containment**: aux dies mid-run → job marked failed with the last
  streamed window preserved; reaper destroys the corpse. Flagship deploys no
  longer touch running jobs at all.

## Security

The flagship needs a DO token that can create/destroy droplets — an internet-facing
box holding infrastructure credentials, so scope it hard:

- **Custom-scoped token**: `droplet:create`, `droplet:read`, `droplet:delete` only
  (DO custom scopes) — no account, DNS, Spaces, or app access.
- **Tag-fenced delete**: the reaper only ever destroys droplets tagged
  `flotilla-aux` that it created (IDs recorded per job); never delete by name.
- **Spend fence**: hard cap on concurrent auxiliaries (knob, default 3) + max
  lifetime (default 6h) + smallest viable size (s-1vcpu-1gb — a 4-admiral match
  peaks ~200MB). Worst-case runaway = cap × $0.009/hr.
- Self-hosters without a token simply keep the local executor (default).

## As built (decisions on the open questions)

1. **Token custody**: config API — `POST /api/aux-config {do_token, callback_base,
   callback_auth?, size?, region?, max_concurrent?, max_age_h?}`, stored 0600 in
   the library dir (never served by any route); `AUX_*` env works too. Use a
   custom-scoped DO token (droplet create/read/delete + tag) — the token-management
   API can't mint these, so it's a one-time control-panel step.
2. **No warm pool**: pure cold start (~60s) against 30-40 minute games.
3. **The auxiliary gets the inference key** — served in job.json over TLS
   (bearer-gated), NOT embedded in user_data, so it never appears in droplet
   metadata. Better custody than the flagship's own env, in fact.
4. **No bucket keys on workers**: the aux fetches the app as a tarball from the
   flagship itself (`GET /api/aux/<job>/app.tar.gz`, per-job bearer in
   `X-Aux-Token` — `Authorization` stays free for the fronting proxy's basic auth).
5. **Live watch is transparent**: aux `live` callbacks append to the same
   live.jsonl the local executor writes; `/api/live/<job>` cannot tell the
   difference. Games file into the library per-game via the `game` callback.
6. Reaper: every 5 min, any `flotilla-aux`-tagged droplet not attached to a live
   job is destroyed; per-job watchdog enforces `max_age_h`. Flagship restart
   mid-aux-run orphans the job (in-memory bearer) — the reaper still collects the
   droplet; rerun the job. Known v1 limitation.

Use: check "⚓ run on a fleet auxiliary" in Chart a Course, or add
`"executor": "auxiliary"` to any run-config.
