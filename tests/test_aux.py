#!/usr/bin/env python3
"""Fleet auxiliaries: provision (mocked DO API), bearer-gated artifact fetch,
callback flow (live/game/done/fail), destroy-on-completion, capacity gate."""
import json
import os
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(HERE, "sim"))
sys.path.insert(0, HERE)
TMP = tempfile.mkdtemp(prefix="flotilla-aux-test-")
os.environ["FLOTILLA_LIBRARY"] = TMP

import server                            # noqa: E402
from http.server import ThreadingHTTPServer  # noqa: E402

fails = 0


def ok(cond, msg):
    global fails
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        fails += 1


# mock the DO API: record calls, hand out droplet ids
DO_CALLS = []


def fake_do(cfg, method, path, body=None):
    DO_CALLS.append((method, path))
    if method == "POST" and path == "/droplets":
        return {"droplet": {"id": 424242}}
    if method == "GET" and path.startswith("/droplets?tag_name"):
        return {"droplets": [{"id": 424242}, {"id": 555}]}
    return {}


server._do = fake_do
with open(os.path.join(TMP, "aux.json"), "w") as fh:
    json.dump({"do_token": "fake", "callback_base": "http://cb.example",
               "callback_auth": "u:p", "max_age_h": 8}, fh)

for d in ("matches", "series", "tournaments", "bundles"):
    os.makedirs(os.path.join(TMP, d), exist_ok=True)
server.build_index(TMP)
srv = ThreadingHTTPServer(("127.0.0.1", 0), server.H)
threading.Thread(target=srv.serve_forever, daemon=True).start()
PORT = srv.server_address[1]


def req(path, body=None, headers=None, raw=False):
    r = urllib.request.Request(
        f"http://127.0.0.1:{PORT}{path}",
        data=json.dumps(body).encode() if body is not None else None,
        headers=headers or {})
    try:
        with urllib.request.urlopen(r, timeout=30) as resp:
            data = resp.read()
            return resp.status, data if raw else json.loads(data or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


st, r = req("/api/health")
ok(r.get("aux") is True, "health reports aux configured")

st, r = req("/api/run", {"mode": "series", "seed": 1, "executor": "auxiliary",
                         "bots": ["merchant", "corsair"],
                         "series": {"games": 2, "memos": False},
                         "name": "aux-series"})
ok(st == 202, "aux job accepted")
jid = r["job"]["id"]
time.sleep(0.8)                          # provisioning thread runs
ok(("POST", "/droplets") in DO_CALLS, "droplet provisioned via DO API")
with server.AUX_LOCK:
    bearer = server.AUX[jid]["bearer"]
    ok(server.AUX[jid]["droplet_id"] == 424242, "droplet id recorded")

st, _ = req(f"/api/aux/{jid}/app.tar.gz", headers={"X-Aux-Token": "wrong"})
ok(st == 401, "bad bearer rejected on artifact fetch")
st, data = req(f"/api/aux/{jid}/app.tar.gz",
               headers={"X-Aux-Token": bearer}, raw=True)
ok(st == 200 and data[:2] == b"\x1f\x8b" and len(data) > 10000,
   f"app tarball serves ({len(data)} bytes gzip)")
st, jj = req(f"/api/aux/{jid}/job.json", headers={"X-Aux-Token": bearer})
ok(jj["job_id"] == jid and jj["callback_auth"] == "u:p"
   and jj["config"]["mode"] == "series", "job.json carries config + callback auth")

# callbacks: live lines land in the live stream file
st, r = req(f"/api/aux/{jid}/live", {"lines": [{"header": True, "meta": {}},
                                               {"t": 99, "frames": []}]},
            headers={"X-Aux-Token": bearer})
ok(st == 200, "live callback accepted")
st, lv = req(f"/api/live/{jid}?ofs=0")
ok(len(lv["lines"]) == 2 and lv["lines"][0].get("header"),
   "aux live lines serve through /api/live")

# game callback files the replay + partial series
rp = {"meta": {"seed": 1}, "frames": [{"t": 0}], "decisions": [],
      "result": {"names": {"0": "A", "1": "B"}, "scores": {"0": 1, "1": 2},
                 "winner": 1, "ticks": 10}}
st, r = req(f"/api/aux/{jid}/game",
            {"file": "g1.json", "replay": rp,
             "row": {"seed": 1, "file": "g1.json", "winner": "B",
                     "scores": {"A": 1, "B": 2}}},
            headers={"X-Aux-Token": bearer})
ok(st == 200, "game callback accepted")
ok(os.path.isfile(os.path.join(TMP, "series", "aux-series", "g1.json")),
   "game filed into the library")
with open(os.path.join(TMP, "series", "aux-series", "series.json")) as fh:
    sj = json.load(fh)
ok(sj.get("partial") and sj["games"][0]["winner"] == "B",
   "partial series.json tracks aux games")

# done callback finalizes + destroys the droplet
st, r = req(f"/api/aux/{jid}/done",
            {"series": {"games": sj["games"], "memos": {"A": {"memo": "gg"}}}},
            headers={"X-Aux-Token": bearer})
ok(st == 200, "done callback accepted")
time.sleep(0.5)
with open(os.path.join(TMP, "series", "aux-series", "series.json")) as fh:
    fin = json.load(fh)
ok("partial" not in fin and fin["memos"], "final series.json replaces partial")
jrow = server._job(jid)
ok(jrow["state"] == "done", "job settles done")
ok(("DELETE", "/droplets/424242") in DO_CALLS, "droplet destroyed after done")
with server.AUX_LOCK:
    ok(jid not in server.AUX, "aux record cleaned up")

# fail path
st, r2 = req("/api/run", {"mode": "match", "seed": 2, "executor": "auxiliary",
                          "bots": ["merchant", "corsair"], "name": "aux-fail"})
jid2 = r2["job"]["id"]
time.sleep(0.8)
with server.AUX_LOCK:
    b2 = server.AUX[jid2]["bearer"]
st, r = req(f"/api/aux/{jid2}/fail", {"error": "boom"},
            headers={"X-Aux-Token": b2})
time.sleep(0.5)
ok(server._job(jid2)["state"] == "failed"
   and server._job(jid2)["error"] == "boom", "fail callback marks the job")

# capacity gate
with open(os.path.join(TMP, "aux.json"), "w") as fh:
    json.dump({"do_token": "fake", "callback_base": "http://cb.example",
               "max_concurrent": 0}, fh)
st, r = req("/api/run", {"mode": "match", "seed": 3, "executor": "auxiliary",
                         "bots": ["merchant", "corsair"], "name": "aux-cap"})
ok(st == 400 and "capacity" in r["error"], "capacity gate refuses over-provision")

srv.shutdown()
import shutil
shutil.rmtree(TMP, ignore_errors=True)
print("FAILURES:", fails)
sys.exit(1 if fails else 0)
