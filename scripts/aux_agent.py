#!/usr/bin/env python3
"""Fleet-auxiliary agent — runs ON a disposable worker droplet.

Reads /etc/flotilla-aux/job.json (written by user_data at provision):
  {"job_id", "bearer", "callback_base", "config", "inference_key"}

Then: runs the job exactly as the flagship would (sim/run_config.py with a live
stream), pushing everything home over HTTPS as it happens:
  POST {base}/api/aux/{job}/live   {"lines": [...]}     each live.jsonl flush
  POST {base}/api/aux/{job}/game   {"file","replay","series"}  each finished game
  POST {base}/api/aux/{job}/done   {"series"}           the final series.json
  POST {base}/api/aux/{job}/fail   {"error"}            on any fatal error

Outbound-only, stdlib-only. Retries with backoff; the flagship's reaper destroys
this droplet after /done (or on timeout), so exiting is all the cleanup we do."""
import json
import os
import subprocess
import sys
import threading
import time
import urllib.request

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JOB = json.load(open("/etc/flotilla-aux/job.json"))
BASE = JOB["callback_base"].rstrip("/")


def post(path, payload, attempts=5):
    """Bearer rides X-Aux-Token; Authorization stays free for the reverse proxy's
    basic auth (callback_auth = 'user:pass' when the flagship is fronted)."""
    import base64
    body = json.dumps(payload).encode()
    headers = {"X-Aux-Token": JOB["bearer"], "Content-Type": "application/json"}
    if JOB.get("callback_auth"):
        headers["Authorization"] = "Basic " + base64.b64encode(
            JOB["callback_auth"].encode()).decode()
    for i in range(attempts):
        try:
            req = urllib.request.Request(
                f"{BASE}/api/aux/{JOB['job_id']}/{path}", data=body,
                headers=headers)
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.status
        except Exception:
            if i == attempts - 1:
                return None
            time.sleep(2 ** i)


def tail_live(path, stop):
    ofs = 0
    while not stop.is_set() or os.path.exists(path):
        lines = []
        try:
            with open(path) as fh:
                fh.seek(ofs)
                chunk = fh.read()
            nl = chunk.rfind("\n")
            if nl >= 0:
                lines = [json.loads(x) for x in chunk[:nl].split("\n") if x]
                ofs += nl + 1
        except FileNotFoundError:
            pass
        except ValueError:
            pass
        if lines:
            post("live", {"lines": lines})
        if stop.is_set():
            break
        time.sleep(2)


def main():
    outdir = "/tmp/flotilla-aux-out"
    os.makedirs(outdir, exist_ok=True)
    cfg = dict(JOB["config"])
    cfg["outdir"] = outdir
    cfgpath = os.path.join(outdir, "run-config.json")
    with open(cfgpath, "w") as fh:
        json.dump(cfg, fh)
    live_path = os.path.join(outdir, "live.jsonl")
    env = {**os.environ, "FLOTILLA_LIVE": live_path,
           "DO_INFERENCE_KEY": JOB.get("inference_key", "")}
    stop = threading.Event()
    t = threading.Thread(target=tail_live, args=(live_path, stop), daemon=True)
    t.start()
    sent = set()
    try:
        p = subprocess.Popen([sys.executable,
                              os.path.join(HERE, "sim", "run_config.py"), cfgpath],
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             text=True, cwd=HERE)
        for line in p.stdout:
            if '"winner"' in line:
                try:
                    row = json.loads(line)
                    fn = os.path.basename(row["file"])
                    with open(row["file"]) as fh:
                        rp = json.load(fh)
                    post("game", {"file": fn, "replay": rp, "row": row})
                    sent.add(fn)
                except (ValueError, KeyError, OSError):
                    pass
            if '"debrief"' in line:
                # the just-debriefed game file gained embedded memos — resend
                for fn in sorted(sent):
                    fp = os.path.join(outdir, fn)
                    try:
                        with open(fp) as fh:
                            rp = json.load(fh)
                        if "memos" in rp:
                            post("game", {"file": fn, "replay": rp, "row": None})
                            sent.discard(fn)
                    except (ValueError, OSError):
                        pass
        rc = p.wait()
        stop.set()
        t.join(timeout=15)
        if rc != 0:
            post("fail", {"error": f"runner exited {rc}"})
            return
        series = {}
        sj = os.path.join(outdir, "series.json")
        if os.path.exists(sj):
            series = json.load(open(sj))
        # final sweep: every game file, memos and all
        for fn in sorted(os.listdir(outdir)):
            if fn.startswith("g") and fn.endswith(".json"):
                try:
                    post("game", {"file": fn,
                                  "replay": json.load(open(os.path.join(outdir, fn))),
                                  "row": None})
                except (ValueError, OSError):
                    pass
            if fn == "match.json":
                post("game", {"file": fn,
                              "replay": json.load(open(os.path.join(outdir, fn))),
                              "row": None})
        post("done", {"series": series})
    except Exception as e:
        stop.set()
        post("fail", {"error": f"{type(e).__name__}: {e}"[:300]})


if __name__ == "__main__":
    main()
