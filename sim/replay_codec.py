"""Replay v3 frame codec — the parse-graph shrink (REPLAY_FORMAT.md).

Encoding happens ONLY at replay()-serialization time; the engine keeps full
frames in memory and the live stream ships full rows (a joining viewer gets
frame 0 via catch-up anyway). replay_version:
  1 (absent) = full rows, plain intent events
  2          = + meta.intern intent string table
  3          = + encoded frame stream (this module)

v3 frame stream, relative to the full shape:
- ship rows drop the fleet column: [id, x, y, hull_pct, cargo] — the viewer
  derives shipId -> fleet from spawn events (every ship has one, starters
  included).
- fleet rows split: every frame carries the DYNAMIC row [id, cargo, bank,
  score]; the STATIC quartet (flag_hull, alive, hx, hy) is emitted as an
  8-col FULL row only in frame 0 and on frames where any of the four changed
  (flagship damage, elimination, relocation) — consumers forward-fill.
  A full row is distinguishable by length (8 vs 4).
- node rows: frame 0 carries the full [id, remaining] list; later frames only
  the pairs that CHANGED since the previous frame — consumers forward-fill.

decode_frames() is the reference decoder: byte-identical round-trip is proven
by test_replay_v3. The migration script and any python tooling use it; the
viewer implements the same rules in JS.
"""

FULL_FLEET_ROW = 8                         # current engine shape; pre-relocation
DYN_FLEET_ROW = 4                          # replays have 6-col rows (no hx/hy) —
                                           # full vs dynamic is len > 4, and the
                                           # codec preserves the input row length


def encode_frames(frames):
    """Full frames -> v3 stream. Pure; never mutates the input."""
    out = []
    prev_static = {}                       # fleet id -> (flag_hull, alive, hx?, hy?)
    prev_nodes = {}                        # node id -> remaining
    for i, fr in enumerate(frames):
        s2 = [[r[0], r[2], r[3], r[4], r[5]] for r in fr["s"]]
        f2 = []
        for r in fr["f"]:
            static = (r[1], r[5], *r[6:])
            if i == 0 or prev_static.get(r[0]) != static:
                f2.append(list(r))         # full row (6 or 8 col, as recorded)
                prev_static[r[0]] = static
            else:
                f2.append([r[0], r[2], r[3], r[4]])
        n2 = []
        for nid, rem in fr["n"]:
            if i == 0 or prev_nodes.get(nid) != rem:
                n2.append([nid, rem])
                prev_nodes[nid] = rem
        fr2 = {"t": fr["t"], "s": s2, "n": n2, "f": f2}
        if "r" in fr:
            fr2["r"] = [list(row) for row in fr["r"]]
        out.append(fr2)
    return out


def decode_frames(frames, ship_fleet):
    """v3 stream -> full frames. ship_fleet: {ship id -> fleet id} (from spawn
    events). The reference implementation of the forward-fill rules."""
    out = []
    static = {}                            # fleet id -> [flag_hull, alive, *tail]
    nodes = {}                             # node id -> remaining (insertion-ordered)
    for fr in frames:
        s2 = [[r[0], ship_fleet[r[0]], r[1], r[2], r[3], r[4]] for r in fr["s"]]
        f2 = []
        for r in fr["f"]:
            if len(r) > DYN_FLEET_ROW:
                static[r[0]] = [r[1], r[5], *r[6:]]
                f2.append(list(r))
            else:
                st = static[r[0]]
                f2.append([r[0], st[0], r[1], r[2], r[3], st[1], *st[2:]])
        for nid, rem in fr["n"]:
            nodes[nid] = rem
        fr2 = {"t": fr["t"], "s": s2,
               "n": [[nid, rem] for nid, rem in nodes.items()], "f": f2}
        if "r" in fr:
            fr2["r"] = [list(row) for row in fr["r"]]
        out.append(fr2)
    return out


def ship_fleet_map(events):
    """shipId -> fleet from spawn events — every ship has one (starters too)."""
    return {e["ship"]: e["fleet"] for e in events if e.get("k") == "spawn"}


def fleet_dyn(row):
    """(cargo, bank, score) from a fleet row of any length — the dynamic
    trio is present every frame; only its indices depend on the row shape."""
    if len(row) > DYN_FLEET_ROW:
        return row[2], row[3], row[4]
    return row[1], row[2], row[3]
