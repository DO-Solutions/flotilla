#!/usr/bin/env python3
"""On-map VFX (docs/SPECTATOR.md §3): the painter rail, fog, and skin tokens.

The rail generalizes the old sink ripples: every effect is a time-indexed
transient keyed by event kind, positioned from data the viewer already keeps,
fog-filtered through evVisible, and styled by SKIN.fx.<effect>. What can rot
silently here:

  * a kind in FX_KIND_CFG with no painter or no SKIN.fx group — the rail
    would throw (or silently skip) on the first beat of that kind
  * fx tokens flattened to strings by applySkin's generic path — the effect
    "works" until a skin touches it, then every number becomes NaN
  * the boolean trap feedToken already hit — enabled:"false" staying truthy
  * fxEdgePoint drifting so the arrow points somewhere the beat is not

Canvas output itself is screenshot territory; everything testable without
pixels is here. node is TEST-ONLY (FLOTILLA_REQUIRE_NODE=1 in CI makes a
missing node a failure, not a skip).
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
NEEDED = ["FX_KIND_CFG", "FX_PAINTERS", "fxTtlTicks", "fxEdgePoint",
          "fxToken", "SKIN_DEFAULT", "EV_RANK"]
parts = {n: extract(src, n) for n in NEEDED}
missing = [n for n, v in parts.items() if not v]
ok(not missing, f"the fx substrate is still extractable from the viewer "
                f"(missing: {missing})")

NODE = shutil.which("node")
if not NODE:
    if os.environ.get("FLOTILLA_REQUIRE_NODE") == "1":
        ok(False, "node is required (FLOTILLA_REQUIRE_NODE=1) but not installed")
    else:
        print("SKIPPED the JS checks — node is not installed")
    print(f"FAILURES: {fails}")
    sys.exit(1 if fails else 0)


def node_eval(js):
    r = subprocess.run([NODE, "-e", js], capture_output=True, text=True,
                       timeout=60)
    if r.returncode != 0:
        raise RuntimeError(r.stderr[:400])
    return json.loads(r.stdout)


# ---- table parity: every kind has a painter, a config group, and a rank ----
par = node_eval(
    "const S={};" + parts["FX_KIND_CFG"] + ";" + parts["FX_PAINTERS"] + ";"
    + parts["SKIN_DEFAULT"] + ";" + parts["EV_RANK"] + ";"
    + """console.log(JSON.stringify({
  kinds: Object.keys(FX_KIND_CFG),
  painters: Object.keys(FX_PAINTERS),
  groups: Object.entries(FX_KIND_CFG).map(([k, g]) =>
    [k, typeof SKIN_DEFAULT.fx[g] === "object" && SKIN_DEFAULT.fx[g] !== null]),
  ranked: Object.keys(FX_KIND_CFG).map(k => [k, EV_RANK[k] ?? null]),
  flash: SKIN_DEFAULT.fx.flash, edge: SKIN_DEFAULT.fx.edgeArrow,
  sink: SKIN_DEFAULT.fx.sink,
}))""")
ok(sorted(par["kinds"]) == sorted(par["painters"]),
   f"every FX kind has a painter and no painter is orphaned "
   f"({par['kinds']} vs {par['painters']})")
bad = [k for k, has in par["groups"] if not has]
ok(not bad, f"every FX kind has a SKIN.fx group to read its style from "
            f"(missing: {bad})")
unranked = [k for k, r in par["ranked"] if r is None]
ok(not unranked,
   f"every FX kind is in EV_RANK — the flash gate reads it (missing: {unranked})")
ok(par["sink"]["enabled"] and par["sink"]["ttlS"] == 3,
   "the default sink effect reproduces the stock ripple (on, 3s)")
ok(par["flash"]["enabled"] and par["edge"]["enabled"],
   "flash + edge indicator default ON — they are the point of §3")

# the rail feeds from S.tl + S.parley, so every non-parley FX kind must be in
# the S.tl filter list or its beats never reach a painter
tl_at = src.index("S.tl = rp.events.filter")
tl_stmt = src[tl_at:src.index(";", tl_at)]
ok(all(k in tl_stmt for k in par["kinds"] if k != "parley"),
   "every non-parley FX kind is captured by the S.tl index the rail reads")
ok('fxToken(base.fx' in src,
   "applySkin routes nested fx objects through fxToken — the generic token "
   "path would flatten them to strings")

# ---- ttl conversion: skin seconds -> sim ticks ----
ttl = node_eval(parts["fxTtlTicks"] + """;console.log(JSON.stringify([
  fxTtlTicks({ttlS: 3}, 10), fxTtlTicks({ttlS: 3}, 20),
  fxTtlTicks({}, 10), fxTtlTicks({ttlS: 0.01}, 10)]))""")
ok(ttl[0] == 30, f"3s at 10hz = 30 ticks — the stock ripple's window ({ttl[0]})")
ok(ttl[1] == 60, "the ttl scales with tick_hz, so story-time is constant")
ok(ttl[2] == 30 and ttl[3] >= 1,
   "a missing ttl defaults sanely and the floor is 1 tick, never 0")

# ---- edge indicator geometry ----
edge = node_eval(parts["fxEdgePoint"] + """;
const W = 1200, H = 675, M = 16;
const r = {
  on: fxEdgePoint(600, 300, W, H, M),
  right: fxEdgePoint(2000, 337.5, W, H, M),
  above: fxEdgePoint(600, -500, W, H, M),
  corner: fxEdgePoint(-900, -600, W, H, M),
};
console.log(JSON.stringify(r))""")
ok(edge["on"] is None, "an on-screen beat gets NO edge arrow")
r = edge["right"]
ok(r and abs(r["x"] - (1200 - 16)) < 1e-6 and abs(r["ang"]) < 0.05,
   f"a beat off the right edge pins to x=w-margin pointing right ({r})")
a = edge["above"]
ok(a and abs(a["y"] - 16) < 1e-6 and abs(a["ang"] + 1.5708) < 0.05,
   f"a beat above the viewport pins to y=margin pointing up ({a})")
c = edge["corner"]
ok(c and 16 - 1e-6 <= c["x"] <= 1200 - 16 + 1e-6
   and 16 - 1e-6 <= c["y"] <= 675 - 16 + 1e-6,
   f"a diagonal off-screen beat stays inside the margin box ({c})")

# ---- fx token validation (the feedToken lessons, applied to nested objects) ----
tok = node_eval(parts["SKIN_DEFAULT"] + ";" + parts["fxToken"] + r""";
function set(group, v) {
  const fx = JSON.parse(JSON.stringify(SKIN_DEFAULT.fx));
  fxToken(fx, group, v);
  return fx[group];
}
console.log(JSON.stringify({
  offBool: set("sink", {enabled: false}).enabled,
  offStr:  set("sink", {enabled: "false"}).enabled,
  partial: set("flagSunk", {color: "#123456"}),
  clampTtl: set("region", {ttlS: 9999}).ttlS,
  clampAlpha: set("region", {alpha: 5}).alpha,
  clampWidth: set("parley", {width: 100}).width,
  clampSize: set("edgeArrow", {size: 1}).size,
  longColor: set("flash", {color: "x".repeat(500)}).color.length,
  junkKey: set("sink", {evil: 1}),
  junkVal: set("sink", "not-an-object"),
  arrVal:  set("sink", [1, 2, 3]),
  numKeepsDefault: set("sink", {ttlS: "soon"}).ttlS,
}))""")
ok(tok["offBool"] is False and tok["offStr"] is False,
   "an effect can actually be switched off (enabled:\"false\" must not stay "
   "truthy — the trap feedToken hit)")
ok(tok["partial"]["color"] == "#123456" and tok["partial"]["ttlS"] == 6
   and tok["partial"]["enabled"] is True,
   f"a skin setting only {{color}} keeps the group's other defaults "
   f"({tok['partial']})")
ok(tok["clampTtl"] == 30 and tok["clampAlpha"] == 1
   and tok["clampWidth"] == 6 and tok["clampSize"] == 4,
   f"numbers clamp into their ranges (ttl={tok['clampTtl']} "
   f"alpha={tok['clampAlpha']} width={tok['clampWidth']} size={tok['clampSize']})")
ok(tok["longColor"] == 48, "a color string is length-capped")
ok("evil" not in tok["junkKey"], "an unknown subkey is ignored, not adopted")
ok(tok["junkVal"]["enabled"] is True and tok["arrVal"]["ttlS"] == 3,
   "a non-object value leaves the group untouched")
ok(tok["numKeepsDefault"] == 3, "a non-numeric number keeps the default")

# ---- applySkin end to end: nested fx objects survive the real merge ----
CLEANERS = ["cleanRadius", "cleanTexture", "cleanTileset", "cleanDecor",
            "cleanCoast", "cleanSprites", "cleanShapes", "feedToken"]
cparts = {n: extract(src, n) for n in CLEANERS + ["applySkin"]}
cmissing = [n for n, v in cparts.items() if not v]
ok(not cmissing, f"applySkin + its cleaners extract (missing: {cmissing})")
if not cmissing:
    stubs = r"""
const document = {documentElement: {style: {setProperty(){},
                                            removeProperty(){}}},
                  body: {style: {}}};
function loadSkinPatterns() {}
function applyFeedPref() {}
function audioSkinChanged() {}
let SKIN = null, TAIL = 14;
const COLORS = [];
"""
    # fxToken must ride along: applySkin's try/catch means a missing helper
    # doesn't throw, it silently applies the DEFAULT skin — which is exactly
    # the failure shape this block exists to catch
    merged = node_eval(
        stubs + parts["SKIN_DEFAULT"] + ";" + parts["fxToken"] + ";"
        + ";".join(cparts[n] for n in CLEANERS) + ";" + cparts["applySkin"]
        + r""";
applySkin({fx: {fog: "#000", sink: {ttlS: 9, color: "#abcdef"},
                flagSunk: {enabled: "false"},
                parley: 42}});                     // junk group value
console.log(JSON.stringify(SKIN.fx))""")
    ok(merged["sink"]["ttlS"] == 9 and merged["sink"]["color"] == "#abcdef",
       f"a real applySkin merges an fx group as an OBJECT "
       f"(sink={merged['sink']})")
    ok(merged["flagSunk"]["enabled"] is False,
       "…including the boolean-string off switch")
    ok(merged["fog"] == "#000" and merged["tail"] == 14,
       "the flat fx tokens (fog, tail) still take the generic path")
    ok(isinstance(merged["parley"], dict) and merged["parley"]["ttlS"] == 2,
       f"a junk group value keeps the default group ({merged['parley']})")

print(f"FAILURES: {fails}")
sys.exit(1 if fails else 0)
