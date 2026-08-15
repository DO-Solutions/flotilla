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
