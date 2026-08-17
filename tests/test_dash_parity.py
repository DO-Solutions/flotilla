#!/usr/bin/env python3
"""Configure-form parity: the UI must expose the schema faithfully, and what it
submits must be a config the runner accepts.

The form is generated from config-schema.json, so parity SHOULD be structural —
which is exactly the kind of "should" worth pinning down, because a knob the
form cannot express is a knob nobody can set from the dashboard, and a config
the form emits but the runner rejects is a dead Launch button.

Note the form already routes engine sections (world/economy/combat/pacing/
scenario) into the `scenario` bucket, so the UI could never have produced the
misplaced-section bug that cost two days — that trap belongs to hand-written
configs, which is why the runner validates them now (test_config_sections.py).

jsdom, not a browser: this asserts INTERACTIONS and class state, never computed
stylesheet visibility, which jsdom does not do. node is a TEST-ONLY requirement;
FLOTILLA_REQUIRE_NODE=1 (CI) turns a missing node into a failure.
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
sys.path.insert(0, ROOT)

from keelspring.runner import validate_config                # noqa: E402

fails = 0


def ok(cond, msg):
    global fails
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        fails += 1


NODE = shutil.which("node")
if not NODE:
    if os.environ.get("FLOTILLA_REQUIRE_NODE") == "1":
        ok(False, "node is required (FLOTILLA_REQUIRE_NODE=1) but not installed")
    else:
        print("SKIPPED — node is not installed, so the Configure form was NOT "
              "verified. Set FLOTILLA_REQUIRE_NODE=1 to make this a failure "
              "(CI does).")
    print(f"FAILURES: {fails}")
    sys.exit(1 if fails else 0)

work = tempfile.mkdtemp(prefix="flotilla-parity-")
shutil.copy(os.path.join(ROOT, "dash", "dashboard.html"), work)
shutil.copy(os.path.join(ROOT, "config-schema.json"), work)

DRIVER = r"""
const fs = require("fs");
const { JSDOM } = require("jsdom");
const schema = JSON.parse(fs.readFileSync("config-schema.json", "utf8"));
const html = fs.readFileSync("dashboard.html", "utf8");

// every fetch the page makes on load, answered with something harmless
function stubFetch(url) {
  const u = String(url);
  const j = (o) => Promise.resolve({ ok: true, status: 200,
                                     json: () => Promise.resolve(o),
                                     text: () => Promise.resolve("") });
  if (u.includes("config-schema.json")) return j(schema);
  if (u.includes("api/runs")) return j({ jobs: [] });
  if (u.includes("api/skins")) return j({ skins: [], reserved: [] });
  if (u.includes("api/site-skin")) return j({ name: "", ui: {} });
  if (u.includes("api/models")) return j({ models: [], calibration: {} });
  if (u.includes("index.json")) return j({ matches: [], series: [],
                                           tournaments: [], bundles: [] });
  return j({});
}

const dom = new JSDOM(html, {
  runScripts: "dangerously", pretendToBeVisual: true, url: "http://localhost/",
  beforeParse(w) {
    w.fetch = stubFetch;
    w.matchMedia = () => ({ matches: false, addEventListener() {} });
    w.scrollTo = () => {};
    w.alert = () => {}; w.confirm = () => true;
  },
});
const w = dom.window, d = w.document;

function knobEls() { return [...d.querySelectorAll("#cform [data-k]")]; }

function waitForForm(tries) {
  return new Promise((res, rej) => {
    const tick = (n) => {
      if (knobEls().length > 5) return res();
      if (n <= 0) return rej(new Error("form never built"));
      w.setTimeout(() => tick(n - 1), 50);
    };
    tick(tries);
  });
}

(async () => {
  const out = { errors: [] };
  try {
    // tournament mode exposes the most sections
    const mode = d.getElementById("cmode");
    mode.value = "tournament";
    mode.dispatchEvent(new w.Event("change", { bubbles: true }));
    await waitForForm(200);
    if (typeof w.renderConfigForm === "function") w.renderConfigForm();
    if (typeof w.applyShowIf === "function") w.applyShowIf();

    const present = new Set(knobEls().map(e => e.dataset.sec + "." + e.dataset.k));
    out.present = [...present];

    // which sections the form shows in tournament mode
    const SECTION_MODE = { series: ["series", "tournament"],
                           tournament: ["tournament"] };
    out.expected = [];
    for (const [sec, knobs] of Object.entries(schema)) {
      const allowed = SECTION_MODE[sec];
      if (allowed && !allowed.includes("tournament")) continue;
      for (const k of Object.keys(knobs)) out.expected.push(sec + "." + k);
    }

    // control types
    out.typeProblems = [];
    for (const el of knobEls()) {
      const s = schema[el.dataset.sec][el.dataset.k];
      const tag = el.tagName.toLowerCase();
      if (s.t === "bool" && el.type !== "checkbox")
        out.typeProblems.push(`${el.dataset.k}: bool rendered as ${el.type}`);
      if (s.t === "enum") {
        if (tag !== "select") {
          out.typeProblems.push(`${el.dataset.k}: enum rendered as ${tag}`);
        } else {
          const opts = [...el.options].map(o => String(o.value)).sort();
          const want = s.opts.map(String).sort();
          if (JSON.stringify(opts) !== JSON.stringify(want))
            out.typeProblems.push(
              `${el.dataset.k}: options ${JSON.stringify(opts)} != schema ` +
              JSON.stringify(want));
        }
      }
      if ((s.t === "int" || s.t === "float") && el.type === "number") {
        if (s.lo !== undefined && String(el.min) !== String(s.lo))
          out.typeProblems.push(`${el.dataset.k}: min ${el.min} != lo ${s.lo}`);
        if (s.hi !== undefined && String(el.max) !== String(s.hi))
          out.typeProblems.push(`${el.dataset.k}: max ${el.max} != hi ${s.hi}`);
      }
    }

    // the swiss format must be offered, and drive show_if correctly
    const fmt = d.querySelector('#cform [data-k="format"]');
    out.formatOpts = fmt ? [...fmt.options].map(o => o.value) : [];
    const rowOf = (k) => {
      const el = d.querySelector(`#cform [data-k="${k}"]`);
      return el && el.closest("label");
    };
    // only if this schema HAS swiss — the assertion then self-activates
    // when the format lands, instead of hardcoding a value the schema may not
    // offer yet (the select-vs-schema options check above is the general rule)
    out.hasSwiss = out.formatOpts.includes("swiss");
    if (out.hasSwiss) {
      fmt.value = "swiss";
      fmt.dispatchEvent(new w.Event("change", { bubbles: true }));
      if (typeof w.applyShowIf === "function") w.applyShowIf();
      out.swiss = {
        ppmHidden: !!(rowOf("players_per_match") || {}).classList
          ?.contains("hiddenknob"),
        roundsHidden: !!(rowOf("rounds") || {}).classList
          ?.contains("hiddenknob"),
      };
    }
    fmt.value = "round_robin";
    fmt.dispatchEvent(new w.Event("change", { bubbles: true }));
    if (typeof w.applyShowIf === "function") w.applyShowIf();
    out.rr = {
      ppmHidden: !!(rowOf("players_per_match") || {}).classList
        ?.contains("hiddenknob"),
    };

    // what the form would SUBMIT, with a couple of knobs moved off default
    const setK = (k, v) => {
      const el = d.querySelector(`#cform [data-k="${k}"]`);
      if (!el) return;
      if (el.type === "checkbox") el.checked = v; else el.value = v;
    };
    // a format that this schema actually offers, so the round-trip check
    // works whether or not swiss has landed
    const altFormat = out.formatOpts.find(o => o !== "round_robin")
                      || out.formatOpts[0];
    out.altFormat = altFormat;
    setK("format", altFormat);
    setK("rounds", 3);
    setK("pipeline_depth", 5);        // a `pacing` knob — must land in scenario
    setK("width", 100);
    if (typeof w.applyShowIf === "function") w.applyShowIf();
    if (typeof w.PLAYERS !== "undefined" && Array.isArray(w.PLAYERS)) {
      w.PLAYERS.length = 0;
      w.PLAYERS.push({ model: "merchant" }, { model: "corsair" });
    }
    out.built = typeof w.buildConfig === "function" ? w.buildConfig() : null;
  } catch (e) {
    out.errors.push(String((e && e.stack) || e));
  }
  console.log(JSON.stringify(out));
})();
"""

open(os.path.join(work, "driver.js"), "w").write(DRIVER)
env = dict(os.environ)
# CI installs jsdom at the repo root (`npm install --no-save jsdom`); a dev box
# may have it anywhere on NODE_PATH. Point node at both.
env["NODE_PATH"] = os.pathsep.join(
    [os.path.join(ROOT, "node_modules"), env.get("NODE_PATH", "")]).strip(os.pathsep)
r = subprocess.run(["node", "driver.js"], cwd=work, capture_output=True,
                   text=True, timeout=300, env=env)
if r.returncode != 0:
    hint = ""
    if "Cannot find module 'jsdom'" in (r.stderr or ""):
        hint = ("  jsdom is missing — it is a TEST-ONLY dependency: "
                "`npm install --no-save jsdom` at the repo root (CI does this). "
                "Failing rather than skipping is deliberate.")
    ok(False, f"the driver ran:{hint or ' ' + r.stderr[-400:]}")
    print(f"FAILURES: {fails}")
    sys.exit(1)
res = json.loads(r.stdout.strip().splitlines()[-1])
ok(not res.get("errors"), f"the page built without errors: {res.get('errors')}")

present, expected = set(res["present"]), set(res["expected"])
missing = sorted(expected - present)
ok(not missing,
   f"every schema knob in tournament mode has a control "
   f"({len(present)} present)" + ("" if not missing else f" MISSING: {missing[:12]}"))
ok(len(present) > 60,
   f"the form really is the whole schema, not a subset ({len(present)} knobs)")

ok(not res["typeProblems"],
   "every control matches its knob's type and range"
   + ("" if not res["typeProblems"] else
      "\n    " + "\n    ".join(res["typeProblems"][:10])))

# the general rule (select options == schema opts) is covered by typeProblems
# above; these are the swiss-specific BEHAVIOURS, checked once swiss exists
if res.get("hasSwiss"):
    ok(res["swiss"]["ppmHidden"],
       "choosing swiss HIDES players_per_match — swiss is always 2, and showing "
       "a knob the runner ignores is how a config comes to lie")
    ok(not res["swiss"]["roundsHidden"],
       "choosing swiss SHOWS rounds, which swiss actually uses")
else:
    print("NOTE swiss is not in this schema (it lands with the swiss PR) — its "
          "show_if behaviour will be checked automatically once it is")
ok(not res["rr"]["ppmHidden"],
   "round_robin shows players_per_match again")

built = res.get("built")
ok(isinstance(built, dict) and built.get("mode") == "tournament",
   f"buildConfig() produced a tournament config ({str(built)[:90]})")
if isinstance(built, dict):
    problems = validate_config(built)
    ok(problems == [],
       f"what the UI submits is a config the RUNNER accepts (got {problems})")
    sc = built.get("scenario") or {}
    ok(sc.get("pipeline_depth") == 5,
       f"a `pacing` knob set in the form lands in `scenario`, where the "
       f"envelope reads it — the UI cannot make the misplaced-section mistake "
       f"(scenario={ {k: v for k, v in sc.items()} })")
    ok((built.get("tournament") or {}).get("format") == res["altFormat"],
       f"tournament knobs land in the tournament section "
       f"(format={res['altFormat']})")

print(f"FAILURES: {fails}")
sys.exit(1 if fails else 0)
