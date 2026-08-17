# Spectator surfaces

Everything a viewer learns about *what just happened* comes from one place: the
event vocabulary in `viewer/index.html`. The timeline tooltip uses it today; the
event feed, the on-map effects and the anchors Historic Moments cites are meant
to use the same formatter. Phrase a beat once and every surface agrees.

Full design + sequencing: `SPECTATOR_PLAN.md` in the ops repo.

## The vocabulary

| piece | what it is |
|---|---|
| `EV_RANK` | how LOUD a beat is — 3 = the match turned on it, 2 = worth interrupting for, 1 = context, 0 = never surfaced. An unknown kind is 0, so a new engine event is silent until someone gives it a voice. |
| `EV_ICON` | one glyph per surfaced kind |
| `evFleet(id)` | fleet id → the admiral's label |
| `evPlace(x, y)` | a point → the territory it sits in, by the ENGINE's rule (Chebyshev, ties by straight-line then lower id). `null` in Bounty/Conquest, which have no seats — callers must cope |
| `describeEvent(e)` | `{t, rank, icon, fleet, text}`, or `null` for anything muted |

It reads the engine's event stream and nothing else. Nothing in the sim, the
POV/fog rules, or the replay pipeline reads *it*.

**`by` is a FLEET id** on both `sink` and `flag_sunk` — `sim/core.py` credits the
attacker's *fleet*, not the ship. Reading it as a ship id names the wrong
admiral as the killer, quietly and plausibly.

Example output, from a real replay:

```
KimiK3 sank Qwen3.5's trawler at Tahaa Waters
Qwen3.5 scuttled a lancer                      (cause: scuttle — not a kill)
KimiK3 took Tahaa Waters from Qwen3.5          (prev holder, vs "claimed" when unheld)
KimiK3 destroyed Qwen3.5's flagship
```

## Timeline tooltip

The timeline already drew marks and already jumped on click. Hovering now says
*what* a mark is — the question a spectator actually has, and the one that makes
finding a moment for a video cut fast.

It honours the spoiler shield: with spoilers on, a mark ahead of the playhead is
neither drawn nor hoverable, so the tooltip cannot leak what the timeline is
hiding. It is hidden entirely in `?broadcast=1` — nothing that follows a cursor
belongs in a capture.

## Testing

`tests/test_spectator.py` extracts the vocabulary from `viewer/index.html` **by
name** and runs it in node against the engine's real event shapes — a copy would
keep passing after the viewer changed. It covers the phrasing of every surfaced
kind, the muting of the high-volume ones, place naming with and without seats,
and the tooltip's tick→pixel hit-test including the spoiler rule.

## Event feed

The answer to *"stuff happens on the map and unless you're staring at the right
spot you'd never know."* A compact strip on the map logging the big beats as
they happen — FPS kill feed, strategy flavoured.

It renders from `describeEvent`, so a beat reads identically in the feed, in the
tooltip, and in anything built later. It is **deliberately visible in a
capture**: a viewer not staring at the right patch of sea otherwise never learns
that anything happened.

The window is derived from the playhead — beats within `ttlS` game-seconds
*behind* it — so scrubbing backwards shows what the feed said at that moment
rather than an append-only log that only makes sense played forward.

### Fog is a contract, not a preference

In a POV view the feed may only report what that admiral could know. Get this
wrong and it narrates the other fleet's private business over the top of a
fog-of-war display.

| kind | rule |
|---|---|
| `sink` / `flag_sunk` | own or allied always; otherwise only if the ship was VISIBLE at that frame — including the **previous** frame, because a ship is already gone from the frame it sinks in |
| `parley` | only the two fleets on the wire |
| `signal` | own/allied — a hoist is your own fleet's business |
| `region` | public: territory ownership is in `state.regions` for every admiral, so reporting it leaks nothing |
| everything else | own/allied (a rival infers yard work by scouting, not by being told) |

### Skin tokens

| token | default | what |
|---|---|---|
| `feed.enabled` | `true` | off entirely |
| `feed.position` | `tl` | `tl` \| `tr` \| `bl` \| `br` |
| `feed.maxLines` | `5` | lines on screen (1–12) |
| `feed.ttlS` | `8` | game-seconds a line lingers (1–60) |
| `feed.scale` | `1` | font scale for 1080p vs 4K capture (0.5–4) |
| `feed.minRank` | `2` | `2` = the big beats, `1` adds context. Floored at 1 — rank 0 is the muted kinds and would bury everything worth reading |
| `feed.bg` `feed.ink` `feed.accent` | `""` | `""` follows the `ui` palette |

Appearance and density only. **What is worth saying lives in the shared
vocabulary, not in a skin** — a skin can restyle the feed, never rewrite the
match.

### Moving it out of the way

Which corner is free depends on where the fleets happen to be on this map at
this moment — that is not a branding decision, so the viewer's settings tab has
its own **📰 feed corner** picker (four corners, or hidden). It is per-browser
and it **outranks the skin's choice**, re-applied after every skin change so
switching skins cannot quietly slide the feed back on top of a flagship. Clear
it and the skin's own choice returns.

> Not built yet: per-kind copy overrides (`feed.templates`), so Design can own
> the wording without a code change. Deferred deliberately until there is real
> copy to build against — inventing a placeholder vocabulary before anyone has
> written a line would almost certainly invent the wrong one.

## On-map VFX + event flash

"Stuff happens on the map" has a second half: even with the feed telling you
*what*, your eye still has to find *where*. The VFX rail marks the spot.

It is the sink ripples, generalized. The ripples were already the right
pattern — a time-indexed transient (`age = playhead − event tick`) fading over
a TTL, POV-filtered, drawn inside the one draw loop — so every new effect is
a **painter on that same rail**, keyed by event kind:

| kind | effect | position source |
|---|---|---|
| `sink` | the stock expanding ripple | the event's own `x,y` |
| `flag_sunk` | double ring + core glow, fleet-colored, biggest + longest | the fleet's flagship history at that tick |
| `region` | color wash over the territory's cells as it flips | `S.cellRegion` (the ownership-tint cell map) |
| `parley` | dashed link line between the two flagships | both fleets' current flagship positions (broadcast `to:"all"` draws nothing — no single far end) |
| `signal` | dashed pulse ring at the harbor | flagship history |
| `treaty` | SOLID link line + rings at both flagships — official, steadier than the parley whisper | both signatories' flagship positions |
| `treaty_end` | the line snaps: two halves retract from a widening middle gap; red on betrayal, dashed on an open dissolution | both flagship positions |

Three of the five kinds carry no coordinates in the event record — positions
resolve through caches the viewer already keeps for hit-testing, which is why
this lives in `draw()` and not in the shared vocabulary.

**Event flash**: any rank ≥ 2 beat the playhead just crossed gets a short
bright contracting ring, painted *after* the ships so it can't be buried under
a hull. The eye goes where the ring is; that is its whole job.

**Edge indicator**: when the map is zoomed and a flashing beat is off-screen,
an arrow sits on the screen edge (along the ray from screen center, so it
reads as "over there") pointing at the beat. This is the "minimap ping"
without a minimap — the `minimap` skin tokens paint the *timeline*; there is
no spatial overview map to ping. At zoom 1 nothing is ever off-screen, so the
indicator only exists while navigating.

### Fog, again

Every effect passes `evVisible` **at the event's frame** before painting —
the same per-kind contract the feed enforces, and the same test the old
ripple block ran by hand. A POV view that flashed an enemy sink the admiral
never saw would leak through motion what the feed carefully refuses to say.

### Skin tokens

One nested object per effect under `fx`; each key optional, each effect
independently disableable — a busy 8-fleet match with everything on is noise.
`color: ""` derives (the fleet's color, or the stock ripple ink). `ttlS` is
**game seconds** (scaled by `tick_hz`), so an effect lasts the same
story-time at any playback speed.

| token | default | keys |
|---|---|---|
| `fx.sink` | on, 3s | `enabled` `color` `ttlS` (0.2–30) `scale` (0.25–4) |
| `fx.flagSunk` | on, 6s | `enabled` `color` `ttlS` `scale` |
| `fx.region` | on, 4s | `enabled` `color` `ttlS` `alpha` (0.02–1) |
| `fx.parley` | **off** | `enabled` `color` `ttlS` `width` (0.5–6) |
| `fx.signal` | **off** | `enabled` `color` `ttlS` `scale` |
| `fx.treaty` | on, 4s | `enabled` `color` `ttlS` `width` |
| `fx.treatyEnd` | on, 3s | `enabled` `color` `ttlS` `width` |
| `fx.flash` | on, 1.2s | `enabled` `color` `ttlS` |
| `fx.edgeArrow` | on | `enabled` `color` `size` (4–40) |

Defaults are deliberately conservative: the two rank-1 chatter kinds (parley,
signal) ship **off**; Design turns knobs from here — their visual language
lands as token values, not code changes. The flat `fx.fog` / `fx.tail` tokens
are unchanged.

`tests/test_fx.py` covers the table parity (every kind has a painter, a skin
group, and a rank), the ttl conversion, the edge-arrow geometry, token
sanitation (including the boolean-`"false"` trap and clamp ranges), and a real
`applySkin` run proving nested fx objects survive the merge as objects.

## Audio stingers

**Off and empty by default — this repo ships no sound.** A skin carries clips
the way it carries sprites, so there is nothing to license here; whatever a
skin embeds is that skin's responsibility, and its license belongs next to it.

| token | default | what |
|---|---|---|
| `audio.enabled` | `false` | the skin's default stance |
| `audio.volume` | `0.7` | master gain, 0–1 (a real 0 is honoured) |
| `audio.sink` `flagSunk` `region` `parley` `signal` | `""` | one clip per beat kind — keys mirror the `fx` effect groups |

Clips are **`data:audio/...` URIs only** (mpeg, ogg, wav, webm, mp4), capped
at 280k URI chars ≈ **200 KB of decoded audio** each — base64 inflates ~33%,
so budget accordingly. Remote URLs never load, the same self-contained rule
as every other skin asset.

### How firing works

The engine narrates **forward playback only**: each animation frame fires the
beats whose ticks the playhead just crossed, and any seek/scrub resets the
window — jumping around a replay never replays history as a burst of noise.
Rules on top:

- **Fog** — a beat passes `evVisible` at the *event's* frame before it
  sounds. A POV listener hears only what that admiral could see; a stinger
  for an unseen sink would leak through sound what the display hides.
- **Coalescing** — 150 ms per-kind floor. A fleet action that lands eight
  sinks in one tick is one stinger, not a machine gun.
- **Precedence** — the viewer's 🔊 toggle (settings panel, per-browser)
  outranks the skin's `enabled`, in both directions — same rule as the feed
  corner. A skin with no clips is silent regardless of every switch.

### Arming (autoplay policy)

Browsers refuse audio before a user gesture. Any click arms the context —
▶ Play counts — and until then a small "🔊 click anywhere to enable sound"
chip shows whenever stingers are wanted but not yet armed. That chip is
mostly for the **broadcast operator**: arm before the take, or OBS captures
silence.

`tests/test_audio.py` proves the asset rule, the token merge, the precedence
table, and the window semantics (forward-only, fog-filtered per event,
coalesced) — `audioBeats` returns the fired groups precisely so all of it is
testable without a speaker.

## Historic Moments

A **narrator model** — independent of who played — writes each LLM admiral's
story through a series, and separately across a whole tournament: key
decisions, turning points, and where the admiral's own words show it
surprised, frustrated, triumphant or resigned. Lives in
`keelspring/moments.py`, invoked from `run_series`/`run_tournament` as a
sibling of the debrief and `sim_feedback` passes.

### The anchor rule

Every beat must cite `{game, tick}` copied exactly from the anchor list the
narrator was shown (the big-beat event record); tournament beats cite
`{matchup, game, tick}` drawn from the already-validated series beats. A beat
that cites any other moment is **dropped mechanically** before it renders,
and the drop is counted (`dropped_beats`). The payoff runs both ways: no
hallucinated drama survives, and every surviving beat renders as a **link
into the replay at that moment** (the viewer's `?t=` deep link) — the
jump-to-the-key-moment tool for cutting video.

### Grounding

A player's narration material is its **own** campaign journal excerpts, its
memos, parley it was party to, and the public event record. A rival's
thoughts never enter. The emotional read comes from the admiral's own words
or is omitted — the prompt forbids inferring feelings from a scoreboard.

### Config + cost

| knob | where | default |
|---|---|---|
| `series.historic_moments` | per-run config | **off** |
| `series.historic_moments_model` | config, else the **Server tab** default (injected at submit) | `""` |
| `series.historic_moments_timeout_s` / `_chars` | config | 300 / 2500 |
| `tournament.historic_moments` | per-run config | **off** — requires the series switch (the finale synthesizes from the series stories) |

Validation is loud at the boundary: narration enabled with no model anywhere,
or the tournament switch without the series one, is refused at submit — not
skipped silently at the end of an hours-long run.

**Cost is first-class:** narration calls (players × series, + one per
participant for the bracket arc) ride the pre-flight estimate under a
dedicated `narrator` calibration profile, count against the Server-tab cost
ceiling (`lim_cost`), and land in the aux worker's least-privilege provider
scoping — a narrator nobody plays still ships its provider key.

### Rendering

`dash/tournament.html`: the tournament arcs render in a 📜 card under the
standings; each completed matchup card gains a REVEAL-gated 📜 story chip
that lazily fetches its `series.json`. **Everything is behind the spoiler
shield — a story reveals the outcome by construction.**

`tests/test_moments.py` proves the anchor rule, the material fog, the
validation caps, both narrate passes over a stubbed transport, the config
refusals, the estimate math, the provider scoping, the keystore round-trip,
and an end-to-end scripted series writing the section.
