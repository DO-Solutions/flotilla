# Fleet Auxiliaries — worker servers for running sims (design)

*Status: DESIGN — not built. 2026-08-07.*

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

## Open questions (decide before building)

1. Token custody: env on the flagship vs a config-API secret like showcase creds.
2. Warm pool (1 idle aux for instant starts) vs pure cold-start (~50s provision)?
3. Does the auxiliary get the inference key, or proxy model calls through the
   flagship? (Proxying keeps the key off disposable boxes; adds a hop.)

## Cost of doing nothing

Acceptable until either (a) mid-run deploys hurt weekly, or (b) we want >2
concurrent series. Both were true this week.
