#!/usr/bin/env python3
"""Build a review gallery of every DISTINCT procedural custom-ship silhouette.

Extracts drawShipShape verbatim from viewer/index.html (the real renderer — a
re-implementation could drift), enumerates all legal 12-point stat combos, and
dedupes by the shape-affecting projection: hull never changes the silhouette,
guns cap at 6 studs, armor only matters at the >=3 doubled-outline threshold,
lookout draws no mast below 2. Each tile is labeled with an id + the exemplar
stats so wonky ones can be called out by number.

Usage: shape_gallery.py [out.html] [--points N]
"""
import itertools
import re
import sys

HERE = __import__("os").path.dirname(__import__("os").path.dirname(
    __import__("os").path.abspath(__file__)))

out = sys.argv[1] if len(sys.argv) > 1 else "shape-gallery.html"
points = 12
if "--points" in sys.argv:
    points = int(sys.argv[sys.argv.index("--points") + 1])

src = open(f"{HERE}/viewer/index.html").read()
m = re.search(r"(function drawShipShape\(.*?\n\})\n", src, re.S)
if not m:
    sys.exit("could not extract drawShipShape from the viewer")
draw_fn = m.group(1)

KEYS = ("speed", "hold", "guns", "armor", "hull", "lookout")
seen = {}
for combo in itertools.product(range(1, points - 4), repeat=5):
    hull = points - sum(combo)
    if hull < 1:
        continue
    stats = dict(zip(("speed", "hold", "guns", "armor", "lookout"), combo))
    stats["hull"] = hull
    # the projection that actually reaches the canvas
    key = (stats["speed"], stats["hold"], min(6, stats["guns"]),
           stats["armor"] >= 3, stats["lookout"] if stats["lookout"] >= 2 else 1)
    if key not in seen:
        seen[key] = stats

shapes = [seen[k] for k in sorted(seen)]
builtins = {
    "trader":  dict(speed=3, hold=5, guns=1, armor=1, hull=1, lookout=1),
    "raider":  dict(speed=4, hold=1, guns=3, armor=2, hull=1, lookout=1),
    "frigate": dict(speed=2, hold=1, guns=4, armor=3, hull=1, lookout=1),
    "scout":   dict(speed=5, hold=1, guns=1, armor=1, hull=1, lookout=3),
}

import json  # noqa: E402
html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Flotilla — custom ship shape gallery</title>
<style>
 body {{ background:#0b1220; color:#e6edf7; font:13px system-ui; padding:16px; }}
 h1 {{ font-size:18px; }} h2 {{ font-size:14px; color:#4da3ff; margin:14px 0 6px; }}
 .grid {{ display:flex; flex-wrap:wrap; gap:8px; }}
 .tile {{ background:#111a2e; border:1px solid #1e2a44; border-radius:8px;
          padding:4px; text-align:center; }}
 .tile canvas {{ display:block; }}
 .lbl {{ font-size:10px; color:#8b9bb8; }} .lbl b {{ color:#e6edf7; }}
</style></head><body>
<h1>Custom ship silhouettes — every distinct shape at {points} points</h1>
<p>Deterministic projection: length=speed · beam=hold · gun mounts at 2+ guns
(symmetric pairs spread along the hull; odd count = bow chaser; saturates at 5
pairs) · bright doubled outline=armor≥3 · mast=lookout≥2 (taller with lookout).
Hull is invisible. No per-stat cap exists — just the point budget. <b>{len(shapes)} distinct shapes</b> from all legal stat lines.
Call out wonky ones by <b>#id</b>.</p>
<h2>Built-ins (reference)</h2><div class="grid" id="ref"></div>
<h2>Custom shapes</h2><div class="grid" id="g"></div>
<script>
"use strict";
let ctx = null;
{draw_fn}
const SHAPES = {json.dumps(shapes)};
const BUILTINS = {json.dumps(builtins)};
function tile(parent, name, preset, stats, color) {{
  const d = document.createElement("div");
  d.className = "tile";
  const c = document.createElement("canvas");
  c.width = 96; c.height = 64;
  d.appendChild(c);
  const l = document.createElement("div");
  l.className = "lbl";
  l.innerHTML = "<b>" + name + "</b><br>" +
    (stats ? "s" + stats.speed + " h" + stats.hold + " g" + stats.guns +
             " a" + stats.armor + " L" + stats.lookout : "");
  d.appendChild(l);
  parent.appendChild(d);
  ctx = c.getContext("2d");
  ctx.save();
  ctx.translate(44, 36);
  ctx.scale(2.4, 2.4);
  drawShipShape(preset, color, 0, false, stats);
  ctx.restore();
}}
for (const [nm, st] of Object.entries(BUILTINS)) {{
  tile(document.getElementById("ref"), nm, nm, st, "#4da3ff");
}}
SHAPES.forEach((st, i) => {{
  tile(document.getElementById("g"), "#" + (i + 1), "custom", st,
       ["#4da3ff", "#ff6b6b", "#ffc94d", "#4dd6b8"][i % 4]);
}});
</script></body></html>
"""
open(out, "w").write(html)
print(f"{out}: {len(shapes)} custom shapes + {len(builtins)} built-ins")
