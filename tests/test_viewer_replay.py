#!/usr/bin/env python3
"""Viewer replay contract — the JS half of the v3 codec.

sim/replay_codec.py has tests/test_replay_v3.py. Its JS twin in viewer/index.html
had NOTHING, so a frame-shape change or a canonicalization regression on the
viewer side passed the entire suite while breaking every replay in the library.
This closes that half:

  1. v3 stream  --JS ingest--> full frames  ==  the engine's original frames
  2. v1 full rows --JS ingest--> the SAME stream sim/replay_codec.py encodes
     (so the two implementations agree on the ENCODING rule, not just decoding)
  3. the live stream (full rows, no replay_version, appended frame by frame)
     lands on that same canonical shape
  4. the 8-vs-4 fleet-row discriminator, including 6-col pre-relocation replays

The functions under test are EXTRACTED FROM viewer/index.html BY NAME, never
copied here — a control has to share the subject's context, and a copy would
keep passing after the viewer changed.

node is a TEST-ONLY requirement (the shipped product stays pure-stdlib Python
with zero dependencies, and the viewer stays one self-contained HTML file).
Without node this prints SKIPPED and explains that nothing was verified; when
FLOTILLA_REQUIRE_NODE=1 (set in CI) a missing node is a hard failure, so the
gate can never silently skip.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "sim"))

from core import Engine                                   # noqa: E402
from bots import BOTS                                     # noqa: E402
import replay_codec                                       # noqa: E402

sys.path.insert(0, HERE)
from jsextract import extract          # noqa: E402

VIEWER = os.path.join(ROOT, "viewer", "index.html")

# every declaration the canonicalization boundary is made of. Missing or renamed
# => this test fails loudly rather than testing a subset.
NEEDED = ["DYN_F", "shipFleet", "fleetDyn", "histAt", "fleetStatic", "nodeVal",
          "ingestFrame"]

fails = 0


def ok(cond, msg):
    global fails
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        fails += 1


def run_game(seed, ticks, scenario, admirals=None):
    lineup = admirals or [(n, BOTS[n]) for n in ("merchant", "corsair")]
    eng = Engine(lineup, seed=seed, max_ticks=ticks, scenario=scenario)
    res = eng.run()
    return eng, res


class Rover:
    """Relocates the flagship on a schedule, so hx/hy actually CHANGE mid-game.

    Without this the static-change key (flag_hull|alive|hx|hy) is only ever
    exercised on flag_hull, and dropping hx/hy from it — which silently stops
    re-emitting a full row on relocation, losing the move — passes every other
    assertion in this file. A fixture that does not vary the dimension under
    test proves nothing about it.
    """
    name = "rover"

    def __init__(self, moves):
        self.moves = list(moves)

    def decide(self, summary, rng):
        return {"relocate": list(self.moves.pop(0))} if self.moves else {}


JS_HARNESS = r"""
'use strict';
const fs = require('fs');
const fx = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));

// The globals the extracted boundary reads. Same fields load() sets up.
let S = {};
function resetS(v3, cat) {
  S.cat = cat;
  S.fleetHist = {};
  S.nodeHist = {};
  S.frameMaps = [];
  S._canon = { v3: v3, ps: {}, pn: {} };
}

// ---------------- code under test, extracted from viewer/index.html ---------
__EXTRACTED__
// ---------------------------------------------------------------------------

// Rebuild a FULL frame using ONLY the documented accessors, exactly as a
// consumer must: ship fleet from the spawn catalog, the dynamic trio via
// fleetDyn, the static quartet forward-filled via fleetStatic, node stock via
// nodeVal.
function rebuild(fr, fi, wantLen) {
  const s = [];
  for (const row of fr.s) s.push([row[0], shipFleet(row[0]), row[1], row[2], row[3], row[4]]);
  const f = [];
  for (const row of fr.f) {
    const d = fleetDyn(row);
    const st = fleetStatic(fi, row[0]);
    const full = [row[0], st[1], d[0], d[1], d[2], st[2], st[3], st[4]];
    f.push(wantLen === 6 ? full.slice(0, 6) : full);
  }
  const n = [];
  for (const k of Object.keys(S.nodeHist)) n.push([Number(k), nodeVal(fi, Number(k))]);
  const out = { t: fr.t, s: s, n: n, f: f };
  if (fr.r !== undefined) out.r = fr.r;
  return out;
}

function decodeAll(frames, v3, cat, wantLen) {
  resetS(v3, cat);
  const out = [];
  frames.forEach((fr, fi) => { ingestFrame(fr, fi); out.push(rebuild(fr, fi, wantLen)); });
  return out;
}

const result = {};

// 1. v3 stream -> full frames
result.fromV3 = decodeAll(JSON.parse(JSON.stringify(fx.v3)), true, fx.cat, 8);

// 2. v1 full rows -> canonical stream. ingestFrame rewrites in place, so after
//    the pass the frames array IS the viewer's v3 encoding of the same game.
const v1 = JSON.parse(JSON.stringify(fx.full));
result.fromV1 = decodeAll(v1, false, fx.cat, 8);
result.v1Canonicalized = v1;

// 3. live: identical rows, but appended one frame at a time (liveAppend's path)
const live = JSON.parse(JSON.stringify(fx.full));
resetS(false, fx.cat);
const liveOut = [];
const accepted = [];
for (const fr of live) {
  accepted.push(fr);
  ingestFrame(fr, accepted.length - 1);
  liveOut.push(rebuild(fr, accepted.length - 1, 8));
}
result.fromLive = liveOut;

// 4. discriminator: 6-col pre-relocation fleet rows must still read as FULL
result.legacy = decodeAll(JSON.parse(JSON.stringify(fx.legacy)), true, fx.legacyCat, 6);
// and the raw static row the viewer harvested, to prove hx/hy are absent (not 0)
resetS(true, fx.legacyCat);
JSON.parse(JSON.stringify(fx.legacy)).forEach((fr, fi) => ingestFrame(fr, fi));
result.legacyStatic = S.fleetHist;
result.dynF = (typeof DYN_F === 'number') ? DYN_F : null;

// 5. territory rows must survive canonicalization untouched
result.fromTerr = decodeAll(JSON.parse(JSON.stringify(fx.terr)), true, fx.terrCat, 8);

// 6. relocation: hx/hy change mid-game, so the static-change key must include
//    them or the move is lost on both the v3 and the v1 path
result.fromReloc = decodeAll(JSON.parse(JSON.stringify(fx.reloc)), true, fx.relocCat, 8);
const relocV1 = JSON.parse(JSON.stringify(fx.relocFull));
result.fromRelocV1 = decodeAll(relocV1, false, fx.relocCat, 8);
result.relocCanonicalized = relocV1;

process.stdout.write(JSON.stringify(result));
"""


def norm_nodes(frames):
    """Node rows are forward-filled into a map on both sides; compare by id so an
    incidental iteration-order difference is not a false failure."""
    out = []
    for fr in frames:
        d = dict(fr)
        d["n"] = sorted([list(x) for x in fr["n"]])
        d["s"] = [list(x) for x in fr["s"]]
        d["f"] = [list(x) for x in fr["f"]]
        if "r" in fr:
            d["r"] = [list(x) for x in fr["r"]]
        out.append(d)
    return out


def main():
    node = shutil.which("node") or shutil.which("nodejs")
    if not node:
        msg = ("node not found — the viewer's JS replay contract was NOT "
               "verified by this run")
        if os.environ.get("FLOTILLA_REQUIRE_NODE") == "1":
            print("FAIL " + msg + " (FLOTILLA_REQUIRE_NODE=1)")
            print("FAILURES: 1")
            return 1
        print("SKIPPED: " + msg)
        print("  install node to run it, or set FLOTILLA_REQUIRE_NODE=1 to make "
              "its absence a failure (CI does).")
        return 0

    with open(VIEWER, encoding="utf-8") as fh:
        src = fh.read()
    chunks, missing = [], []
    for name in NEEDED:
        got = extract(src, name)
        if got is None:
            missing.append(name)
        else:
            chunks.append(got)
    ok(not missing,
       f"every canonicalization declaration found in viewer/index.html "
       f"(missing: {missing or 'none'})")
    if missing:
        print("FAILURES:", fails)
        return 1

    # --- fixtures from a REAL game, not hand-written rows ---
    eng, res = run_game(5, 400, {"role_fallback": True})
    full = [dict(t=f["t"], s=[list(r) for r in f["s"]],
                 n=[list(r) for r in f["n"]], f=[list(r) for r in f["f"]])
            for f in eng.frames]
    v3 = replay_codec.encode_frames(full)
    ship_fleet = replay_codec.ship_fleet_map(eng.events)
    cat = {str(k): {"fleet": v} for k, v in ship_fleet.items()}
    ok(len(full) > 50 and any(len(r) == 8 for r in full[0]["f"]),
       f"fixture is a real game: {len(full)} frames, 8-col fleet rows")

    # a territory game exercises the "r" row passthrough (owner + contest state)
    engt, _ = run_game(11, 300, {"role_fallback": True, "win": "territory",
                                 "territories": 4})
    full_t = [dict(t=f["t"], s=[list(r) for r in f["s"]],
                   n=[list(r) for r in f["n"]], f=[list(r) for r in f["f"]],
                   **({"r": [list(r) for r in f["r"]]} if "r" in f else {}))
              for f in engt.frames]
    ok(all("r" in f for f in full_t) and full_t[0]["r"],
       f"territory fixture carries r rows ({len(full_t[0]['r'])} regions)")
    v3_t = replay_codec.encode_frames(full_t)
    cat_t = {str(k): {"fleet": v}
             for k, v in replay_codec.ship_fleet_map(engt.events).items()}

    # a RELOCATING fixture: the static quartet includes hx/hy, and only a
    # flagship move exercises that half of the change key
    engr, _ = run_game(3, 500, {"role_fallback": True, "flag_move": True},
                       admirals=[("rover", Rover([(30, 20), (44, 30), (20, 12)])),
                                 ("merchant", BOTS["merchant"])])
    full_r = [dict(t=f["t"], s=[list(r) for r in f["s"]],
                   n=[list(r) for r in f["n"]], f=[list(r) for r in f["f"]])
              for f in engr.frames]
    harbors = {(r[6], r[7]) for f in full_r for r in f["f"] if r[0] == 0}
    ok(len(harbors) >= 3,
       f"relocation fixture actually moves the flagship (distinct hx/hy for "
       f"fleet 0: {sorted(harbors)})")
    v3_r = replay_codec.encode_frames(full_r)
    full_rows_r = sum(1 for f in v3_r[1:] for r in f["f"] if len(r) > 4)
    ok(full_rows_r >= 3,
       f"relocation re-emits full fleet rows after frame 0 ({full_rows_r})")
    cat_r = {str(k): {"fleet": v}
             for k, v in replay_codec.ship_fleet_map(engr.events).items()}

    # 6-col pre-relocation fleet rows: full-vs-dynamic is len > 4, and the codec
    # preserves the recorded row length
    legacy_full = []
    for i, f in enumerate(full[:12]):
        legacy_full.append(dict(
            t=f["t"], s=[list(r) for r in f["s"]],
            n=[list(r) for r in f["n"]],
            f=[[r[0], r[1], r[2], r[3], r[4], r[5]] for r in f["f"]]))
    legacy_v3 = replay_codec.encode_frames(legacy_full)
    ok(any(len(r) == 6 for r in legacy_v3[0]["f"]),
       "legacy fixture encodes 6-col full rows")

    fixtures = dict(full=full, v3=v3, cat=cat,
                    legacy=legacy_v3, legacyCat=cat,
                    terr=v3_t, terrCat=cat_t,
                    reloc=v3_r, relocFull=full_r, relocCat=cat_r)
    tmp = tempfile.mkdtemp(prefix="flotilla-viewer-test-")
    try:
        fxp = os.path.join(tmp, "fixtures.json")
        with open(fxp, "w", encoding="utf-8") as fh:
            json.dump(fixtures, fh)
        jsp = os.path.join(tmp, "harness.js")
        with open(jsp, "w", encoding="utf-8") as fh:
            fh.write(JS_HARNESS.replace("__EXTRACTED__", "\n\n".join(chunks)))
        proc = subprocess.run([node, jsp, fxp], capture_output=True, text=True,
                              timeout=120)
        if proc.returncode != 0:
            err = proc.stderr.strip().split("\n")
            why = " | ".join([ln.strip() for ln in err
                              if "Error" in ln or "error" in ln][:3]) \
                or " | ".join(ln.strip() for ln in err[:3])
            ok(False, f"node harness crashed (exit {proc.returncode}) — the "
                      f"boundary threw instead of decoding: {why[:400]}")
            print("FAILURES:", fails)
            return 1
        out = json.loads(proc.stdout)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # ---- 1. v3 -> full frames must equal the engine's own frames ----
    ok(norm_nodes(out["fromV3"]) == norm_nodes(full),
       "v3 stream decodes in the VIEWER to the engine's original frames")
    ref = replay_codec.decode_frames(v3, ship_fleet)
    ok(norm_nodes(out["fromV3"]) == norm_nodes(ref),
       "viewer decode agrees with replay_codec.decode_frames (the reference)")

    # ---- 2. v1 -> the same canonical stream python encodes ----
    ok(norm_nodes(out["fromV1"]) == norm_nodes(full),
       "v1 full rows canonicalize in the VIEWER back to the same full frames")
    ok(out["v1Canonicalized"] == json.loads(json.dumps(v3)),
       "the viewer's v1->v3 conversion is BYTE-IDENTICAL to "
       "replay_codec.encode_frames (both implementations, one rule)")

    # ---- 3. the live stream lands on the same shape ----
    ok(norm_nodes(out["fromLive"]) == norm_nodes(full),
       "the live stream (full rows, appended per frame) canonicalizes identically")

    # ---- 4. the 8-vs-4 discriminator ----
    ok(out["dynF"] == replay_codec.DYN_FLEET_ROW,
       f"viewer DYN_F ({out['dynF']}) == codec DYN_FLEET_ROW "
       f"({replay_codec.DYN_FLEET_ROW})")
    ref_legacy = replay_codec.decode_frames(legacy_v3, ship_fleet)
    ok(norm_nodes(out["legacy"]) == norm_nodes(ref_legacy),
       "6-col pre-relocation fleet rows read as FULL rows, not dynamic")
    st = out["legacyStatic"]
    ok(st and all(v[0][3] is None and v[0][4] is None for v in st.values()),
       "a 6-col row leaves hx/hy ABSENT (consumers fall back to the header "
       f"harbor), got {list(st.values())[:1]}")

    # ---- 5. territory rows pass through untouched ----
    ref_t = replay_codec.decode_frames(v3_t,
                                       replay_codec.ship_fleet_map(engt.events))
    ok(norm_nodes(out["fromTerr"]) == norm_nodes(full_t),
       "territory r rows survive the viewer's canonicalization intact")
    ok(norm_nodes(out["fromTerr"]) == norm_nodes(ref_t),
       "territory decode agrees with the reference decoder")

    # ---- 6. relocation must survive both paths ----
    ok(norm_nodes(out["fromReloc"]) == norm_nodes(full_r),
       "v3: a relocating flagship's hx/hy decode correctly in the viewer")
    ok(norm_nodes(out["fromRelocV1"]) == norm_nodes(full_r),
       "v1: relocation survives the viewer's canonicalization")
    ok(out["relocCanonicalized"] == json.loads(json.dumps(v3_r)),
       "the viewer re-emits a full row on relocation, byte-identically to "
       "replay_codec.encode_frames")

    print("FAILURES:", fails)
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
