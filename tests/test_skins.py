#!/usr/bin/env python3
"""Site-chrome skinning: the server/viewer mirror, resolution, and sanitation.

The dashboard and tournament pages have no skin registry — the server resolves
ONE ui block for them (/api/site-skin). That means the built-in ui blocks now
exist twice, in two languages: viewer/index.html's SKINS registry and
server.py's SKIN_BUILTIN_UI. A duplication across a language boundary rots
silently, so the first test here reads the REAL registry out of the viewer and
fails the moment the two disagree.

The rest covers what the resolver promises the pages: an unknown skin degrades
to stock rather than half-painting the site, an imported skin is read off disk,
and ui.radius cannot smuggle CSS into the page through a var.

node is a TEST-ONLY requirement (same rule as test_viewer_replay): without it
the JS-side checks print SKIPPED, and FLOTILLA_REQUIRE_NODE=1 (CI) makes a
missing node a hard failure so the gate can never silently skip.
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
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "sim"))
TMP = tempfile.mkdtemp(prefix="flotilla-skins-test-")
os.environ["FLOTILLA_LIBRARY"] = TMP

from jsextract import extract                              # noqa: E402
import server                                              # noqa: E402

VIEWER = os.path.join(ROOT, "viewer", "index.html")
fails = 0


def ok(cond, msg):
    global fails
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        fails += 1


def node_json(js):
    """Evaluate viewer declarations in node and return the JSON they print."""
    d = tempfile.mkdtemp(prefix="flotilla-skinjs-")
    p = os.path.join(d, "s.js")
    with open(p, "w") as fh:
        fh.write(js)
    out = subprocess.run([shutil.which("node"), p], capture_output=True,
                         text=True, timeout=60)
    if out.returncode != 0:
        raise RuntimeError(out.stderr[:400])
    return json.loads(out.stdout)


src = open(VIEWER, encoding="utf-8").read()
SKIN_DEFAULT_JS = extract(src, "SKIN_DEFAULT")
SKINS_JS = extract(src, "SKINS")
CLEAN_RADIUS_JS = extract(src, "cleanRadius")
ok(bool(SKIN_DEFAULT_JS and SKINS_JS and CLEAN_RADIUS_JS),
   "the viewer's SKIN_DEFAULT, SKINS and cleanRadius are all still extractable")

NODE = shutil.which("node")
REQUIRE = os.environ.get("FLOTILLA_REQUIRE_NODE") == "1"
if not NODE and REQUIRE:
    ok(False, "node is required (FLOTILLA_REQUIRE_NODE=1) but not installed")
elif not NODE:
    print("SKIPPED the JS-side checks — node is not installed, so the "
          "server/viewer skin mirror was NOT verified. "
          "Set FLOTILLA_REQUIRE_NODE=1 to make this a failure (CI does).")
else:
    # ---- the mirror: server.SKIN_BUILTIN_UI == the viewer's own registry ----
    payload = node_json(
        SKIN_DEFAULT_JS + "\n" + SKINS_JS + "\n"
        "const out = {};\n"
        "for (const [k, v] of Object.entries(SKINS))\n"
        "  out[k] = Object.assign({}, SKIN_DEFAULT.ui, v.ui || {});\n"
        "console.log(JSON.stringify({resolved: out, dflt: SKIN_DEFAULT.ui}));")
    viewer_ui, viewer_default = payload["resolved"], payload["dflt"]

    ok(viewer_default == server.SKIN_UI_DEFAULT,
       "server.SKIN_UI_DEFAULT matches the viewer's SKIN_DEFAULT.ui")
    ok(set(viewer_ui) == set(server.SKIN_BUILTIN_UI),
       "the two registries list the SAME built-in skins "
       f"(viewer: {sorted(viewer_ui)})")
    for name in sorted(set(viewer_ui) & set(server.SKIN_BUILTIN_UI)):
        ok(server._site_skin_ui(name) == viewer_ui[name],
           f'built-in "{name}" resolves identically on both sides')

    # ---- ui.radius is a value, not an injection point ----
    cases = json.dumps([12, "6px", "0.5rem", "50%", -5, 9999,
                        "10px;background:url(x)", "red", "",
                        "expression(alert(1))", None])
    radii = node_json(
        SKIN_DEFAULT_JS + "\n" + CLEAN_RADIUS_JS + "\n"
        f"console.log(JSON.stringify({cases}.map(cleanRadius)));")
    ok(radii[0] == "12px" and radii[1] == "6px" and radii[2] == "0.5rem"
       and radii[3] == "50%",
       "cleanRadius keeps good values (bare number -> px, units preserved)")
    ok(radii[4] == "0px" and radii[5] == "64px",
       "cleanRadius clamps a number to the 0-64px range")
    stock = server.SKIN_UI_DEFAULT["radius"]
    ok(all(r == stock for r in radii[6:]),
       "cleanRadius refuses junk and CSS payloads, falling back to the default")

# ---- resolution rules the dash pages depend on ----
ok(server._site_skin_name() == "",
   "no site skin configured -> empty name (pages keep their own stylesheet)")
ok(server._site_skin_ui("") == server.SKIN_UI_DEFAULT,
   "the empty name resolves to the stock block")
ok(server._site_skin_ui("no-such-skin") == server.SKIN_UI_DEFAULT,
   "an unknown skin degrades to stock instead of half-painting the site")

sd = server._skins_dir()
with open(os.path.join(sd, "brandy.json"), "w") as fh:
    json.dump({"ui": {"accent": "#abcdef", "radius": 3,
                      "bogus": "ignored"}, "fleet": ["#000000"]}, fh)
res = server._site_skin_ui("brandy")
ok(res["accent"] == "#abcdef", "an imported skin's ui block is read off disk")
ok(res["radius"] == "3", "a numeric token survives the read as a string")
ok("bogus" not in res, "an unknown ui token is dropped, not forwarded")
ok(res["bg"] == server.SKIN_UI_DEFAULT["bg"],
   "tokens the imported skin omits fall back to stock (block is COMPLETE)")

with open(os.path.join(sd, "broken.json"), "w") as fh:
    fh.write("{not json at all")
ok(server._site_skin_ui("broken") == server.SKIN_UI_DEFAULT,
   "a corrupt skin file resolves to stock rather than raising into the page")

with open(os.path.join(sd, "listy.json"), "w") as fh:
    json.dump({"ui": ["not", "an", "object"]}, fh)
ok(server._site_skin_ui("listy") == server.SKIN_UI_DEFAULT,
   "a ui block of the wrong TYPE is refused whole")

print(f"FAILURES: {fails}")
sys.exit(1 if fails else 0)
