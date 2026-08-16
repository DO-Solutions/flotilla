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

> Not built yet: per-kind copy overrides (`feed.templates`), so Design can own
> the wording without a code change. Deferred deliberately until there is real
> copy to build against — inventing a placeholder vocabulary before anyone has
> written a line would almost certainly invent the wrong one.
