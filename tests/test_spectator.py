#!/usr/bin/env python3
"""Spectator event vocabulary — the shared formatter every surface renders from.

docs/SPECTATOR.md's substrate: one rank table, one icon table, one
describeEvent(). The timeline tooltip uses it today; the event feed, the on-map
effects and the anchors Historic Moments cites are meant to use the SAME
formatter, so a beat cannot read one way in the feed and another in a tooltip.
That only holds if the formatter is tested, hence this file.

The functions are EXTRACTED FROM viewer/index.html BY NAME rather than copied —
a copy would keep passing after the viewer changed, which is exactly the failure
this suite exists to prevent.

Fixtures use the engine's REAL event shapes, taken from live replays:
  sink   {fleet, ship, x, y, preset, by, cause?}   by = FLEET id, not ship
  region {region, name, fleet, prev}               prev = previous holder or null
  signal {fleet, flag}                             flag = return | return_safe
  parley {fleet, to, text}
  flag_sunk {fleet, by}                            by = FLEET id or null

node is a TEST-ONLY requirement (same rule as test_viewer_replay): without it
the checks print SKIPPED, and FLOTILLA_REQUIRE_NODE=1 (CI) makes a missing node
a hard failure so the gate can never silently skip.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
from jsextract import extract                                # noqa: E402

VIEWER = os.path.join(ROOT, "viewer", "index.html")
fails = 0


def ok(cond, msg):
    global fails
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        fails += 1


src = open(VIEWER, encoding="utf-8").read()
NEEDED = ["EV_RANK", "EV_ICON", "evFleet", "evPlace", "describeEvent"]
parts = {n: extract(src, n) for n in NEEDED}
missing = [n for n, v in parts.items() if not v]
ok(not missing, f"the vocabulary is still extractable from the viewer "
                f"(missing: {missing})")

NODE = shutil.which("node")
if not NODE:
    if os.environ.get("FLOTILLA_REQUIRE_NODE") == "1":
        ok(False, "node is required (FLOTILLA_REQUIRE_NODE=1) but not installed")
    else:
        print("SKIPPED the formatter checks — node is not installed, so "
              "describeEvent was NOT verified. Set FLOTILLA_REQUIRE_NODE=1 to "
              "make this a failure (CI does).")
    print(f"FAILURES: {fails}")
    sys.exit(1 if fails else 0)

# a territory match: seats give beats a place name
SEATS = [{"id": 0, "x": 45, "y": 15, "name": "Niue Waters"},
         {"id": 1, "x": 167, "y": 45, "name": "Tahaa Waters"},
         {"id": 2, "x": 24, "y": 62, "name": "Fakaofo Waters"}]
NAMES = {"0": "Qwen3.5", "1": "KimiK3"}

CASES = [
    # (label, event, scenario_has_regions, expected_substrings, expect_none)
    ("an intent is muted (95% of the stream by volume)",
     {"k": "intent", "t": 10, "fleet": 0, "ship": 3, "s": "program L2: home"},
     True, [], True),
    ("an unknown engine event is silent, not broken",
     {"k": "brand_new_thing", "t": 10, "fleet": 0}, True, [], True),
    ("a combat sink names the KILLER's fleet, the victim, the class and the place",
     {"k": "sink", "t": 100, "fleet": 0, "ship": 4, "x": 167, "y": 45,
      "preset": "trawler", "by": 1},
     True, ["KimiK3 sank", "Qwen3.5's trawler", "at Tahaa Waters"], False),
    ("a scuttle is not reported as a kill",
     {"k": "sink", "t": 100, "fleet": 0, "ship": 4, "x": 45, "y": 15,
      "preset": "raider", "by": None, "cause": "scuttle"},
     True, ["Qwen3.5 scuttled a raider"], False),
    ("an unattributed loss says lost, not sank",
     {"k": "sink", "t": 100, "fleet": 1, "ship": 9, "x": 24, "y": 62,
      "preset": "scout", "by": None},
     True, ["KimiK3 lost a scout", "at Fakaofo Waters"], False),
    ("no seats (Bounty/Conquest) -> no place name, and no crash",
     {"k": "sink", "t": 100, "fleet": 0, "ship": 4, "x": 167, "y": 45,
      "preset": "trawler", "by": 1},
     False, ["KimiK3 sank", "Qwen3.5's trawler"], False),
    ("an unheld territory is CLAIMED",
     {"k": "region", "t": 49, "region": 1, "name": "Tahaa Waters",
      "fleet": 1, "prev": None},
     True, ["KimiK3 claimed Tahaa Waters"], False),
    ("a contested territory is TAKEN FROM the previous holder",
     {"k": "region", "t": 900, "region": 1, "name": "Tahaa Waters",
      "fleet": 1, "prev": 0},
     True, ["KimiK3 took Tahaa Waters from Qwen3.5"], False),
    ("the flagship kill names both sides",
     {"k": "flag_sunk", "t": 2000, "fleet": 0, "by": 1},
     True, ["KimiK3 destroyed", "Qwen3.5's flagship"], False),
    ("an unattributed flagship loss still reads",
     {"k": "flag_sunk", "t": 2000, "fleet": 0, "by": None},
     True, ["Qwen3.5's flagship went down"], False),
    ("the safe-route signal is distinguished from the urgent one",
     {"k": "signal", "t": 3900, "fleet": 1, "flag": "return_safe"},
     True, ["KimiK3 signalled return to port", "safe route"], False),
    ("parley names sender and recipient",
     {"k": "parley", "t": 0, "fleet": 1, "to": 0, "text": "…"},
     True, ["KimiK3", "Qwen3.5"], False),
]

harness = "\n".join(parts[n] for n in NEEDED) + r"""
function run(ev, hasRegions) {
  S = {rp: {result: {names: NAMES},
            meta: {regions: hasRegions ? SEATS : []}}};
  return describeEvent(ev);
}
"""
js = ("const SEATS = " + json.dumps(SEATS) + ";\n"
      + "const NAMES = " + json.dumps(NAMES) + ";\n"
      + "let S = {};\n" + harness
      + "const CASES = " + json.dumps([[c[1], c[2]] for c in CASES]) + ";\n"
      + "console.log(JSON.stringify(CASES.map(c => run(c[0], c[1]))));")

d = tempfile.mkdtemp(prefix="flotilla-spec-")
p = os.path.join(d, "s.js")
open(p, "w").write(js)
out = subprocess.run([NODE, p], capture_output=True, text=True, timeout=60)
if out.returncode != 0:
    ok(False, f"the vocabulary runs in node: {out.stderr[:300]}")
    print(f"FAILURES: {fails}")
    sys.exit(1)
got = json.loads(out.stdout)

for (label, ev, _has, wants, expect_none), g in zip(CASES, got):
    if expect_none:
        ok(g is None, f"{label} (got {g!r})")
        continue
    if g is None:
        ok(False, f"{label} — formatter returned nothing")
        continue
    txt = g.get("text", "")
    miss = [w for w in wants if w not in txt]
    ok(not miss, f"{label} -> {txt!r}" + (f" MISSING {miss}" if miss else ""))

def node_eval(js):
    r = subprocess.run([NODE, "-e", js], capture_output=True, text=True,
                       timeout=60)
    if r.returncode != 0:
        raise RuntimeError(r.stderr[:300])
    return json.loads(r.stdout)


# rank ordering is the thing every surface will filter on
ranks = node_eval(parts["EV_RANK"] + ";console.log(JSON.stringify(EV_RANK))")
ok(ranks.get("flag_sunk", 0) > ranks.get("sink", 0) > ranks.get("spawn", 9),
   f"rank orders the beats: flag_sunk({ranks.get('flag_sunk')}) > "
   f"sink({ranks.get('sink')}) > spawn({ranks.get('spawn')})")
ok(ranks.get("intent") == 0 and ranks.get("orders") == 0,
   "the high-volume kinds (intent, orders) are muted at rank 0")

# evPlace must follow the ENGINE's rule, not euclidean-nearest
place = node_eval(
    "const SEATS=" + json.dumps(SEATS) + ";let S={rp:{meta:{regions:SEATS}}};"
    + parts["evPlace"]
    + ";console.log(JSON.stringify([evPlace(45,15), evPlace(46,16), "
      "evPlace(1000,1000)]))")
ok(place[0] == "Niue Waters" and place[1] == "Niue Waters",
   "evPlace resolves a point to its nearest seat")
ok(place[2] == "Tahaa Waters",
   "a point off the chart still resolves to the nearest seat, never null")

# ---- the tooltip's hit-test: tick -> logical px -> "which mark is this?" ----
# The formatter above is only half of it; this is the coordinate conversion that
# decides WHICH beat you are hovering, and it is the half a screenshot proves
# but CI otherwise never sees.
HIT = extract(src, "tlEventNear")
ok(bool(HIT), "tlEventNear is still extractable")
if HIT:
    stub = r"""
let S = null, tlc = null;
function setup(spoiler, fi) {
  tlc = {width: 1200,                       // backing store
         getBoundingClientRect: () => ({left: 100, width: 600})};  // CSS px
  S = {tlq: 1, spoiler: spoiler, fi: fi,
       rp: {frames: [{t: 0}, {t: 500}, {t: 1000}]},
       tl: [{k: "sink", t: 0, fleet: 0},
            {k: "region", t: 500, fleet: 1},
            {k: "flag_sunk", t: 1000, fleet: 0}],
       parley: [{k: "parley", t: 250, fleet: 1}]};
}
"""
    # tlc logical width 1200; the element is 600 CSS px wide starting at x=100.
    # So a tick at t maps to clientX = 100 + (t/1000)*600.
    probe = (stub + HIT + r"""
const out = [];
setup(false, 2);
out.push((tlEventNear(100) || {}).t);          // t=0    -> left edge
out.push((tlEventNear(400) || {}).t);          // t=500  -> midpoint
out.push((tlEventNear(700) || {}).t);          // t=1000 -> right edge
out.push((tlEventNear(250) || {}).t);          // t=250  -> a parley dot
out.push(tlEventNear(550));                    // between marks -> nothing
setup(true, 0);                                // spoilers ON, playhead at t=0
out.push((tlEventNear(100) || {}).t);          // the past is fine
out.push(tlEventNear(400));                    // the FUTURE must stay hidden
console.log(JSON.stringify(out));
""")
    h = node_eval(probe)
    ok(h[0] == 0 and h[1] == 500 and h[2] == 1000,
       f"a mark is found under the cursor across the whole width (got {h[:3]})")
    ok(h[3] == 250, f"parley dots are hoverable too (got {h[3]!r})")
    ok(h[4] is None, "hovering between marks finds nothing rather than the nearest")
    ok(h[5] == 0, "with spoilers on, a mark in the PAST is still hoverable")
    ok(h[6] is None,
       "with spoilers on, a FUTURE mark is not hoverable — the tooltip cannot "
       "leak what the timeline itself is hiding")

# ---- the feed's fog rule ----
# The feed may only report what the POV admiral could actually know. Get this
# wrong and the POV view stops meaning anything — it would be narrating the
# other fleet's private business over the top of a fog-of-war display.
VIS = extract(src, "evVisible")
ok(bool(VIS), "evVisible is still extractable")
if VIS:
    fog = node_eval(r"""
let S = {};
function povAlly(f) { return S.povCache && (S.povCache.allies || [S.pov]).includes(f); }
""" + VIS + r"""
// fleet 0 is us. Ship 7 (fleet 1) was visible at frame 5, ship 9 never was.
function setPov(on) {
  S = on ? {pov: 0, fi: 5, povCache: {allies: [0],
             vis: [new Set(), new Set(), new Set(), new Set(), new Set(),
                   new Set([7])]}}
         : {pov: -1, povCache: null};
}
const out = {};
setPov(false);
out.allSeeing = evVisible({k: "sink", fleet: 1, ship: 9, by: 1}, 5);
setPov(true);
out.ownLoss      = evVisible({k: "sink", fleet: 0, ship: 3, by: 1}, 5);
out.ourKill      = evVisible({k: "sink", fleet: 1, ship: 9, by: 0}, 5);
out.seenEnemy    = evVisible({k: "sink", fleet: 1, ship: 7, by: 1}, 5);
out.unseenEnemy  = evVisible({k: "sink", fleet: 1, ship: 9, by: 1}, 5);
out.priorFrame   = evVisible({k: "sink", fleet: 1, ship: 7, by: 1}, 6);
out.parleyOurs   = evVisible({k: "parley", fleet: 1, to: 0}, 5);
out.parleyTheirs = evVisible({k: "parley", fleet: 1, to: 2}, 5);
out.signalTheirs = evVisible({k: "signal", fleet: 1}, 5);
out.signalOurs   = evVisible({k: "signal", fleet: 0}, 5);
out.regionAny    = evVisible({k: "region", fleet: 1}, 5);
out.yardTheirs   = evVisible({k: "yard_built", fleet: 1}, 5);
console.log(JSON.stringify(out));
""")
    ok(fog["allSeeing"] is True,
       "all-seeing view reports everything (no POV, no filtering)")
    ok(fog["ownLoss"] is True and fog["ourKill"] is True,
       "our own losses and our own kills always report")
    ok(fog["seenEnemy"] is True,
       "an enemy ship we could SEE reports when it sinks")
    ok(fog["unseenEnemy"] is False,
       "an enemy ship we never saw does NOT report — the feed cannot leak a "
       "sinking that happened outside our vision")
    ok(fog["priorFrame"] is True,
       "the previous frame counts: a ship is already gone from the frame it "
       "sinks in, so a strict same-frame test would drop every enemy sink")
    ok(fog["parleyOurs"] is True and fog["parleyTheirs"] is False,
       "parley reports only to the two fleets on the wire")
    ok(fog["signalOurs"] is True and fog["signalTheirs"] is False,
       "a rival's signal hoist is their own business")
    ok(fog["regionAny"] is True,
       "territory flips report to everyone — ownership is public in "
       "state.regions, so this leaks nothing")
    ok(fog["yardTheirs"] is False,
       "a rival's yard work is inferred by scouting, not announced")

# ---- feed token validation ----
TOK = extract(src, "feedToken")
ok(bool(TOK), "feedToken is still extractable")
if TOK:
    t = node_eval(TOK + r"""
function fresh() { return {enabled: true, position: "tl", maxLines: 5, ttlS: 8,
                           scale: 1, minRank: 2, bg: "", ink: "", accent: ""}; }
function set(k, v) { const f = fresh(); feedToken(f, k, v); return f[k]; }
console.log(JSON.stringify({
  offBool:  set("enabled", false),
  offStr:   set("enabled", "false"),
  onDefault: set("enabled", true),
  goodPos:  set("position", "br"),
  badPos:   set("position", "../evil"),
  clampMax: set("maxLines", 999),
  clampTtl: set("ttlS", 0),
  clampRank: set("minRank", 0),
  badNum:   set("scale", "enormous"),
}));
""")
    ok(t["offBool"] is False and t["offStr"] is False,
       "the feed can actually be switched off (a boolean must not become the "
       "truthy string \"false\")")
    ok(t["onDefault"] is True, "enabled:true stays on")
    ok(t["goodPos"] == "br" and t["badPos"] == "tl",
       "position takes a corner and refuses anything else")
    ok(t["clampMax"] == 12 and t["clampTtl"] == 1,
       f"numbers clamp into their range (got maxLines={t['clampMax']}, "
       f"ttlS={t['clampTtl']})")
    ok(t["clampRank"] == 1,
       "minRank cannot drop to 0 — that would admit the muted kinds and bury "
       "everything worth reading")
    ok(t["badNum"] == 1, "a non-numeric value keeps the default")

print(f"FAILURES: {fails}")
sys.exit(1 if fails else 0)
