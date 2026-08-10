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
server._AUX_STAGGER_S = 0               # this suite launches many aux jobs
                                        # back-to-back; the 8-min parallel-launch
                                        # stagger is real but not under test here
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
    json.dump({"do_token": "fake", "callback_base": "https://cb.example",
               "callback_auth": "u:p", "max_age_h": 8}, fh)

for d in ("matches", "series", "tournaments", "bundles"):
    os.makedirs(os.path.join(TMP, d), exist_ok=True)
server.build_index(TMP)
srv = ThreadingHTTPServer(("127.0.0.1", 0), server.H)
threading.Thread(target=srv.serve_forever, daemon=True).start()
PORT = srv.server_address[1]


def req(path, body=None, headers=None, raw=False, rawbody=None):
    r = urllib.request.Request(
        f"http://127.0.0.1:{PORT}{path}",
        data=rawbody if rawbody is not None
        else (json.dumps(body).encode() if body is not None else None),
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
ok(jj["job_id"] == jid and jj["callback_auth"] == ""
   and jj["config"]["mode"] == "series",
   "job.json carries config; callback_auth is NOT shipped (F2: worker uses the "
   "bearer + the /api/aux/* Caddy lane, never the dashboard password)")

# callbacks: live lines land in the live stream file
st, r = req(f"/api/aux/{jid}/live", {"lines": [{"header": True, "meta": {}},
                                               {"t": 99, "frames": []}]},
            headers={"X-Aux-Token": bearer})
ok(st == 200, "live callback accepted")
st, lv = req(f"/api/live/{jid}?ofs=0")
ok(len(lv["lines"]) == 2 and lv["lines"][0].get("header"),
   "aux live lines serve through /api/live")

# one batch carrying TWO game boundaries (a short game ending fast) must
# truncate at the LAST header — breaking at the first re-created the exact
# multi-game concatenation the truncation exists to kill. Non-dict lines
# must not crash the handler.
st, r = req(f"/api/aux/{jid}/live",
            {"lines": [{"header": True, "game": 1, "meta": {}}, {"t": 1},
                       {"header": True, "game": 2, "meta": {}}, {"t": 2},
                       "not-a-dict"]},
            headers={"X-Aux-Token": bearer})
ok(st == 200, "two-header batch accepted (non-dict line tolerated)")
st, lv = req(f"/api/live/{jid}?ofs=0")
ok(len(lv["lines"]) == 3 and lv["lines"][0].get("game") == 2,
   f"stream truncated at the LAST header ({[l for l in lv['lines']]})")

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

# ---- tournament on an auxiliary: matchup-dir filing + bracket + public show ----
PUT_KEYS = []


def fake_put(cfg, key, data, content_type="text/html"):
    PUT_KEYS.append(key)


server._s3_put_public = fake_put
with open(os.path.join(TMP, "showcase.json"), "w") as fh:
    json.dump({"access_key": "k", "secret_key": "s", "region": "nyc3",
               "endpoint": "nyc3.digitaloceanspaces.com", "bucket": "b"}, fh)
os.chmod(os.path.join(TMP, "showcase.json"), 0o600)

st, r3 = req("/api/run", {"mode": "tournament", "seed": 4,
                          "executor": "auxiliary", "public": True,
                          "participants": ["merchant", "corsair"],
                          "tournament": {"format": "round_robin",
                                         "games_per_match": 2},
                          "name": "aux-tourn"})
ok(st == 202, "aux tournament accepted")
jid3 = r3["job"]["id"]
ok(r3["job"].get("public") is True, "public flag recorded on the job")
ok(r3["job"]["games_expected"] == 2, "tournament games_expected estimated")
time.sleep(0.8)
with server.AUX_LOCK:
    b3 = server.AUX[jid3]["bearer"]
ok(any(k.endswith("player.html") for k in PUT_KEYS),
   "public show seeded with the player page")
rp3 = {"meta": {"seed": 4}, "frames": [{"t": 0}], "decisions": [],
       "result": {"names": {"0": "merchant", "1": "corsair"},
                  "scores": {"0": 3, "1": 9}, "winner": 1, "ticks": 10}}
st, _ = req(f"/api/aux/{jid3}/game",
            {"file": "m01_merchant_v_corsair/g1.json", "replay": rp3,
             "row": {"seed": 4, "file": "m01_merchant_v_corsair/g1.json",
                     "winner": "corsair", "scores": {"merchant": 3, "corsair": 9}}},
            headers={"X-Aux-Token": b3})
ok(st == 200, "tournament game callback accepted")
ok(os.path.isfile(os.path.join(TMP, "tournaments", "aux-tourn",
                               "m01_merchant_v_corsair", "g1.json")),
   "tournament game filed under its matchup dir")
tj_part = {"config": {"tournament": {"format": "round_robin"}},
           "matchups": [{"round": 1, "players": ["merchant", "corsair"],
                         "dir": "m01_merchant_v_corsair",
                         "games": [{"game": 1, "seed": 4,
                                    "file": "m01_merchant_v_corsair/g1.json",
                                    "scores": {"merchant": 3, "corsair": 9},
                                    "winner": "corsair"}],
                         "winner": "corsair"}],
           "standings": {"merchant": {"wins": 0, "games": 1, "score": 3},
                         "corsair": {"wins": 1, "games": 1, "score": 9}},
           "partial": True}
st, _ = req(f"/api/aux/{jid3}/game",
            {"file": "tournament.json", "replay": tj_part, "row": None},
            headers={"X-Aux-Token": b3})
ok(st == 200, "incremental bracket accepted")
with open(os.path.join(TMP, "tournaments", "aux-tourn", "tournament.json")) as fh:
    ok(json.load(fh).get("partial") is True, "partial bracket filed")
idx3 = json.load(open(os.path.join(TMP, "index.json")))
trow = next((t for t in idx3["tournaments"] if t["name"] == "aux-tourn"), None)
ok(trow and trow["partial"] is True, "index marks the tournament in progress")
# traversal never escapes the tournament dir
st, _ = req(f"/api/aux/{jid3}/game",
            {"file": "../../evil/x.json", "replay": rp3, "row": None},
            headers={"X-Aux-Token": b3})
ok(not os.path.exists(os.path.join(TMP, "evil")), "path traversal neutralized")
# live chunks mirror to the public prefix
st, _ = req(f"/api/aux/{jid3}/live", {"lines": [{"header": True, "meta": {}}]},
            headers={"X-Aux-Token": b3})
ok(any(k.endswith("live/00001.jsonl") for k in PUT_KEYS)
   and any(k.endswith("live/state.json") for k in PUT_KEYS),
   "public live chunk + cursor published")
st, _ = req(f"/api/aux/{jid3}/done",
            {"series": {}, "tournament": {**tj_part, "champion": "corsair"}},
            headers={"X-Aux-Token": b3})
ok(st == 200, "tournament done accepted")
time.sleep(0.5)
with open(os.path.join(TMP, "tournaments", "aux-tourn", "tournament.json")) as fh:
    fin3 = json.load(fh)
ok("partial" not in fin3 and fin3["champion"] == "corsair",
   "final bracket replaces partial")
ok(any(k.endswith("index.html") for k in PUT_KEYS), "public hub page published")
ok(server._job(jid3)["state"] == "done", "tournament job settles done")

# local-executor tournaments: the partial publisher mirrors matchup dirs too
wd = os.path.join(TMP, "_work", "localtourn")
os.makedirs(os.path.join(wd, "m01_a_v_b"), exist_ok=True)
with open(os.path.join(wd, "m01_a_v_b", "g1.json"), "w") as fh:
    json.dump(rp3, fh)
with open(os.path.join(wd, "tournament.json"), "w") as fh:
    json.dump(tj_part, fh)
fakejob = dict(id="localtourn", name="local-tourn", mode="tournament",
               state="running", games_done=1, log=[], public=False)
server._publish_partial_tournament(fakejob, wd)
ok(os.path.isfile(os.path.join(TMP, "tournaments", "local-tourn",
                               "m01_a_v_b", "g1.json"))
   and os.path.isfile(os.path.join(TMP, "tournaments", "local-tourn",
                                   "tournament.json")),
   "local partial tournament publish mirrors matchup dirs + bracket")

# ---- warm pool (aux v2): provision, claim, poll route, per-job size ----
with open(os.path.join(TMP, "aux.json"), "w") as fh:
    json.dump({"do_token": "fake", "callback_base": "https://cb.example",
               "region": "nyc3", "size": "s-1vcpu-2gb", "max_age_h": 8,
               "warm_pool": 1, "warm_size": "s-1vcpu-2gb"}, fh)
DO_CALLS.clear()
server._pool_tick()
ok(("POST", "/droplets") in DO_CALLS, "pool tick provisions a warm worker")
with server.POOL_LOCK:
    ok(len(server.POOL) == 1, "pool holds one idle worker")
    ppid, prec = next(iter(server.POOL.items()))
    pbearer = prec["bearer"]
server._pool_tick()
ok(DO_CALLS.count(("POST", "/droplets")) == 1, "pool tick is idempotent at target")
st, _ = req(f"/api/aux/pool/{ppid}/job", headers={"X-Aux-Token": "wrong"})
ok(st == 401, "pool route rejects a bad bearer")
st, _ = req(f"/api/aux/pool/{ppid}/job", headers={"X-Aux-Token": pbearer})
ok(st == 204, "unassigned pooler gets 204 (keep waiting)")
DO_CALLS.clear()
st, r4 = req("/api/run", {"mode": "match", "seed": 9, "executor": "auxiliary",
                          "bots": ["merchant", "corsair"], "name": "aux-warm"})
jid4 = r4["job"]["id"]
time.sleep(0.8)
ok(("POST", "/droplets") not in DO_CALLS,
   "warm job claims the pooler instead of provisioning")
with server.AUX_LOCK:
    ok(server.AUX[jid4]["droplet_id"] == 424242, "pooler droplet handed to the job")
    jb4 = server.AUX[jid4]["bearer"]
st, asg = req(f"/api/aux/pool/{ppid}/job", headers={"X-Aux-Token": pbearer})
ok(st == 200 and asg["job_id"] == jid4 and asg["bearer"] == jb4,
   "assigned pooler receives the job.json payload")
st, _ = req(f"/api/aux/{jid4}/done", {"series": {}},
            headers={"X-Aux-Token": jb4})
time.sleep(0.5)
with server.POOL_LOCK:
    ok(ppid not in server.POOL, "used pooler retired with its job")
# per-job size: a mismatched size skips the pool and cold-provisions
server._pool_tick()                     # replace the pool for the size test
DO_CALLS.clear()
st, r5 = req("/api/run", {"mode": "match", "seed": 10, "executor": "auxiliary",
                          "aux_size": "s-2vcpu-4gb",
                          "bots": ["merchant", "corsair"], "name": "aux-big"})
time.sleep(0.8)
ok(("POST", "/droplets") in DO_CALLS,
   "size mismatch cold-provisions instead of claiming the pool")
ok(server._job(r5["job"]["id"])["aux_size"] == "s-2vcpu-4gb",
   "per-job size recorded")
req(f"/api/aux/{r5['job']['id']}/fail", {"error": "cleanup"},
    headers={"X-Aux-Token": server.AUX[r5["job"]["id"]]["bearer"]})
with server.POOL_LOCK:                  # drain the pool for the capacity test
    server.POOL.clear()

# ---- aux pause/resume: command channel + checkpoint round trip ----
import gzip as _gzip
with open(os.path.join(TMP, "aux.json"), "w") as fh:
    json.dump({"do_token": "fake", "callback_base": "https://cb.example",
               "region": "nyc3", "size": "s-1vcpu-2gb", "max_age_h": 8}, fh)
st, rp5 = req("/api/run", {"mode": "series", "seed": 15, "executor": "auxiliary",
                           "bots": ["merchant", "corsair"],
                           "series": {"games": 2}, "name": "aux-pausable"})
jid5 = rp5["job"]["id"]
time.sleep(0.8)
with server.AUX_LOCK:
    b5 = server.AUX[jid5]["bearer"]
st, r = req(f"/api/aux/{jid5}/live", {"lines": [{"t": 1}]},
            headers={"X-Aux-Token": b5})
ok(st == 200 and r.get("command") is None, "no command when none queued")
st, r = req("/api/pause", {"id": jid5})
ok(st == 200 and r.get("pausing"), "aux pause accepted (command queued)")
st, r = req(f"/api/aux/{jid5}/live", {"lines": []},
            headers={"X-Aux-Token": b5})
ok(r.get("command") == "pause", "pause command rides the callback response")
st, r = req(f"/api/aux/{jid5}/live", {"lines": []},
            headers={"X-Aux-Token": b5})
ok(r.get("command") is None, "command delivery is one-shot")
# the worker ships its checkpoint home -> paused, droplet reaped, record kept
DO_CALLS.clear()
blob = _gzip.compress(b"opaque-frozen-engine-json")
st, r = req(f"/api/aux/{jid5}/paused", None,
            headers={"X-Aux-Token": b5, "Content-Type": "application/octet-stream"},
            rawbody=blob)
ok(st == 200 and r.get("paused"), "checkpoint intake accepted")
ok(server._job(jid5)["state"] == "paused", "aux job settles paused")
time.sleep(0.4)
ok(("DELETE", "/droplets/424242") in DO_CALLS,
   "paused aux droplet destroyed — a paused job costs nothing")
with server.AUX_LOCK:
    ok(jid5 in server.AUX and server.AUX[jid5]["droplet_id"] is None,
       "aux record survives with no droplet")
st, data = req(f"/api/aux/{jid5}/checkpoint.json.gz",
               headers={"X-Aux-Token": b5}, raw=True)
ok(st == 200 and _gzip.decompress(data) == b"opaque-frozen-engine-json",
   "checkpoint served back byte-identical")
st, _ = req(f"/api/aux/{jid5}/checkpoint.json.gz", headers={"X-Aux-Token": "no"})
ok(st == 401, "checkpoint fetch is bearer-gated")
# resume: a FRESH droplet spins up for the same job + bearer
DO_CALLS.clear()
st, r = req("/api/resume", {"id": jid5})
ok(st == 200 and r.get("resuming"), "aux resume accepted")
time.sleep(0.8)
ok(("POST", "/droplets") in DO_CALLS, "resume provisions a fresh worker")
ok(server._job(jid5)["state"] == "running", "resumed aux job runs")
with server.AUX_LOCK:
    ok(server.AUX[jid5]["droplet_id"] == 424242, "new droplet recorded")
st, _ = req(f"/api/aux/{jid5}/done", {"series": {}},
            headers={"X-Aux-Token": b5})
ok(st == 200 and server._job(jid5)["state"] == "done",
   "resumed aux job completes with the SAME bearer")

# watcher must keep a paused job's record (field bug: reaped -> resume 400)
import gzip as _gz2
pjob = dict(id="pwatch", name="pwatch-series", mode="series", state="paused",
            games_done=1, games_expected=3, submitted=time.time(),
            started=time.time(), finished=None, error=None, log=[], aux=True)
with server.JOBS_LOCK:
    server.JOBS.append(pjob)
with server.AUX_LOCK:
    server.AUX["pwatch"] = {"bearer": "kaux_pw", "droplet_id": None,
                            "born": time.time(), "config": {}, "rows": []}
with open(os.path.join(TMP, "aux.json")) as fh:
    auxcfg = json.load(fh)
server._aux_watch(pjob, auxcfg)          # paused -> returns WITHOUT reaping
with server.AUX_LOCK:
    ok("pwatch" in server.AUX, "watcher preserves the paused record")
# record loss + resume: a fresh bearer is minted from the checkpoint
os.makedirs(os.path.join(TMP, "_work", "pwatch"), exist_ok=True)
with open(os.path.join(TMP, "_work", "pwatch", "checkpoint.json.gz"), "wb") as fh:
    fh.write(_gz2.compress(b"frozen-engine"))
with server.AUX_LOCK:
    server.AUX.pop("pwatch")
server._persist_aux()
DO_CALLS.clear()
st, r = req("/api/resume", {"id": "pwatch"})
ok(st == 200 and r.get("resuming"), "resume revives a record-less paused job")
time.sleep(0.8)
with server.AUX_LOCK:
    ok("pwatch" in server.AUX and server.AUX["pwatch"]["droplet_id"] == 424242,
       "fresh record minted + worker provisioned")
    b5b = server.AUX["pwatch"]["bearer"]
st, _ = req("/api/aux/pwatch/done", {"series": {}},
            headers={"X-Aux-Token": b5b})
ok(st == 200 and server._job("pwatch")["state"] == "done",
   "revived job completes on the minted bearer")

# ---- tournaments are EXEMPT from pause-rotation (they cannot checkpoint;
# the rotation pause request killed champions-cup-1 at the 8h cap) — they run
# under the tournament_max_age_h runaway ceiling instead ----
tj1 = dict(id="twatch", name="twatch-cup", mode="tournament", state="done",
           games_done=6, games_expected=50, submitted=time.time(),
           started=time.time() - 10 * 3600,      # 10h old: PAST max_age_h=8
           finished=time.time(), error=None, log=[], aux=True)
with server.JOBS_LOCK:
    server.JOBS.append(tj1)
with open(os.path.join(TMP, "aux.json")) as fh:
    auxcfg_t = json.load(fh)
server._aux_watch(tj1, auxcfg_t)         # done -> returns; must NOT fail it
ok(tj1["state"] == "done" and tj1["error"] is None,
   "a finished tournament past the rotation cap is left alone")
ok(any("rotation cap does not apply" in l for l in tj1["log"]),
   "tournament watcher logs the exemption")
tj2 = dict(id="twatch2", name="twatch2-cup", mode="tournament",
           state="running", games_done=3, games_expected=50,
           submitted=time.time(), started=time.time() - 80 * 3600,  # 80h: past
           finished=None, error=None, log=[], aux=True)             # the 72h ceiling
with server.JOBS_LOCK:
    server.JOBS.append(tj2)
server._aux_watch(tj2, dict(auxcfg_t, tournament_max_age_h=72))
ok(tj2["state"] == "failed" and "runaway ceiling" in (tj2["error"] or ""),
   "a tournament past the runaway ceiling is failed loudly")

# ---- restart survival: bearers + jobs live through a flagship bounce ----
with open(os.path.join(TMP, "aux.json"), "w") as fh:
    json.dump({"do_token": "fake", "callback_base": "https://cb.example",
               "region": "nyc3", "size": "s-1vcpu-2gb", "max_age_h": 8}, fh)
st, r6 = req("/api/run", {"mode": "series", "seed": 12, "executor": "auxiliary",
                          "bots": ["merchant", "corsair"],
                          "series": {"games": 2}, "name": "aux-revive"})
jid6 = r6["job"]["id"]
time.sleep(0.8)
with server.AUX_LOCK:
    b6 = server.AUX[jid6]["bearer"]
# also park a RUNNING LOCAL job — a restart must fail it loudly
with server.JOBS_LOCK:
    server.JOBS.append(dict(id="localdead", name="local-dead", mode="series",
                            state="running", games_done=0, games_expected=2,
                            submitted=time.time(), started=time.time(),
                            finished=None, error=None, log=[]))
server._persist_jobs()
ok(os.path.isfile(os.path.join(TMP, "aux-state.json")), "aux state persisted")
ok(oct(os.stat(os.path.join(TMP, "aux-state.json")).st_mode & 0o777) == "0o600",
   "aux state file is 0600 (bearers)")
# simulate the restart: wipe in-memory state, restore from disk
with server.AUX_LOCK:
    server.AUX.clear()
with server.POOL_LOCK:
    server.POOL.clear()
with server.JOBS_LOCK:
    server.JOBS.clear()
server._restore_state()
with server.AUX_LOCK:
    ok(server.AUX.get(jid6, {}).get("bearer") == b6, "bearer restored")
j6 = server._job(jid6)
ok(j6 and j6["state"] == "running"
   and any("restarted" in x for x in j6["log"]), "aux job still running after restart")
ok(server._job("localdead")["state"] == "failed"
   and "restarted" in server._job("localdead")["error"],
   "local job failed loudly on restart")
# the worker's callbacks still authenticate post-restart
st, _ = req(f"/api/aux/{jid6}/live", {"lines": [{"t": 1}]},
            headers={"X-Aux-Token": b6})
ok(st == 200, "callback authenticates with the restored bearer")
st, _ = req(f"/api/aux/{jid6}/done", {"series": {}},
            headers={"X-Aux-Token": b6})
ok(st == 200 and server._job(jid6)["state"] == "done", "restored job completes")

# capacity gate
with open(os.path.join(TMP, "aux.json"), "w") as fh:
    json.dump({"do_token": "fake", "callback_base": "https://cb.example",
               "max_concurrent": 0}, fh)
st, r = req("/api/run", {"mode": "match", "seed": 3, "executor": "auxiliary",
                         "bots": ["merchant", "corsair"], "name": "aux-cap"})
ok(st == 400 and "capacity" in r["error"], "capacity gate refuses over-provision")

srv.shutdown()
import shutil
shutil.rmtree(TMP, ignore_errors=True)
print("FAILURES:", fails)
sys.exit(1 if fails else 0)
