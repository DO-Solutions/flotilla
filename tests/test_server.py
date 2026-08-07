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

st, r = req("/api/bundle", {"series": "test-series", "name": "The Grand Regatta"})
ok(st == 200 and r["ok"], "bundle built")
bpath = os.path.join(TMP, r["file"])
with open(bpath) as fh:
    ok(os.path.getsize(bpath) > 10000 and "The Grand Regatta" in fh.read(),
       "bundle embeds the series payload")
with open(os.path.join(TMP, "index.json")) as fh:
    ok(r["file"] in json.load(fh)["bundles"], "bundle indexed")

# cancel: run 1 occupies the single slot; run 2 queues behind it
st, r1 = req("/api/run", {"mode": "match", "seed": 5, "bots": ["merchant", "corsair"],
                          "scenario": {"max_ticks": 6000, "role_fallback": True},
                          "name": "cancel-run"})
ok(st == 202, "run 1 submitted")
st, r2 = req("/api/run", {"mode": "match", "seed": 6, "bots": ["merchant", "corsair"],
                          "name": "cancel-queued"})
ok(st == 202, "run 2 submitted (queued)")
st, c2 = req("/api/cancel", {"id": r2["job"]["id"]})
ok(st == 200 and c2["state"] == "cancelled", "queued job cancels instantly")
time.sleep(1.0)                                 # let run 1 start its subprocess
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

srv.shutdown()
shutil.rmtree(TMP, ignore_errors=True)
print("FAILURES:", fails)
sys.exit(1 if fails else 0)
