#!/usr/bin/env python3
"""server.py endpoint tests — rename, bundle, cancel, import. Stdlib only:
spins the real handler up in-process on an ephemeral port with a temp library."""
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TMP = tempfile.mkdtemp(prefix="flotilla-test-lib-")
os.environ["FLOTILLA_LIBRARY"] = TMP
os.environ["FLOTILLA_CONCURRENT_RUNS"] = "1"
sys.path.insert(0, HERE)

rp = {"meta": {"seed": 1}, "frames": [{"t": 0}], "decisions": [],
      "result": {"names": {"0": "A", "1": "B"}, "scores": {"0": 1, "1": 2},
                 "winner": 1, "ticks": 10}}
sdir = os.path.join(TMP, "series", "test-series")
os.makedirs(sdir)
with open(os.path.join(sdir, "g1.json"), "w") as fh:
    json.dump(rp, fh)
with open(os.path.join(sdir, "series.json"), "w") as fh:
    json.dump({"games": [{"game": 1, "seed": 1, "file": "g1.json", "winner": "B"}],
               "memos": {}}, fh)

import server                                   # noqa: E402
from http.server import ThreadingHTTPServer     # noqa: E402

for d in ("matches", "series", "tournaments", "bundles"):
    os.makedirs(os.path.join(TMP, d), exist_ok=True)
server.build_index(TMP)
srv = ThreadingHTTPServer(("127.0.0.1", 0), server.H)
threading.Thread(target=srv.serve_forever, daemon=True).start()
PORT = srv.server_address[1]


def req(path, body=None):
    r = urllib.request.Request(f"http://127.0.0.1:{PORT}{path}",
                               data=json.dumps(body).encode()
                               if body is not None else None)
    try:
        with urllib.request.urlopen(r, timeout=30) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


fails = 0


def ok(cond, msg):
    global fails
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        fails += 1


st, h = req("/api/health")
ok(st == 200 and h["ok"], "health")

st, r = req("/api/rename", {"series": "test-series", "display_name": "The Grand Regatta"})
ok(st == 200 and r["ok"], "rename accepted")
with open(os.path.join(sdir, "series.json")) as fh:
    ok(json.load(fh).get("display_name") == "The Grand Regatta", "display_name persisted")
with open(os.path.join(TMP, "index.json")) as fh:
    ok(json.load(fh)["series"][0].get("display_name") == "The Grand Regatta",
       "index rebuilt with display_name")
st, r = req("/api/rename", {"series": "nope", "display_name": "x"})
ok(st == 404, "rename unknown series -> 404")

# --- archive / unarchive: a flag in series.json, surfaced by the index ---
st, r = req("/api/archive", {"series": "test-series", "archived": True})
ok(st == 200 and r["archived"], "archive accepted")
with open(os.path.join(TMP, "index.json")) as fh:
    ok(json.load(fh)["series"][0].get("archived") is True,
       "index carries archived=true")
st, r = req("/api/rename", {"series": "test-series",
                            "display_name": "The Grand Regatta"})
with open(os.path.join(sdir, "series.json")) as fh:
    ok(json.load(fh).get("archived") is True,
       "archived survives a series.json rewrite (rename)")
st, r = req("/api/archive", {"series": "test-series", "archived": False})
with open(os.path.join(TMP, "index.json")) as fh:
    ok(st == 200 and json.load(fh)["series"][0].get("archived") is False,
       "unarchive clears the flag in the index")
st, r = req("/api/archive", {"series": "nope", "archived": True})
ok(st == 404, "archive unknown series -> 404")

# --- delete-series: gone from disk + index; live-job guard ---
ddir = os.path.join(TMP, "series", "doomed")
os.makedirs(ddir)
with open(os.path.join(ddir, "g1.json"), "w") as fh:
    json.dump(rp, fh)
with open(os.path.join(ddir, "series.json"), "w") as fh:
    json.dump({"games": [{"game": 1, "seed": 1, "file": "g1.json",
                          "winner": "B"}], "memos": {}}, fh)
server.build_index(TMP)
with server.JOBS_LOCK:
    server.JOBS.append({"id": "guard-job", "name": "doomed", "state": "running",
                        "log": [], "games_done": 0})
st, r = req("/api/delete-series", {"series": "doomed"})
ok(st == 400, "delete refused while a job for the series is live")
with server.JOBS_LOCK:
    server.JOBS[-1]["state"] = "done"
st, r = req("/api/delete-series", {"series": "doomed"})
ok(st == 200 and not os.path.isdir(ddir), "delete removes the series dir")
with open(os.path.join(TMP, "index.json")) as fh:
    ok(all(s["name"] != "doomed" for s in json.load(fh)["series"]),
       "deleted series gone from the index")
st, r = req("/api/delete-series", {"series": "doomed"})
ok(st == 404, "delete unknown series -> 404")

# --- standalone-match meta: rename + archive via the sidecar, delete file ---
mpath = os.path.join(TMP, "matches", "solo.json")
with open(mpath, "w") as fh:
    json.dump(rp, fh)
server.build_index(TMP)
st, r = req("/api/rename", {"match": "solo.json", "display_name": "The Duel"})
ok(st == 200 and r["ok"], "match rename accepted")
st, r = req("/api/archive", {"match": "solo.json", "archived": True})
ok(st == 200 and r["archived"], "match archive accepted")
with open(os.path.join(TMP, "matches", "matches-meta.json")) as fh:
    mm = json.load(fh)
    ok(mm["solo.json"] == {"display_name": "The Duel", "archived": True},
       "sidecar carries both fields")
with open(os.path.join(TMP, "index.json")) as fh:
    row = next(m for m in json.load(fh)["matches"]
               if m["file"] == "replays/solo.json")
    ok(row.get("archived") is True and row.get("display_name") == "The Duel",
       "index row carries match meta")
with open(mpath) as fh:
    ok("display_name" not in json.load(fh), "replay file itself untouched")
st, r = req("/api/archive", {"match": "nope.json", "archived": True})
ok(st == 404, "archive unknown match -> 404")
st, r = req("/api/delete-match", {"match": "solo.json"})
ok(st == 200 and not os.path.exists(mpath), "delete-match removes the file")
with open(os.path.join(TMP, "matches", "matches-meta.json")) as fh:
    ok("solo.json" not in json.load(fh), "delete-match clears its meta")
st, r = req("/api/delete-match", {"match": "matches-meta.json"})
ok(st == 404, "the meta sidecar itself is not deletable")

# --- provider key store: CRUD, masking, ordering, clamps, injection ---
st, r = req("/api/providers", {})
ok(st == 200 and r["providers"][0]["id"] == "digitalocean" and
   "key" not in r["providers"][0], "builtin DO listed, key never returned")
st, r = req("/api/providers-op", {
    "op": "add", "label": "Baseten",
    "base_url": "https://127.0.0.1:9", "key": "sk-test-abcdef123",
    "model_map": {"kimi-k3": "moonshotai/Kimi-K3"}})
ok(st == 200 and r["ok"], "provider add")
st, r = req("/api/providers", {})
bt = next(p for p in r["providers"] if p["id"] == "baseten")
ok(bt["key_hint"].endswith("f123") and "key" not in bt, "key masked to last 4")
kp = os.path.join(TMP, "server-keys.json")
ok(oct(os.stat(kp).st_mode & 0o777) == "0o600", "keystore file is 0600")
ok("sk-test-abcdef123" in open(kp).read(), "real key stored server-side only")
st, r = req("/api/providers-op", {"op": "move", "id": "baseten", "dir": -1})
st, r = req("/api/providers", {})
ok(r["providers"][0]["id"] == "baseten", "move up reorders the ladder")
req("/api/providers-op", {"op": "move", "id": "baseten", "dir": 1})
st, r = req("/api/providers-op", {"op": "toggle", "id": "baseten",
                                  "enabled": False})
st, r = req("/api/providers", {})
ok(next(p for p in r["providers"]
        if p["id"] == "baseten")["enabled"] is False, "disable sticks")
ok("sk-test-abcdef123" in open(kp).read(),
   "the disabled provider's key is kept on disk")
_env = json.loads(server._providers_json())
_bt = next(p for p in _env["providers"] if p["id"] == "baseten")
ok(_bt["enabled"] is False,
   "runner env carries the entry marked disabled")
sys.path.insert(0, os.path.join(HERE, "sim"))
import providers as _prov
os.environ["FLOTILLA_PROVIDERS"] = server._providers_json()
_lad = _prov.Ladder()
ok(all(q.get("id") != "baseten" for q in _lad.providers),
   "the ladder's enabled filter drops it — the operator's toggle is honored")
os.environ.pop("FLOTILLA_PROVIDERS", None)
st, r = req("/api/providers-op", {"op": "remove", "id": "digitalocean"})
ok(st == 400, "the builtin primary cannot be removed")
st, r = req("/api/providers-op", {"op": "fallback",
    "fallback": {"timeout_streak": 99, "canary_minutes": 0}})
st, r = req("/api/providers", {})
ok(r["fallback"]["timeout_streak"] == 20 and
   r["fallback"]["canary_minutes"] == 1, "fallback settings clamp to bounds")
st, r = req("/api/provider-check", {"id": "baseten"})
ok(st == 200 and r["ok"] is False and r.get("error"),
   "check on an unreachable base reports the error")
os.environ["DO_INFERENCE_KEY"] = "dk-injection-test"
pj = json.loads(server._providers_json())
ok(pj["providers"][0]["key"] == "dk-injection-test" and
   any(p.get("key") == "sk-test-abcdef123" for p in pj["providers"]),
   "runner injection carries the env key + stored keys")
os.environ.pop("DO_INFERENCE_KEY", None)
st, r = req("/api/providers-op", {"op": "remove", "id": "baseten"})
ok(st == 200, "non-builtin provider removable")

st, r = req("/api/bundle", {"series": "test-series", "name": "The Grand Regatta"})
ok(st == 200 and r["ok"], "bundle built")
bpath = os.path.join(TMP, r["file"])
with open(bpath) as fh:
    ok(os.path.getsize(bpath) > 10000 and "The Grand Regatta" in fh.read(),
       "bundle embeds the series payload")
with open(os.path.join(TMP, "index.json")) as fh:
    ok(r["file"] in json.load(fh)["bundles"], "bundle indexed")

# cancel: run 1 occupies the single slot; run 2 queues behind it
st, r1 = req("/api/run", {"mode": "match", "seed": 5, "bots": ["merchant", "merchant"],
                          "scenario": {"max_ticks": 60000, "role_fallback": True},
                          "name": "cancel-run"})
ok(st == 202, "run 1 submitted")
st, r2 = req("/api/run", {"mode": "match", "seed": 6, "bots": ["merchant", "corsair"],
                          "name": "cancel-queued"})
ok(st == 202, "run 2 submitted (queued)")
st, c2 = req("/api/cancel", {"id": r2["job"]["id"]})
ok(st == 200 and c2["state"] == "cancelled", "queued job cancels instantly")
for _ in range(240):                # wait for the OBSERVABLE condition (the
    if server.PROCS.get(r1["job"]["id"]):        # subprocess exists), not a nap
        break
    time.sleep(0.25)
st, c1 = req("/api/cancel", {"id": r1["job"]["id"]})
ok(st == 200, "running-job cancel accepted")
state, deadline = None, time.time() + 30
while time.time() < deadline:
    st, runs = req("/api/runs")
    state = next(j["state"] for j in runs["jobs"] if j["id"] == r1["job"]["id"])
    if state in ("cancelled", "done", "failed"):
        break
    time.sleep(0.5)
ok(state == "cancelled", f"running job settles cancelled (got {state})")
ok(not os.path.exists(os.path.join(TMP, "matches", "cancel-run.json")),
   "cancelled run leaves no library entry")
st, c = req("/api/cancel", {"id": r1["job"]["id"]})
ok(st == 400, "cancelling a settled job -> 400")

st, r = req("/api/import?name=imported-one", rp)
ok(st == 200 and r["ok"], "import replay")

st, r = req("/api/models")
ok(st == 200 and r["scripted"] == ["merchant", "corsair", "admiralty", "turtle"]
   and isinstance(r["models"], list), "models endpoint answers (scripted + list)")

st, r = req("/api/prompts", {"name": "trade first", "text": "Favor trade over war."})
ok(st == 200 and r.get("trade-first") == "Favor trade over war.", "prompt saved")
st, r = req("/api/prompts")
ok(st == 200 and "trade-first" in r, "prompt listed")
st, r = req("/api/prompts", {"name": "trade first", "text": ""})
ok(st == 200 and "trade-first" not in r, "empty text deletes the prompt")
st, r = req("/api/prompts", {"name": "", "text": "x"})
ok(st == 400, "unnamed prompt rejected")

# ships: the Configure designer's store — save, list-fresh, delete, validate
st, r = req("/api/ships")
ok(st == 200 and set(r["builtin"]) == {"trawler", "raider", "frigate", "scout"}
   and r["saved"] == {}, "ships endpoint lists the built-ins")
st, r = req("/api/ships", {"name": "gunboat", "stats":
                           {"speed": 2, "hold": 1, "guns": 5, "armor": 2,
                            "hull": 1, "lookout": 1}})
ok(st == 200 and r["saved"].get("gunboat", {}).get("guns") == 5, "ship saved")
st, r = req("/api/ships")
ok(st == 200 and "gunboat" in r["saved"], "saved ship listed on a fresh read")
st, r = req("/api/ships", {"name": "trawler", "stats":
                           {"speed": 1, "hold": 1, "guns": 1, "armor": 1,
                            "hull": 1, "lookout": 1}})
ok(st == 400, "a built-in class name is rejected")
st, r = req("/api/ships", {"name": "bad", "stats": {"speed": 0}})
ok(st == 400, "sub-1 stats are rejected")
st, r = req("/api/ships", {"name": "gunboat", "delete": True})
ok(st == 200 and "gunboat" not in r["saved"], "ship deleted")

# showcase: unconfigured 400 -> config roundtrip -> graceful upload failure
st, r = req("/api/showcase", {"series": "test-series"})
ok(st == 400 and "not configured" in r["error"], "showcase unconfigured -> 400")
st, r = req("/api/showcase-config", {"access_key": "AK", "secret_key": "SK",
                                     "endpoint": "127.0.0.1:9", "bucket": "b"})
ok(st == 200 and r["showcase"], "showcase config stored")
ok(oct(os.stat(os.path.join(TMP, "showcase.json")).st_mode & 0o777) == "0o600",
   "showcase config file is 0600")
st, h2 = req("/api/health")
ok(h2.get("showcase") is True, "health reports showcase enabled")
st, r = req("/api/showcase", {"series": "nope"})
ok(st == 404, "unknown series -> 404")
st, r = req("/api/showcase", {"series": "test-series"})
ok(st == 502 and "upload failed" in r["error"],
   "unreachable bucket fails gracefully (bundle built, upload 502)")
os.remove(os.path.join(TMP, "showcase.json"))

# live per-game publishing: a series game lands in the library before the job ends
wd = os.path.join(TMP, "_work", "livetest")
os.makedirs(wd, exist_ok=True)
with open(os.path.join(wd, "g1.json"), "w") as fh:
    json.dump(rp, fh)
livejob = dict(name="live-series", mode="series", games_done=1,
               log=[json.dumps({"seed": 9, "file": os.path.join(wd, "g1.json"),
                                "scores": {"A": 1, "B": 2}, "winner": "B"})])
server._publish_partial_series(livejob, wd)
ok(os.path.isfile(os.path.join(TMP, "series", "live-series", "g1.json")),
   "partial publish copies the finished game")
with open(os.path.join(TMP, "series", "live-series", "series.json")) as fh:
    sj = json.load(fh)
ok(sj.get("partial") is True and sj["games"][0]["winner"] == "B",
   "partial series.json written with winner")
with open(os.path.join(TMP, "index.json")) as fh:
    ix = json.load(fh)
live = next((x for x in ix["series"] if x["name"] == "live-series"), None)
ok(live is not None and live.get("partial") is True,
   "index lists the in-progress series as partial")
ok(any(m["file"] == "replays/live-series/g1.json" for m in ix["matches"]),
   "the live game is watchable from the index")

# ---- cancelled runs keep their finished games, marked cancelled ----
cxdir = os.path.join(TMP, "series", "cx-series")
os.makedirs(cxdir, exist_ok=True)
with open(os.path.join(cxdir, "g1.json"), "w") as fh:
    json.dump({"meta": {"seed": 5}, "frames": [{"t": 0}], "decisions": [],
               "result": {"names": {"0": "A", "1": "B"}, "scores": {"0": 1, "1": 9},
                          "winner": 1, "ticks": 10}}, fh)
with open(os.path.join(cxdir, "series.json"), "w") as fh:
    json.dump({"games": [{"game": 1, "seed": 5, "file": "g1.json",
                          "winner": "B"}], "memos": {}, "partial": True}, fh)
server._mark_cancelled(dict(id="cx", name="cx-series", mode="series"))
with open(os.path.join(cxdir, "series.json")) as fh:
    cx = json.load(fh)
ok(cx.get("cancelled") is True and "partial" not in cx
   and cx["games"][0]["winner"] == "B" and cx["games_completed"] == 1,
   "cancel marks the series and KEEPS its games")
with open(os.path.join(TMP, "index.json")) as fh:
    ix2 = json.load(fh)
cxrow = next((x for x in ix2["series"] if x["name"] == "cx-series"), None)
ok(cxrow and cxrow.get("cancelled") is True, "index carries the cancelled flag")

# ---- an in-flight series is listed from the moment of submission ----
st, r = req("/api/run", {"mode": "series", "seed": 77,
                         "bots": ["merchant", "merchant"],
                         "scenario": {"max_ticks": 30000, "role_fallback": True,
                                      "warmup": False},
                         "series": {"games": 2, "memos": False},
                         "name": "stub-visible"})
sv = r["job"]["id"]
with open(os.path.join(TMP, "index.json")) as fh:
    ixs = json.load(fh)
row = next((x for x in ixs["series"] if x["name"] == "stub-visible"), None)
ok(row is not None and row["partial"] is True and row["games"] == [],
   "a just-submitted series shows in the index as ⏳ live with 0 games")
req("/api/cancel", {"id": sv})
for _ in range(80):
    if server._job(sv)["state"] == "cancelled":
        break
    time.sleep(0.25)

# ---- pause / resume through the API (real subprocess, scripted bots) ----
st, r = req("/api/run", {"mode": "series", "seed": 44,
                         "bots": ["merchant", "merchant"],
                         "scenario": {"width": 64, "height": 36,
                                      "max_ticks": 40000, "role_fallback": True,
                                      "warmup": False},
                         "series": {"games": 2, "memos": False},
                         "name": "pausable"})
jid = r["job"]["id"]
for _ in range(80):
    if server._job(jid)["state"] == "running":
        break
    time.sleep(0.25)
ok(server._job(jid)["state"] == "running", "pausable job runs")
_lf = os.path.join(TMP, "_work", jid, "live.jsonl")
for _ in range(240):                  # ticks are OBSERVABLE: wait for frames,
    if os.path.isfile(_lf) and os.path.getsize(_lf) > 200:   # not a nap
        break
    time.sleep(0.25)
st, r = req("/api/pause", {"id": jid})
ok(st == 200 and r.get("pausing"), "pause accepted")
for _ in range(120):
    if server._job(jid)["state"] == "paused":
        break
    time.sleep(0.25)
ok(server._job(jid)["state"] == "paused", "job froze at a window boundary")
ok(os.path.isfile(os.path.join(TMP, "_work", jid, "checkpoint.json")),
   "checkpoint survives in _work")
st, r = req("/api/resume", {"id": jid})
ok(st == 200 and r.get("resuming"), "resume accepted")
for _ in range(40):
    if server._job(jid)["state"] == "running":
        break
    time.sleep(0.25)
ok(server._job(jid)["state"] == "running", "job sails again after resume")
st, r = req("/api/cancel", {"id": jid})
ok(st == 200, "cancel of the resumed job accepted")
# generous ceiling: under CPU load (a parallel test sweep, jsdom containers)
# subprocess teardown + finalization can take tens of seconds — the old 20s
# wait was the head of a cascade that failed 6 later cases
for _ in range(240):
    if server._job(jid)["state"] == "cancelled":
        break
    time.sleep(0.25)
ok(server._job(jid)["state"] == "cancelled", "resumed job settles cancelled")
# do NOT start the next block until the runner PROCESS is truly gone — the
# run-queue semaphore is 1, and a lingering process starved the preserve-me
# job of it (its pause then timed out with no checkpoint: the "7 failures")
_p = server.PROCS.get(jid)
for _ in range(240):
    if _p is None or _p.poll() is not None:
        break
    time.sleep(0.25)

# ---- cancel INSIDE the register window: state=="running", PROCS still empty ----
# _run_job flips state to "running" and only registers PROCS after makedirs +
# config write + Popen. A cancel landing in that gap found no process to
# terminate and matched no branch in the handler: the API answered 200 while the
# runner sailed on. This is the flake behind "resumed job settles cancelled" —
# reproduced deterministically here by holding Popen open instead of racing it.
#
# The assertion is on the EFFECT (the child died of a SIGNAL), not on the state
# label: a short series can finish naturally inside any timeout and then get
# stamped "cancelled" by the p.wait() branch, so asserting the label alone passes
# whether or not the cancel did anything. returncode < 0 means terminate landed.
_real_popen = server.subprocess.Popen
_window_open = threading.Event()
_spawned = []


def _slow_popen(*a, **kw):
    _window_open.set()
    time.sleep(2.0)                        # hold the window wide open
    p = _real_popen(*a, **kw)
    _spawned.append(p)                     # our own handle: PROCS is popped later
    return p


server.subprocess.Popen = _slow_popen
try:
    st, r = req("/api/run", {"mode": "series", "seed": 46,
                             "bots": ["merchant", "merchant"],
                             "scenario": {"width": 64, "height": 36,
                                          "max_ticks": 40000,
                                          "role_fallback": True,
                                          "warmup": False},
                             "series": {"games": 2, "memos": False},
                             "name": "cancel-window"})
    jidw = r["job"]["id"]
    ok(_window_open.wait(60), "runner reached Popen (cancel window open)")
    ok(server._job(jidw)["state"] == "running"
       and server.PROCS.get(jidw) is None,
       "cancel window reproduced: running with no process registered")
    st, r = req("/api/cancel", {"id": jidw})
    ok(st == 200, "cancel accepted inside the register window")
finally:
    server.subprocess.Popen = _real_popen
for _ in range(240):
    if server._job(jidw)["state"] == "cancelled":
        break
    time.sleep(0.25)
ok(server._job(jidw)["state"] == "cancelled",
   "job cancelled inside the register window settles cancelled")
_cw = _spawned[0] if _spawned else None
if _cw is not None:
    for _ in range(240):
        if _cw.poll() is not None:
            break
        time.sleep(0.25)
ok(_cw is not None and _cw.returncode is not None and _cw.returncode < 0,
   f"the runner was actually SIGNALLED, not left to finish "
   f"(returncode {None if _cw is None else _cw.returncode})")
if _cw is not None and _cw.poll() is None:     # never starve the run-queue
    _cw.kill()                                 # semaphore for the later blocks
    _cw.wait()

# ---- a FAILED resume must preserve the checkpoint and re-pause the job ----
st, r = req("/api/run", {"mode": "series", "seed": 45,
                         "bots": ["merchant", "merchant"],
                         "scenario": {"width": 64, "height": 36,
                                      "max_ticks": 40000, "role_fallback": True,
                                      "warmup": False},
                         "series": {"games": 2, "memos": False},
                         "name": "preserve-me"})
jid2 = r["job"]["id"]
for _ in range(240):                       # ceiling covers a loaded box —
    if server._job(jid2)["state"] == "running":    # the semaphore may free late
        break
    time.sleep(0.25)
ok(server._job(jid2)["state"] == "running", "preserve-me job runs")
_lf2 = os.path.join(TMP, "_work", jid2, "live.jsonl")
for _ in range(240):
    if os.path.isfile(_lf2) and os.path.getsize(_lf2) > 200:
        break
    time.sleep(0.25)
req("/api/pause", {"id": jid2})
for _ in range(240):                       # generous: a loaded box can take a
    if server._job(jid2)["state"] == "paused":   # while to reach the boundary
        break
    time.sleep(0.5)
ck2 = os.path.join(TMP, "_work", jid2, "checkpoint.json")
ok(server._job(jid2)["state"] == "paused" and os.path.isfile(ck2),
   "preserve-me checkpointed")
if not os.path.isfile(ck2):                # cannot continue this block —
    srv.shutdown()                         # report the real count + the bail
    shutil.rmtree(TMP, ignore_errors=True)
    print("SKIPPED: the rest of the preserve-me block (no checkpoint)")
    print("FAILURES:", fails)
    sys.exit(1)
good = open(ck2).read()
with open(ck2, "w") as fh:
    fh.write('{"corrupt": ')            # resume will crash the runner
st, r = req("/api/resume", {"id": jid2})
ok(st == 200, "resume of the doomed checkpoint accepted")
for _ in range(80):
    if server._job(jid2)["state"] not in ("running",):
        break
    time.sleep(0.25)
ok(server._job(jid2)["state"] == "paused",
   "failed resume returns the job to PAUSED, not failed")
ok(os.path.isfile(ck2), "failed resume did NOT delete the checkpoint")
with open(ck2, "w") as fh:
    fh.write(good)                      # repair -> the same job resumes fine
st, r = req("/api/resume", {"id": jid2})
ok(st == 200 and r.get("resuming"), "repaired checkpoint resumes")
for _ in range(40):
    if server._job(jid2)["state"] == "running":
        break
    time.sleep(0.25)
ok(server._job(jid2)["state"] == "running", "job recovered after the repair")
req("/api/cancel", {"id": jid2})
for _ in range(80):
    if server._job(jid2)["state"] == "cancelled":
        break
    time.sleep(0.25)

# ---- _dispatch_resume claims atomically (the double-runner fix) ----
cw = os.path.join(TMP, "_work", "claimjob")
os.makedirs(cw, exist_ok=True)
with open(os.path.join(cw, "checkpoint.json"), "w") as fh:
    fh.write('{"not": "a real checkpoint"}')   # the CLAIM is what's under test
cj = dict(id="claimjob", name="claimjob", mode="series", state="paused",
          games_done=0, log=[])
with server.JOBS_LOCK:
    server.JOBS.append(cj)
code1, _p1 = server._dispatch_resume(cj)
code2, p2 = server._dispatch_resume(cj)
ok(code1 == 200 and code2 == 400 and "job is" in p2.get("error", ""),
   f"a second dispatcher is rejected while the first holds the claim "
   f"({code2} {p2})")
for _ in range(120):        # the garbage checkpoint fails the runner — fine
    if server._job("claimjob")["state"] not in ("queued", "running"):
        break
    time.sleep(0.25)
bj = dict(id="bailjob", name="bailjob", mode="series", state="paused",
          games_done=0, log=[])
with server.JOBS_LOCK:
    server.JOBS.append(bj)
code3, p3 = server._dispatch_resume(bj)      # no checkpoint on disk
ok(code3 == 400 and bj["state"] == "paused",
   f"a failed claim bails back to paused ({code3} state={bj['state']})")

# ---- _norm_variants / _auto_map: the id bridge to fallback providers ----
ok("kimik3" in server._norm_variants("moonshotai/Kimi-K3")
   and "kimik3" in server._norm_variants("kimi-k3"),
   "norm variants meet across vendor prefixes")
amap = server._auto_map(["moonshotai/Kimi-K3", "unrelated-model-9b"])
ok(amap.get("kimi-k3") == "moonshotai/Kimi-K3",
   f"kimi-k3 auto-maps to its vendor-prefixed twin ({amap})")
ok("unrelated-model-9b" not in amap.values(),
   "an upstream model matching no admiral id maps to nothing")

# ---- provider-check success path: models stored, manual map entries win ----
st, r = req("/api/providers-op", {"op": "add", "label": "checkme",
                                  "base_url": "https://checkme.example",
                                  "key": "sk-check",
                                  "model_map": {"kimi-k3": "MANUAL"}})
ok(st == 200 and r.get("id") == "checkme",
   "add returns the server-derived id")
_old_lm = server._list_models
server._list_models = lambda base, key: ["moonshotai/Kimi-K3",
                                         "other/Thing-2B"]
try:
    st, r = req("/api/provider-check", {"id": "checkme"})
    ok(st == 200 and r["ok"] and len(r["models"]) == 2,
       f"provider-check stores the discovered models ({r.get('models')})")
    stj = json.load(open(kp))
    pc = next(p for p in stj["providers"] if p["id"] == "checkme")
    ok(pc["model_map"].get("kimi-k3") == "MANUAL",
       "a manual model_map entry survives the auto-map merge")
    ok(pc.get("checked") and pc["models"] == ["moonshotai/Kimi-K3",
                                              "other/Thing-2B"],
       "models + checked stamp persisted")
finally:
    server._list_models = _old_lm

# ---- _publish_partial_series must carry rename/archive/started through ----
mdir = os.path.join(TMP, "series", "metakeep")
os.makedirs(mdir, exist_ok=True)
with open(os.path.join(mdir, "series.json"), "w") as fh:
    json.dump({"display_name": "Kept Name", "archived": True,
               "started": 12345.0, "games": [], "memos": {},
               "partial": True}, fh)
mwd = os.path.join(TMP, "_work", "metajob")
os.makedirs(mwd, exist_ok=True)
with open(os.path.join(mwd, "g1.json"), "w") as fh:
    fh.write("{}")
mjob = {"id": "metajob", "name": "metakeep", "mode": "series",
        "log": ['{"winner": "A", "file": "g1.json", "seed": 1}'],
        "games_done": 1, "state": "running"}
server._publish_partial_series(mjob, mwd)
ms = json.load(open(os.path.join(mdir, "series.json")))
ok(ms.get("display_name") == "Kept Name" and ms.get("archived") is True
   and ms.get("started") == 12345.0 and len(ms.get("games", [])) == 1,
   f"partial publish keeps display_name/archived/started ({ms})")

# ---- corrupt keystore is PARKED, never silently reset (run LAST: it
# replaces the store; the original bytes are restored after) ----
_orig_ks = open(kp).read()
with open(kp, "w") as fh:
    fh.write("{corrupt json!!")
_st2 = server._keystore()
ok(os.path.isfile(kp + ".corrupt")
   and open(kp + ".corrupt").read() == "{corrupt json!!",
   "corrupt keystore bytes are parked for recovery")
ok(any(p.get("builtin") for p in _st2["providers"]),
   "a fresh store still carries the builtin primary")
with open(kp, "w") as fh:
    fh.write(_orig_ks)
os.remove(kp + ".corrupt")

# ---- cost estimate + Server-tab ceiling (runaway-run guard) ----
ok(server._estimate_cost({"mode": "series",
    "bots": ["llm:kimi-k3:K", "llm:glm-5.2:G"], "series": {"games": 5},
    "scenario": {}}) > 0, "series cost estimate is positive")
ok(server._estimate_cost({"mode": "match",
    "bots": ["merchant", "corsair"], "scenario": {}}) == 0,
   "scripted-only run estimates free")
_c1 = server._estimate_cost({"mode": "series", "bots": ["llm:kimi-k3:K"],
                             "series": {"games": 1}, "scenario": {}})
_c5 = server._estimate_cost({"mode": "series", "bots": ["llm:kimi-k3:K"],
                             "series": {"games": 5}, "scenario": {}})
ok(4.9 * _c1 <= _c5 <= 5.1 * _c1 + 0.05,
   f"series cost scales ~5× with game count ({_c1} → {_c5})")
server.KEYSTORE = os.path.join(tempfile.mkdtemp(), "server-keys.json")
code, _ = server._providers_op({"op": "limits",
                                "limits": {"max_series_cost": 0.01}})
ok(code == 200, "limits op accepted")
st, r = req("/api/providers", {})
ok(r.get("limits", {}).get("max_series_cost") == 0.01,
   "ceiling round-trips through /api/providers")
st, r = req("/api/run", {"mode": "series", "seed": 9,
                         "bots": ["llm:anthropic-claude-opus-5:O",
                                  "llm:kimi-k3:K"],
                         "series": {"games": 5}, "name": "pricey"})
ok(st == 400 and "ceiling" in str(r.get("error", "")),
   f"over-ceiling run refused ({str(r.get('error',''))[:60]})")
st, r = req("/api/run", {"mode": "series", "seed": 9, "ack_cost": True,
                         "bots": ["llm:anthropic-claude-opus-5:O",
                                  "llm:kimi-k3:K"],
                         "series": {"games": 5}, "name": "pricey-ack"})
ok(st == 202, "ack_cost overrides the ceiling")
if r.get("job"):
    req("/api/cancel", {"id": r["job"]["id"]})
server._providers_op({"op": "limits", "limits": {"max_series_cost": 0}})

srv.shutdown()
shutil.rmtree(TMP, ignore_errors=True)
print("FAILURES:", fails)
sys.exit(1 if fails else 0)
