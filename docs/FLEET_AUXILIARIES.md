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
- Self-hosters without a token keep the local executor (default).

## As built (decisions on the open questions)

1. **Token custody**: config API — `POST /api/aux-config {do_token, callback_base,
   callback_auth?, size?, region?, max_concurrent?, max_age_h?}`, stored 0600 in
   the library dir (never served by any route); `AUX_*` env works too. Use a
   custom-scoped DO token (droplet create/read/delete + tag) — the token-management
   API can't mint these, so it's a one-time control-panel step.
2. **No warm pool**: pure cold start (~60s) against 30-40 minute games.
3. **The auxiliary gets the inference key** — served in job.json over TLS
   (bearer-gated), NOT embedded in user_data, so it never appears in droplet
   metadata. And the provider keys are **least-privilege**: a worker receives
   the builtin DigitalOcean key plus only the ladder rungs that serve ITS
   job's models (`_providers_json` scopes by the config's models), so a seized
   disposable droplet yields at most the keys that job used — never the whole
   ladder. The local runner, on the flagship itself, gets the full ladder.
   `callback_base` MUST be `https://` (enforced on both the env and
   `aux.json` config paths) or job.json would ship these keys in cleartext.
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

## Pause / resume (aux v3, 2026-08-07)

Workers stay outbound-only: the flagship rides **commands** home on the
responses to the worker's own `live` callbacks (the agent heartbeats an empty
`live` post every ~10s when the stream is quiet, so commands land even
mid-window). `POST /api/pause` queues a one-shot `pause` command → the agent
plants `pause.flag` → the runner freezes at the next window boundary (exit 75)
→ the agent gzips `checkpoint.json` and ships it to `POST /api/aux/<jid>/paused`
→ the flagship stores it, marks the job `paused`, and **destroys the droplet —
a paused aux job costs $0**. `POST /api/resume` provisions a fresh worker with
the same bearer; its agent probes `GET /api/aux/<jid>/checkpoint.json.gz`,
thaws, and continues mid-game (`run_config --resume`). That is the
spot-instance workflow: pause on an interruption notice, resume when capacity
or prices return.

**Tournaments pause too (2026-08-12).** A tournament freezes a **two-level
checkpoint**: every in-flight matchup lane writes its own series checkpoint
(the same freeze a standalone series does), then the tournament writes one
`checkpoint.json` embedding them all plus the bracket state — completed
matchup records, standings, carried memos. One pause request fans in across
parallel lanes (each freezes at its next window boundary) and the tournament
checkpoint is written **last**, so it can never point at a lane that is not
on disk. Resume rebuilds the schedule deterministically from the config,
replays nothing that is recorded, thaws each frozen lane mid-game, and plays
on. A lane whose checkpoint did not survive resumes from its completed-game
rows — the worst case is losing one partially-played game per lane, never
the run. The old "tournaments cannot pause" exemption and its 72h runaway
ceiling are gone.

**Fail-safe, not fail-dead.** A pause the runner cannot honor — the
checkpoint write fails — is *refused*: the run logs `pause_refused` and keeps
playing. Exit 75 with nothing on disk is no longer a reachable state (that
gap killed champions-cup-1 at 6/50 and champions-cup-2 at 33/50). Belt and
braces on the worker: `aux_agent` retries an unreadable checkpoint before
reporting anything, and every finished game is already in the library —
`POST /api/reconcile-tournament {"name", "write"?}` (or
`scripts/reconcile_tournament.py <dir> --write`) rebuilds a bracket's records
and standings from the replays on disk. A rebuilt matchup only gets a winner
the games mathematically decide; the champion field is never invented.

**Worker rotation is a Server-tab setting**: ♻ worker rotation on/off + the
age cap in hours (default 8, range 1–168), stored in `library/rotation.json`
via `POST /api/aux-rotation` — separate from `/api/aux-config` so changing it
never re-posts the DO token. The watcher re-reads it every pass, so a change
applies to live jobs. At the cap the job is checkpointed onto a fresh droplet
(the cap re-arms on thaw); rotation off means no age cap at all.

Checkpoints are **plain JSON** (2026-08-08): `Engine.freeze()` records only the
mutable runtime state; `Engine.thaw()` overlays it onto a freshly-constructed
engine on CURRENT code, so a checkpoint written by an older build resumes
tolerantly (missing fields keep current defaults, unknown fields are ignored,
conn programs recompile from source — one the new grammar rejects is dropped
with a warning to its admiral). No side ever deserializes executable state.
Resume-with-anything-but-success **preserves the checkpoint**: a failed run or
worker with a checkpoint on disk returns to `paused` (with the error logged)
instead of `failed` — fix the cause and resume again. `POST /api/resume
{"where": "local"}` finishes a paused aux job on the flagship box (operator
trade-off, e.g. during a droplet-create outage).

## Public showcase (no-login spectator links)

A finished — or live — series/match can be published to a public object-store
bucket and watched by anyone, no login and no download. Configure the bucket
with `SHOWCASE_ACCESS_KEY` / `SHOWCASE_SECRET_KEY` / `SHOWCASE_ENDPOINT` /
`SHOWCASE_BUCKET` / `SHOWCASE_REGION` (or `POST /api/showcase-config` with the
same fields — stored 0600 in the library). Then:

- **`POST /api/showcase {"series"|"match": <name>}`** (the 🌐 button on the
  ⛵ Games row) uploads a spoiler-free copy of the replay(s) plus a small hub
  page and returns a public link.
- A **live** series publishes as it plays: each flush mirrors to numbered
  `<prefix>/NNNNN.jsonl` chunks with a tiny `<prefix>/state.json` cursor, and
  the public **spectator player** tails them — `player.html?livejsonl=<prefix>`.
  It cold-joins to the current game (not game 1) and carries `games_expected`
  for the "game X of Y" header.

The viewer's URL params are **same-origin only** (a guard rejects absolute or
protocol-relative values): `?replay=<path>` (a stored replay), `?series=<name>`
(a bundle, opens on the latest game), `?live=<job>` (the operator live tail),
`?livejsonl=<prefix>` (the public live tail). Bucket objects get public-read
ACLs; nothing else on the flagship is exposed.

## API-outage circuit breaker (2026-08-09)

*This applies to **local** runs too, not just cloud auxiliaries — it's
documented here because the machinery grew alongside the fleet, but a
self-hoster with no droplets still gets the auto-pause-and-probe behavior.*

A run whose LLM admirals ALL fail with transport/API errors (connection
refused, 5xx, timeout — NOT a truncated or unparseable reply, which means the
API answered) for `outage_pause_windows` consecutive **fully-reported** windows
(default 2, admirals section, 0 = off) **auto-pauses itself**: the engine
freezes exactly like an operator pause, the checkpoint carries an
`auto_pause: {reason, at}` field, and the job lands in `paused` with the
reason in its log. Instead of burning game time on stale orders through a
provider outage, the game stops costing anything.

Recovery is automatic: the flagship's `_auto_resume_loop` probes every
outage-paused job back to life every `FLOTILLA_AUTORESUME_S` (default 600s)
through the same dispatch as `POST /api/resume` — local and aux jobs alike.
The outage streak is frozen WITH the game, so a probe that finds the API
still down replays exactly ONE window before re-pausing (cheap probes); the
first healthy window resets the streak and the run just keeps playing. A
human pause has no `auto_pause` field and is never touched by the prober.
Aux resume-provisioning failures during a probe leave the job paused for the
next probe, and initial aux droplet-creates now retry 4× with backoff
(~8 min) before failing a launch — a transient DO API outage killed a series
at the starting line on 2026-08-08; neither failure mode does now.
