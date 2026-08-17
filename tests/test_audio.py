#!/usr/bin/env python3
"""Audio stingers (docs/SPECTATOR.md §4): tokens, policy, window, coalescing.

No sound ships in this repo — a skin carries clips like it carries sprites —
so what needs proving is the MECHANISM: the asset rule (data:audio only,
size-capped), the token merge (the enabled:"false" trap again), the on/off
policy precedence (viewer pref > skin default, no clips = silent always),
the fire window (forward playback only, fog-checked at the event's frame),
and the coalescing floor (eight sinks in one tick = one stinger).

Playback itself (AudioContext, decoding, the arming gesture) is browser
territory — audioBeats returns the fired groups precisely so everything
around the speaker is testable without one. node is TEST-ONLY
(FLOTILLA_REQUIRE_NODE=1 in CI makes a missing node a failure, not a skip).
"""
import json
import os
import shutil
import subprocess
import sys

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
NEEDED = ["cleanAudioClip", "audioToken", "audioPref", "audioClips",
          "audioActive", "audioVol", "audioBeats", "AUDIO_MIN_GAP_MS",
          "FX_KIND_CFG", "AUDIO_KIND_CFG", "SKIN_DEFAULT"]
parts = {n: extract(src, n) for n in NEEDED}
missing = [n for n, v in parts.items() if not v]
ok(not missing, f"the audio substrate is still extractable from the viewer "
                f"(missing: {missing})")
ok("audioToken(base.audio" in src,
   "applySkin routes the audio section through audioToken — the generic "
   "path would stringify enabled and skip the asset rule")
ok("S.audioT = fr.t" in src and "audioBeats(S.audioT" in src,
   "the loop fires the crossed window and seek() resets it (source anchors)")

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


# a tiny but genuine WAV data URI (44-byte header, 4 samples of silence)
import base64                                                # noqa: E402
wav = (b"RIFF" + (36 + 8).to_bytes(4, "little") + b"WAVEfmt "
       + (16).to_bytes(4, "little") + (1).to_bytes(2, "little")
       + (1).to_bytes(2, "little") + (8000).to_bytes(4, "little")
       + (16000).to_bytes(4, "little") + (2).to_bytes(2, "little")
       + (16).to_bytes(2, "little") + b"data" + (8).to_bytes(4, "little")
       + b"\x00" * 8)
CLIP = "data:audio/wav;base64," + base64.b64encode(wav).decode()

# ---- the asset rule ----
c = node_eval(parts["cleanAudioClip"] + f"""
const CLIP = {json.dumps(CLIP)};
console.log(JSON.stringify({{
  wav: cleanAudioClip(CLIP) === CLIP,
  mpeg: cleanAudioClip("data:audio/mpeg;base64,AAAA") !== "",
  image: cleanAudioClip("data:image/png;base64,AAAA"),
  url: cleanAudioClip("https://cdn.example/boom.mp3"),
  huge: cleanAudioClip("data:audio/wav;base64," + "A".repeat(300000)),
  junk: cleanAudioClip({{}}),
}}))""")
ok(c["wav"] and c["mpeg"], "real audio data URIs pass (wav, mpeg)")
ok(c["image"] == "" and c["url"] == "",
   "an image URI and a remote URL are both refused — self-contained only")
ok(c["huge"] == "" and c["junk"] == "",
   "an oversize clip and a non-string are refused")

# ---- token merge ----
t = node_eval(parts["SKIN_DEFAULT"] + ";" + parts["cleanAudioClip"] + ";"
              + parts["audioToken"] + f"""
const CLIP = {json.dumps(CLIP)};
function set(k, v) {{
  const a = JSON.parse(JSON.stringify(SKIN_DEFAULT.audio));
  audioToken(a, k, v);
  return a;
}}
console.log(JSON.stringify({{
  offStr: set("enabled", "false").enabled,
  on: set("enabled", true).enabled,
  vol0: set("volume", 0).volume,
  volClamp: set("volume", 9).volume,
  volJunk: set("volume", "loud").volume,
  clip: set("sink", CLIP).sink === CLIP,
  clipBad: set("sink", "https://x/y.mp3").sink,
  defOff: SKIN_DEFAULT.audio.enabled,
  defEmpty: Object.keys(SKIN_DEFAULT.audio)
    .filter(k => typeof SKIN_DEFAULT.audio[k] === "string"
                 && SKIN_DEFAULT.audio[k] !== "").length,
}}))""")
ok(t["offStr"] is False and t["on"] is True,
   "enabled survives as a real boolean (the \"false\" trap)")
ok(t["vol0"] == 0 and t["volClamp"] == 1 and t["volJunk"] == 0.7,
   f"volume clamps 0-1 and a legitimate 0 is not swallowed "
   f"(0->{t['vol0']}, 9->{t['volClamp']}, junk->{t['volJunk']})")
ok(t["clip"] and t["clipBad"] == "",
   "clip slots take a valid data URI and blank anything else")
ok(t["defOff"] is False and t["defEmpty"] == 0,
   "the DEFAULT is off and empty — this repo ships no sound")

# ---- policy precedence: viewer pref > skin default; no clips = silent ----
POLICY_STUBS = """
let SKIN = null, store = {};
const localStorage = {getItem: k => (k in store ? store[k] : null)};
"""
p = node_eval(POLICY_STUBS + parts["FX_KIND_CFG"] + ";"
              + parts["AUDIO_KIND_CFG"] + ";" + parts["audioPref"]
              + ";" + parts["audioClips"] + ";" + parts["audioActive"] + f"""
const CLIP = {json.dumps(CLIP)};
function run(pref, skinOn, withClip) {{
  store = pref === null ? {{}} : {{"flotilla-audio": pref}};
  SKIN = {{audio: {{enabled: skinOn, volume: 0.7,
                    sink: withClip ? CLIP : "", flagSunk: "", region: "",
                    parley: "", signal: ""}}}};
  return audioActive();
}}
console.log(JSON.stringify({{
  skinOnNoPref: run(null, true, true),
  skinOffNoPref: run(null, false, true),
  prefBeatsSkinOff: run("on", false, true),
  prefOffBeatsSkinOn: run("off", true, true),
  noClips: run("on", true, false),
  junkPrefFollowsSkin: run("maybe", false, true),
}}))""")
ok(p["skinOnNoPref"] is True and p["skinOffNoPref"] is False,
   "with no viewer pref, the skin's default stands")
ok(p["prefBeatsSkinOff"] is True and p["prefOffBeatsSkinOn"] is False,
   "the viewer's saved choice outranks the skin, both directions")
ok(p["noClips"] is False,
   "no clips = silent no matter what any switch says")
ok(p["junkPrefFollowsSkin"] is False,
   "a junk localStorage value counts as no preference")

# ---- the fire window: forward-only, fog-checked, coalesced ----
w = node_eval(POLICY_STUBS + parts["FX_KIND_CFG"] + ";"
              + parts["AUDIO_KIND_CFG"] + ";" + parts["audioPref"]
              + ";" + parts["audioClips"] + ";" + parts["audioActive"] + ";"
              + parts["AUDIO_MIN_GAP_MS"] + ";" + f"""
const CLIP = {json.dumps(CLIP)};
const AUDIO = {{ctx: null, gain: null, buffers: {{}}, last: {{}}}};
let played = [];
function audioPlay(g) {{ played.push(g); }}
let VIS = () => true;
function evVisible(e, fi) {{ return VIS(e, fi); }}
const S = {{rp: {{meta: {{frame_every: 2}}}}, frameMaps: new Array(50),
            tl: [{{k: "sink", t: 10, fleet: 0}},
                 {{k: "sink", t: 10, fleet: 1}},
                 {{k: "sink", t: 11, fleet: 0}},
                 {{k: "flag_sunk", t: 20, fleet: 1}},
                 {{k: "region", t: 30, fleet: 0, region: 1}}],
            parley: [{{k: "parley", t: 12, fleet: 0, to: 1}}]}};
store = {{"flotilla-audio": "on"}};
SKIN = {{audio: {{enabled: false, volume: 0.7, sink: CLIP, flagSunk: CLIP,
                  region: "", parley: "", signal: ""}}}};
""" + parts["audioBeats"] + """
const r = {};
r.burst = audioBeats(-1, 15, 1000);          // 3 sinks + 1 parley in window
r.gap   = audioBeats(15, 25, 1100);          // flag_sunk, inside sink's gap
r.later = audioBeats(25, 35, 5000);          // region: no clip -> silent
r.behind = audioBeats(15, 15, 9000);         // empty window fires nothing
AUDIO.last = {};
VIS = () => false;                            // fog hides everything
r.fogged = audioBeats(-1, 100, 20000);
VIS = (e) => e.k !== "sink";                  // fog hides only the sinks
r.partial = audioBeats(-1, 100, 40000);
console.log(JSON.stringify(r))""")
ok(w["burst"] == ["sink"],
   f"three sinks in one window coalesce to ONE stinger, and parley without "
   f"a clip is silent ({w['burst']})")
ok(w["gap"] == ["flagSunk"],
   f"a different kind fires inside another kind's cooldown ({w['gap']})")
ok(w["later"] == [] and w["behind"] == [],
   "a clip-less kind and an empty window both fire nothing")
ok(w["fogged"] == [],
   "FOG: a POV listener hears nothing it could not see")
ok(w["partial"] == ["flagSunk"],
   f"fog filters per event, not globally ({w['partial']})")

print(f"FAILURES: {fails}")
sys.exit(1 if fails else 0)
