# Flotilla configuration reference

Every knob, its default, bounds, and effect. Agent-first: read
`config-schema.json` (same content, machine-readable), write a config
JSON with any subset of these keys, run `python3 sim/run_config.py
your-config.json`. Unknown keys fail loudly; values clamp to bounds.
Some knobs carry a `show_if` hint in config-schema.json — purely a
UI visibility cue (the knob only matters when another knob has that
value); the engine accepts every key regardless. Renamed knobs
(regions→territories, region_*→territory_*, ship_designs→
default_designs) keep their old names as accepted aliases.

## world

| key | default | range | effect |
|---|---|---|---|
| `width` | `96` | 48–384 | Map width in cells (the viewer zooms + pans, so huge charts stay watchable). |
| `height` | `54` | 27–216 | Map height in cells. |
| `fish_sites` | `4` | 1–10 | Fish shoals per map quadrant (mirrored 4-way, so total = 4x this). |
| `fish_cap` | `40` | 5–200 | Max cargo a fish shoal holds. |
| `fish_regen_period` | `50` | 5–1000 | Ticks per +1 cargo regen on fish shoals (10 ticks = 1s). |
| `wreck_cap` | `60` | 0–300 | Cargo in each of the two contested center wrecks. |
| `start_cargo` | `30` | 0–500 | Each fleet's starting treasury. |
| `flag_hull` | `200` | 50–1000 | Flagship hull points. Destroying a flagship eliminates its fleet. |
| `harbor_r` | `3` | 1–12 | Harbor command-circle radius: any ship inside it deposits cargo, repairs, and picks up the admiral's latest standing orders. |
| `contact_ttl` | `1200` | 0–60000 | Ticks an enemy CONTACT stays on the admiral's plot after last sighting (state.enemies entries carry age_s; stale contacts keep last-known position/type/laden). 0 = only currently-visible enemies are reported. |

## economy

| key | default | range | effect |
|---|---|---|---|
| `ship_cost` | `15` | 1–100 | Cargo cost to build any ship. |
| `build_ticks` | `100` | 10–1000 | Ticks to build one ship (queue max 3). |
| `gather_period` | `4` | 1–50 | Ticks per 1 cargo gathered while sitting on a node. |
| `signal_cost` | `5` | 0–100 | Cargo cost to hoist a signal flag. |
| `signal_cd` | `3` | 0–20 | Cooldown between signal hoists, in decision windows. |
| `signal_mode` | `return_only` | return_only / preset / custom | What signal flags can say. return_only: the built-in RETURN TO PORT flag (per-squad or all) in two urgencies — return = NOW (beeline home, even through danger) and return_safe = turn back immediately with DEFENSIVE routing (any enemy met en route is evaded until clear, then homing resumes — slower, but recalled trawlers don't sail through an attack force); returned ships collect your latest orders in the harbor circle and head back out. preset: return/return_safe + user-defined named flags (signal_presets) whose baked-in orders apply instantly at sea. custom: a hoist pushes your current standing orders to every ship at sea (the classic instant push, payload capped by signal_max_chars). |
| `signal_max_chars` | `400` | 40–4000 | custom mode: max JSON characters of standing orders one hoist may push; an oversized hoist is refused. |
| `signal_presets` | `` | text | preset mode: JSON object of named flags, e.g. {"Strike": {"E": {"role": "assault", "target_fleet": 1}}, "Regroup": {"D": {"role": "guard", "rally": [20, 20]}}}. Hoisting a flag applies its orders to those squads instantly at sea AND sets them as the squads' standing orders. Invalid JSON fails loudly at match start. |
| `repair_period` | `5` | 1–50 | LEGACY trickle repair (+2 hull per this many ticks while docked, free) — active ONLY when shipyard_slots=0. With shipyards on, repairs are yard works: see repair_cost_pct / repair_ticks_pct. |
| `shipyard_slots` | `1` | 0–8 | STARTING yard slots per fleet — every BUILD, REFIT, and REPAIR occupies one slot until it completes; extra work waits its turn. Build capacity as a strategic resource: repairing battle damage competes with growing the fleet, and admirals EXPAND capacity by building more slots (shipyard_cost/shipyard_ticks, up to shipyard_max). A rival who scouts your flagship up close can read your yard activity (rival.yard_busy). 0 = legacy pre-shipyard behavior (one serialized build, instant-start refits, free trickle repairs). |
| `shipyard_cost` | `45` | 1–500 | Treasury cost to build one ADDITIONAL yard slot ({"build_yard": true}) — priced like three ships by default, a real investment that pays off in build/repair throughput. |
| `shipyard_ticks` | `300` | 10–5000 | Ticks to build an additional yard slot. Expansion is a harbor construction project, NOT yard work — it does not occupy a slot while building; one expansion at a time. |
| `shipyard_max` | `4` | 1–8 | Ceiling on a fleet's total yard slots (starting slots included). |
| `repair_cost_pct` | `40` | 0–200 | Treasury cost of a yard repair, as % of the ship's class build cost for a FULL-hull restoration; the actual charge scales with missing hull (minimum 1). Default 40: even a nearly-dead hull costs well under half a new ship — sailing damaged ships home pays. |
| `repair_ticks_pct` | `40` | 0–400 | Drydock time of a yard repair, as % of build_ticks for a FULL-hull restoration; scales with missing hull (minimum 1 tick). Default 40: even a nearly-dead hull is back at sea in under half a build time. |
| `refit_cost` | `8` | 0–100 | Cargo cost to REFIT a docked ship to another class. Refit directives are per-squadron standing orders ("refit": {"A": "frigate"}); ships convert as they dock until the directive is cleared (null). |
| `refit_ticks` | `100` | -1–2000 | Drydock time per refit: the ship is held in port this many ticks before the conversion completes (paid up front). Default 100 matches build_ticks, so converting is not an instant fleet-swap — it costs less than a new ship (refit_cost < ship_cost) but takes the same time to build (the Configure page keeps this in step if you change build_ticks). 0 = immediate. (-1 still means match build_ticks, for old configs.) |

## combat

| key | default | range | effect |
|---|---|---|---|
| `volley` | `5` | 1–50 | Ticks between shots for every gun. |
| `kill_score` | `8` | 0–100 | Score per enemy ship sunk (timed_score mode). |
| `flag_kill_score` | `150` | 0–1000 | Score for destroying a flagship (timed_score mode). |
| `leash` | `12` | 2–60 | ROLE-driven ships only: max cells a ship pursues an enemy from its station. Conn-programmed ships make their own pursuit decisions and ignore this. |
| `flag_targets` | `4` | 1–12 | How many enemy ships the flagship battery fires on per volley. The WHOLE command circle (harbor_r) is its kill zone — nearest targets first. 4 keeps a lone scout from loitering while a massed assault still lands most of its attackers. |

## pacing

| key | default | range | effect |
|---|---|---|---|
| `window` | `100` | 20–1000 | Ticks between admiral decision windows (100 = every 10s of game time). |
| `max_ticks` | `6000` | 500–60000 | Match length in ticks (6000 = 10 minutes). |
| `clock_jitter` | `600` | 0–18000 | Anti-turtle: extend the match by a seeded-random 0..N extra ticks that admirals cannot predict, so a leader can never compute the exact bell and freeze through the final stretch. Default 600 (up to a minute on the standard clock) — ON by default since 2026-08-13; 0 = the classic fixed clock. Ignored in domination (no clock there). Reproducible: the same seed always draws the same extension. |
| `score_visibility` | `banded` | exact / banded / hidden | What admirals see of RIVAL scores. banded (default since 2026-08-13) = rivals' scores rounded down to the nearest 100 — a leader can't compute that no rival can catch them, which keeps the endgame honest. exact = live totals (classic). hidden = each admiral sees only its own score. Spectators and the replay always see exact scores. |
| `territory_tick` | `50` | 10–500 | Territory mode: ticks between scoring updates (+1 point per held territory). Capture PROGRESS advances every tick regardless — this knob only paces the scoreboard. |
| `pipeline_depth` | `0` | 0–8 | CATCH-UP pipelining. 0 = classic lockstep: the sim pauses every window until the slowest admiral replies. N>0: the sim keeps sailing while admirals think — each admiral has at most ONE reply in flight; a fast admiral decides every window, a slow one misses windows and, when its reply lands, immediately gets a fresh snapshot with everything that happened while it thought (state.CATCH_UP) and rejoins live. The sim only waits when an admiral falls N whole windows behind. Reply speed becomes part of the game: quick admirals decide more often. A pipelined run also raises the provider ladder's timeout-before-fallback threshold (timeout_streak_pipelined, since in-flight windows tolerate a slow reply) — see docs/PROVIDERS.md. |
| `window_wait_s` | `45` | 1–600 | Catch-up pipelining only: wall-clock seconds each window stays open for replies. A live admiral answering within it decides every window; one that runs over misses windows until its reply arrives. Also the pacing unit for hold_full_window. |
| `hold_full_window` | `True` | text | Catch-up pipelining only: while any admiral is behind, each window lasts the FULL window_wait_s (wall-clock breathing room for stragglers to catch up without the sim racing ahead). Off = the window advances the moment every live admiral has replied — fastest sim, but laggards fall behind quicker. |

## scenario

| key | default | range | effect |
|---|---|---|---|
| `win` | `timed_score` | timed_score / territory / domination | Victory condition. timed_score ("Score"): cargo hauled + kills, highest at the bell. territory ("Territories"): control points from holding named territories. domination ("Conquest"): no clock — last admiral standing wins. The ids stay stable for configs; the labels are what the UI shows. |
| `domination_cap` | `18000` | 3000–360000 | Domination mode: safety cap in ticks (18000 = 30 minutes game time). The match has no clock — it ends when one admiral remains; if the cap is somehow reached, highest kill score wins. |
| `territories` | `16` | 4–48 | Territory mode: number of named control territories (used only when territory_size is 0). |
| `territory_size` | `0` | 0–800 | Territory mode: AVERAGE territory size in cells — the territory count becomes map area / this (clamped 4-48). 0 = use the explicit `territories` count. ~324 on the default 96x54 map ≈ the classic 16 territories (count = 5184 / territory_size). |
| `territory_symmetry` | `True` | text | Territory mode: place territory seeds in 4-way mirrored sets (both axes — every quadrant gets the same layout, like the fish shoals) so no admiral's corner of the map carves into systematically bigger territories. Off = fully random seeds. |
| `territory_capture_ticks` | `50` | 0–6000 | Ticks of SOLE possession needed to capture a territory (10 ticks = 1s; default 50 = 5s). The clock runs only while NO enemy ship (the holder included) stands in the territory — ship counts never matter. An enemy entering PAUSES progress where it is; progress resets only when every capturing ship leaves (the territory stays with its owner). The viewer draws a filling pie; admirals see contested_by + capture_pct in state.regions. 0 = instant flips (pre-timer behavior). |
| `parley` | `True` | text | Admiral-to-admiral messaging (diplomacy). Off = no communication between fleets: pure play skill, no negotiation. |
| `voyage_reports` | `True` | text | Ships file a one-line voyage report when they return to the harbor circle (time out, cargo delivered, gathered, hits taken, range) — delivered in the admiral's next state.reports and recorded for post-game review. |
| `programs` | `True` | text | Ship programming: admirals may write conn-language control programs per squadron (the real coding challenge — the API reference is injected into their prompts). Off = standing-order roles only. |
| `program_chars` | `3000` | 200–12000 | Max characters per squadron program; oversized programs are rejected at delivery. Roomy by default so admirals can write expressive, multi-phase programs. |
| `conn_examples` | `5` | 2–5 | Worked examples in the conn API reference — the difficulty ladder. 2 = the bare survival pair (forager + lane split); 3 adds the PHASE MACHINE walkthrough; 4 adds FLAGSHIP HUNT; 5 (default) adds PACK HUNTER. Rank presets trim it: a Captain sails with the full tutorial, an Admiral with the bare tables. |
| `role_fallback` | `False` | text | Built-in role autopilot for unprogrammed ships. OFF (default): ships execute ONLY conn programs — an unprogrammed ship sits idle in port (still deposits/repairs); standing orders serve purely as orders.* parameters. ON: unprogrammed ships run their standing-order role (the classic behavior). |
| `roles_allowed` | `` | text | role_fallback=on only: comma-separated subset of roles admirals may assign (e.g. "forage,scout,guard"). Empty = all of forage/scout/escort/raid/guard/blockade/assault. |
| `allow_designs` | `True` | text | Admirals may DESIGN custom ship classes mid-game ("designs": {"name": {speed,hold,guns,armor,hull,lookout}}): every stat >=1, total == design_points, max 4 classes/fleet. Built and refitted by name like built-ins; the viewer derives each class's silhouette from its stats. |
| `design_points` | `12` | 6–30 | Stat-point budget every ship class must total — built-ins use 12. Also the COST baseline under flex_design: a class's build/refit price scales by its points relative to this number. |
| `flex_design` | `False` | text | Variable-size ship design: custom classes may total anywhere from 6 points to design_points_max instead of exactly design_points, and BUILD/REFIT costs scale linearly with the class's points (a 6-point cutter costs half a standard hull; a 24-point dreadnought costs double and is one expensive target). Changes the meta — off for comparison runs. |
| `design_points_max` | `24` | 8–40 | flex_design only: the largest class anyone may design (design_points stays the exact-total rule when flex is off, and the price baseline when it's on). |
| `default_designs` | `` | text | Operator-defined EXTRA classes available to ALL fleets from the start (symmetric), on top of the four built-ins: JSON like {"corvette": {"speed":4,"hold":1,"guns":2,"armor":2,"hull":2,"lookout":1}}. Same validation as live designs; invalid JSON fails loudly at match start. The Configure page builds this from its ship roster + designer. |
| `scuttle` | `True` | text | Admirals may SCUTTLE any of their ships anywhere ('scuttle': [ship ids]) for scuttle_value treasury each — the mid-game budget-lock escape hatch. A laden ship's cargo sinks as a wreck where it goes down. |
| `scuttle_value` | `5` | 0–50 | Treasury recovered per scuttled ship. |
| `flag_salvage` | `0` | 0–2000 | Cargo spilled as a wreck at a destroyed flagship's harbor. 0 = off. A reward for making the kill (and bait for vultures who bring trawlers over). In DOMINATION the unset default becomes 50 — a big prize snowballed the first kill into the whole game; an explicit value always wins. |
| `income_amount` | `0` | 0–100 | Passive income: every fleet receives this much treasury every income_period ticks. 0 = off. Another budget-lock relief valve. |
| `income_period` | `100` | 10–5000 | Ticks between passive income payments. |
| `flag_move` | `False` | text | Flagship relocation: admirals may order their flagship (and the whole command circle with it) to sail to a new anchorage. |
| `flag_speed` | `2` | 1–6 | Flagship relocation speed, on the same scale as a ship's speed stat (a trawler is 3). |
| `island_coverage` | `0` | 0–20 | Impassable island terrain, as % of the map under land. 0 = open ocean. Low values scatter a few small dots; high values build a dense archipelago of mixed-size landmasses. Sea lanes at least 2 cells wide are always preserved between islands, and harbors/resource nodes stay clear. Charted — all admirals know the landmasses from the start. |
| `teams` | `` | text | Team play: player index groups, e.g. "0,1|2,3" for 2v2. Teammates never damage each other, share lookout vision, and win or lose together (scores sum). Empty = free-for-all. |
| `description` | `` | text | Shown to admirals in state.scenario. Leave empty to auto-generate from the win condition. |

## admirals

| key | default | range | effect |
|---|---|---|---|
| `temperature` | `0.2` | 0.0–1.5 | LLM sampling temperature for decisions. |
| `max_tokens` | `4000` | 100–32000 | Budget for the VISIBLE answer (the JSON reply). When think is on, every model automatically gets a +24k thinking allowance ON TOP — vendors count reasoning tokens inconsistently (GPT's never appear, Anthropic's and the open models' land in the completion), so capping them together silently handicapped honest reporters. 4000 fits multi-squadron conn programs. |
| `timeout_s` | `300` | 5–600 | Seconds an admiral gets to answer a decision window; slower = orders stand (missed window). Thinking models need REAL headroom — Kimi's successful thinking calls ran up to 171s in the field and 180s still lost half its windows; pipeline_depth hides the wall-clock cost. |
| `think` | `True` | text | Let every model run its native reasoning mode. ⚠ OFF is NOT symmetric: open models (Qwen/Kimi/GLM/DeepSeek) can truly be stripped of thinking, but frontier closed models (GPT-5.x, Claude) only drop to MINIMAL hidden reasoning — so off handicaps the open models. On (default) = each vendor's intended operating point; slower and pricier, fairer. |
| `history_chars` | `8000` | 0–60000 | Character budget for the admiral's full-game memory in every prompt: its campaign journal (every past thought) + the complete parley transcript. Oldest entries drop first when over budget. 0 disables in-game history. |
| `memo_chars` | `6000` | 200–20000 | Hard character cap for post-game strategy memos, opening plans, AND per-admiral custom prompts. Models are told this budget explicitly; text is cut at the limit. Raised 2500→6000 (2026-08-11): thinking-era models routinely hit the old cap mid-sentence. |
| `scratchpad` | `True` | text | Admirals get a persistent scratchpad: a freeform note they can fully rewrite any window (response field "scratchpad"), always in their context and handed to the post-game review. |
| `scratchpad_chars` | `2000` | 0–10000 | Character cap for the scratchpad. |
| `warmup` | `True` | text | Pre-game planning phase: before window 0 each admiral studies the rules + its opening view and writes an OPENING PLAN (kept in context all game, shown in the replay). |
| `warmup_timeout_s` | `120` | 10–600 | Seconds for the warmup planning call — deliberately longer than the in-game window. |
| `outage_pause_windows` | `2` | 0–50 | API-outage circuit breaker: consecutive decision windows where EVERY LLM admiral fails with a transport/API error (connection refused, 5xx, timeout) before the run auto-pauses with a checkpoint instead of burning game time. The server probes an outage-paused run every ~10 minutes and it resumes for good once a window succeeds; a resumed run that is still in outage re-pauses after ONE more bad window. 0 disables auto-pause. |
| `base_prompt` | `` | text | The base instructions every LLM admiral receives (the game-rules briefing). Empty = the built-in suggested prompt (GET /api/base-prompt to read it; the Configure page can load it for editing). Applies to ALL players; per-player custom prompts layer on top as operator directives. |

## series

| key | default | range | effect |
|---|---|---|---|
| `games` | `3` | 1–20 | Games in the series. |
| `memos` | `True` | text | Between games, each admiral studies its own record and writes a strategy memo carried into the next game. |
| `memo_history` | `True` | text | Carry the FULL series memo log (every past game's memo, headed by game number) into every pre-game plan, every in-game prompt, and every post-game debrief — so a game-1 betrayal is still on the record in game 4. Off = only the latest memo rides forward (the pre-2026-08-09 behavior). |
| `vary_seeds` | `False` | text | New map each game. Off = same map, sharper learning signal. |
| `debrief_timeout_s` | `300` | 30–900 | Seconds for the post-game memo call. Care beats speed here. |
| `debrief_full_info` | `False` | text | Death-lifted fog for the POST-GAME review: once an admiral is eliminated, its debrief digest gains the full board from the moment it died onward (all fleets' economy timelines, builds, losses) — so a beaten admiral sees WHY it lost, e.g. the winner's economy. Before its death it keeps only what its own windows saw; the window it died in is included. A SURVIVING admiral's fog is never lifted. In-game vision is unaffected. |
| `sim_feedback` | `True` | text | After the final memo, ask each admiral — as a playtester, out of character — how to improve Flotilla itself (unclear rules, missing actions, degenerate strategies, conn friction, bugs). One call per LLM admiral at series end; collected in series.json under sim_feedback for human review. |

## tournament

| key | default | range | effect |
|---|---|---|---|
| `format` | `round_robin` | round_robin / random_pairs / single_elim | Bracket structure. round_robin: every combination plays. random_pairs: seeded random pairings per round. single_elim: knockout bracket (2-player matchups, participants must be a power of 2). |
| `players_per_match` | `2` | 2 / 4 / 8 | Fleets per matchup (single_elim is always 2). |
| `games_per_match` | `1` | 1–9 | Games per matchup; odd numbers avoid ties (winner by wins, then total score). |
| `map_set` | `True` | text | On (default): every matchup plays the SAME map set — game N uses the same seed (map, islands, shoals, territory seats) in every matchup, so no pairing draws luckier water than its rivals. Off: each matchup derives its own seeds from its bracket slot (the pre-2026-08-13 behavior). |
| `rounds` | `1` | 1–10 | random_pairs only: how many rounds of fresh pairings. |
| `memo_policy` | `per_series` | none / per_series / persistent | none: fresh mind every game. per_series: memos carry within a matchup, reset between. persistent: an admiral keeps its notes across the WHOLE tournament — win a series, carry the lessons into the next bracket. |
| `full_series` | `False` | text | Off (default): a matchup STOPS once it is mathematically decided — no remaining game could change the winner (can't be won or tied). On: play every game of the matchup out regardless, for a complete sample. |
| `parallel` | `1` | 1–8 | Matchups run at once. 1 (default) = sequential, the classic behavior — one matchup finishes before the next begins, and admirals keep one shared mind across the bracket. N>1 = up to N matchups sail concurrently (single_elim still waits for each round to finish before pairing the next); every parallel matchup gets its OWN fresh admiral instances, so memo_policy=persistent forces sequential. Parallel matchups multiply concurrent LLM load AND the worker's memory (one replay in RAM per live matchup) — size the box accordingly. |
| `stagger_s` | `480` | 0–3600 | Parallel matchups only: minimum seconds between the STARTS of two concurrent matchups. The default 480 matches the longest decision-window wait, so parallel games don't all slam the model APIs in the same instant. 0 = launch together. |

