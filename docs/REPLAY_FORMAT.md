# Replay format (v3)

A Flotilla replay is one JSON object: `meta` (config + a string-intern table),
`fleets`/`nodes` (the opening state), `events` (spawns, sinks, intents, parley,
region captures, …), `frames` (the per-tick world), `decisions` (per-window
admiral telemetry), and `result`. The format is **versioned** by
`meta.replay_version`; the current version is **3**.

## Why the frame stream is encoded

gzip handles bytes on the wire, but the browser **parses the whole replay into
an object graph** on load — a 60k-tick, 8-fleet game was a ~370 MB in-memory
graph and hundreds of ms of parse on a phone. v3 shrinks the *parsed* structure,
not just the transfer, by removing what repeats:

- **Intent strings** — tens of thousands of `intent` events carry only a couple
  hundred distinct strings. A `meta.intern` string table replaces each with a
  small integer index (interning began in v2; ~44% of raw bytes).
- **Ship-row `fleet` column** — a ship's fleet is already in its `spawn` event;
  the viewer builds a `shipId → fleet` map at load, so frames drop the column.
- **Fleet static fields** (`hx`/`hy`/`flag_hull`/`alive`) — repeat every frame
  and change rarely (never, unless `flag_move` is on). A fleet row is **8
  columns when it carries them and 4 when it doesn't** (the reader tells the two
  apart by length); the viewer forward-fills the last full row.
- **Node rows** — emitted as deltas plus a frame-0 snapshot; forward-filled.

Measured: the frame stream drops to ~74% of full size on top of interning.
Reproducibility is untouched — `meta.config` (all knobs) and `replay.run`
(per-player settings) stay verbatim; only the frame stream is encoded.

## Where the encoding happens

**Only at `replay()` serialization.** The engine keeps FULL frames in memory,
and the live stream ships full rows — so checkpoints, determinism hashes, and
the live tail are unaffected; only the *stored* replay shrinks. The codec is
`sim/replay_codec.py`:

- `encode_frames` / `decode_frames` — the round trip (the decoder is the
  reference used by the tests and the migration script). Round-trip is
  byte-identical, proven across combat deaths, mid-game wreck nodes, flagship
  relocation, elimination, and territory captures (`tests/test_replay_v3.py`).
- `ship_fleet_map` — spawn events cover every ship, starters included.
- `fleet_dyn(row)` — reads `[cargo, bank, score]` from either row length;
  `sim/series.py` uses it so it never branches on version.

The **viewer's in-memory canonical form IS the v3 format.** `ingestFrame()` at
the load/append boundary converts v1/v2 replays and the live stream (full rows,
no `replay_version` — `live_header` never stamps one) to v3 in place and
harvests the derived change-lists (`S.fleetHist`, `S.nodeHist`) in the same
pass. Readers never branch on version; they go through the accessors
`shipFleet`, `fleetDyn`, `fleetStatic`, `nodeVal`. Because the canonical form is
the exact file format, the ⬇ download serializes viewer state verbatim as a
valid v3 replay (older replays download upgraded, stamped v3). Old
downloaded/showcase bundles stay v1-embedded and still load (dual-read).

## Decision telemetry

Each entry in `decisions` records the admiral's window: `thoughts`, token/cost
`u`sage, and two forensic fields added with per-field action isolation —
`acts` (which action fields the reply actually carried) and `warns` (the
warnings that window). One malformed field is rejected **by name** and warned
about; the others still apply. So "did my `build_yard` order go in?" is
answerable straight from the replay.

## Migrating an existing library

```
python3 scripts/migrate_replays.py <library-dir> [--dry-run]
```

Encodes every replay with `meta.replay_version < 3` (and interns any
un-interned intent strings on already-v3 files), **verifying the encoded stream
decodes byte-identical to the original frames before writing**. Writes are
atomic (a failure leaves the file untouched) and preserve each file's mtime so
the library's chronology survives. Handles `.json` and `.json.gz`, skips
bundles / `_work` / showcase and metadata files, and is idempotent.

## Changing the frame shape

The frame stream is the compatibility contract. If you add or reorder a
frame/event column you must update, together: `sim/replay_codec.py` (both
directions **and** the 8-vs-4 fleet-row length discriminator — a new fleet
column silently corrupts encode/decode), the viewer's `ingestFrame` + accessors,
and a `tests/test_replay_v3.py` round-trip case. The engine determinism hashes
are on the *game*, not the serialization, so they don't move.
