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


def _headers(ctype="application/json"):
    import base64
    h = {"X-Aux-Token": JOB["bearer"], "Content-Type": ctype}
    if JOB.get("callback_auth"):
        h["Authorization"] = "Basic " + base64.b64encode(
            JOB["callback_auth"].encode()).decode()
    return h


def post(path, payload, attempts=5, raw=None):
    """Bearer rides X-Aux-Token; Authorization stays free for the reverse
    proxy's basic auth. Returns (status, response-dict) — the flagship rides
    COMMANDS home on these responses (the worker is outbound-only by design,
    so polling its own callbacks IS the command channel)."""
    # compact separators: replays are compact on disk; default separators
    # re-inflated a 10.7 MB game post to 12.8 MB of whitespace
    body = raw if raw is not None else json.dumps(
        payload, separators=(",", ":")).encode()
    ctype = "application/octet-stream" if raw is not None else "application/json"
    for i in range(attempts):
        try:
            req = urllib.request.Request(
                f"{BASE}/api/aux/{JOB['job_id']}/{path}", data=body,
                headers=_headers(ctype))
            with urllib.request.urlopen(req, timeout=60) as r:
                try:
                    return r.status, json.loads(r.read() or b"{}")
                except ValueError:
                    return r.status, {}
        except Exception:
            if i == attempts - 1:
                return None, {}
            time.sleep(2 ** i)


def fetch(path, attempts=5):
    """GET an artifact from the flagship (job-bearer gated), with backoff —
    a transient network blip must never be mistaken for a real 404."""
    for i in range(attempts):
        try:
            req = urllib.request.Request(f"{BASE}/api/aux/{JOB['job_id']}/{path}",
                                         headers=_headers())
            with urllib.request.urlopen(req, timeout=120) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, b""             # a real HTTP status is an answer
        except Exception:
            if i == attempts - 1:
                return None, b""
            time.sleep(2 ** i)


def post_game(fn, path, row):
    """Splice the on-disk replay bytes straight into the JSON envelope — the
    file is already compact JSON, so parsing and re-dumping a multi-MB replay
    cost seconds of CPU and ~100 MB of RAM on the little worker to produce the
    identical value."""
    with open(path, "rb") as fh:
        raw_replay = fh.read()
    body = (b'{"file":' + json.dumps(fn).encode()
            + b',"replay":' + raw_replay
            + b',"row":' + json.dumps(row, separators=(",", ":")).encode()
            + b"}")
    return post("game", None, raw=body)


def _drain(path, ofs):
    """New complete lines from path past ofs -> (lines, new_ofs). BYTES
    throughout: the old text-mode tail fed CHARACTER offsets to seek() — the
    first em dash in an admiral's thoughts desynchronized the cursor and the
    live stream silently died for the rest of the match. A NEW GAME truncates
    the file — reset the cursor or the tail sticks past EOF forever and games
    2+ never stream home."""
    try:
        if os.path.getsize(path) < ofs:
            ofs = 0
        with open(path, "rb") as fh:
            fh.seek(ofs)
            chunk = fh.read()
        nl = chunk.rfind(b"\n")
        if nl >= 0:
            return [json.loads(x) for x in chunk[:nl].split(b"\n") if x], \
                ofs + nl + 1
    except (FileNotFoundError, ValueError):
        pass
    return [], ofs


def tail_live(path, stop, outdir):
    """Tail the base live.jsonl home — plus every live-<lane>.jsonl a
    parallel tournament's matchup lanes write (each posts with its lane name,
    so the flagship keeps one stream per lane). Commands ride EVERY response;
    planting pause.flag twice is harmless."""
    ofs = 0
    lanes = {}                             # lane file path -> offset
    quiet = 0
    while not stop.is_set() or os.path.exists(path):
        sent = False
        lines, ofs = _drain(path, ofs)
        resp = {}
        if lines:
            sent = True
            _, resp = post("live", {"lines": lines})
        try:
            for fn in os.listdir(outdir):
                if fn.startswith("live-") and fn.endswith(".jsonl"):
                    lanes.setdefault(os.path.join(outdir, fn), 0)
        except OSError:
            pass
        for lp, lofs in list(lanes.items()):
            llines, lanes[lp] = _drain(lp, lofs)
            if llines:
                sent = True
                lane = os.path.basename(lp)[len("live-"):-len(".jsonl")]
                _, r2 = post("live", {"lines": llines, "lane": lane})
                resp = r2 or resp
        if not sent:
            quiet += 1
            if quiet >= 5:                # heartbeat: poll for commands anyway
                quiet = 0
                _, resp = post("live", {"lines": []})
        else:
            quiet = 0
        if resp.get("command") == "pause":
            # the runner freezes at its next window boundary + exits 75
            with open(os.path.join(outdir, "pause.flag"), "w") as fh:
                fh.write("1")
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
           "DO_INFERENCE_KEY": JOB.get("inference_key", ""),
           "FLOTILLA_PROVIDERS": JOB.get("providers_env", "")}
    # a RESUMED job: the flagship holds a checkpoint from the previous worker —
    # fetch it and thaw instead of starting fresh (checkpoints are PLAIN JSON:
    # tolerantly rehydrated on current code, no pickle anywhere in the chain)
    argv = [sys.executable, os.path.join(HERE, "sim", "run_config.py"), cfgpath]
    st_ck, ck = fetch("checkpoint.json.gz")
    if st_ck == 200 and ck:
        import gzip
        with open(os.path.join(outdir, "checkpoint.json"), "wb") as fh:
            fh.write(gzip.decompress(ck))
        argv = [sys.executable, os.path.join(HERE, "sim", "run_config.py"),
                "--resume", outdir]
    elif st_ck is None:
        # the flagship was unreachable through every retry: this might be a
        # RESUME whose checkpoint we simply couldn't get — starting fresh here
        # would silently REPLAY the series from game 1 over the real replays.
        # Die loudly instead; the operator can resume again.
        post("fail", {"error": "could not reach the flagship for the "
                               "checkpoint probe; refusing to start fresh"})
        return
    stop = threading.Event()
    t = threading.Thread(target=tail_live, args=(live_path, stop, outdir),
                         daemon=True)
    t.start()
    sent = set()
    try:
        p = subprocess.Popen(argv,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             text=True, cwd=HERE, env=env)
        _tail = []                         # last output lines: the fail post
        for line in p.stdout:              # must say WHY, not just the code
            _tail.append(line.rstrip()[:200])
            del _tail[:-12]
            if '"winner"' in line:
                try:
                    row = json.loads(line)
                    fn = os.path.relpath(row["file"], outdir)
                    if fn.startswith(".."):
                        fn = os.path.basename(row["file"])
                    post_game(fn, row["file"], row)
                    sent.add(fn)
                    tj = os.path.join(outdir, "tournament.json")
                    if os.path.exists(tj):       # bracket-so-far rides along
                        post("game", {"file": "tournament.json",
                                      "replay": json.load(open(tj)), "row": None})
                except (ValueError, KeyError, OSError):
                    pass
            if '"memos_saved"' in line:
                # the runner just rewrote a game file WITH its memos — resend
                # that exact file now (the "debrief"-line trigger below fires
                # before the write and only catches up a game later)
                try:
                    d = json.loads(line)
                    fn = os.path.relpath(d["file"], outdir)
                    if fn.startswith(".."):
                        fn = os.path.basename(d["file"])
                    fp = os.path.join(outdir, fn)
                    with open(fp, "rb") as fh:
                        head = fh.read()
                    if b'"memos"' in head:
                        post_game(fn, fp, None)
                        sent.discard(fn)
                except (ValueError, KeyError, OSError):
                    pass
            if '"debrief"' in line:
                # the just-debriefed game file gained embedded memos — resend
                for fn in sorted(sent):
                    fp = os.path.join(outdir, fn)
                    try:
                        with open(fp, "rb") as fh:
                            raw = fh.read()
                        if b'"memos"' in raw:
                            post_game(fn, fp, None)
                            sent.discard(fn)
                    except (ValueError, OSError):
                        pass
        rc = p.wait()
        stop.set()
        t.join(timeout=15)
        if rc == 75:                       # paused: ship the checkpoint home,
            import gzip                    # then the flagship reaps this box
            ckp = os.path.join(outdir, "checkpoint.json")
            # the runner writes the checkpoint atomically BEFORE exiting 75
            # (and refuses pauses it cannot checkpoint), so a missing file
            # here is a transient at worst — retry before declaring anything
            last = None
            for i in range(4):
                try:
                    with open(ckp, "rb") as fh:
                        blob = gzip.compress(fh.read(), 6)
                    post("paused", None, raw=blob)
                    return
                except OSError as e:
                    last = e
                    time.sleep(5 * (i + 1))
            post("fail", {"error": "paused but checkpoint unreadable after "
                          f"retries: {last} — completed games are already in "
                          "the library; reconcile-tournament rebuilds the "
                          "bracket from them"})
            return
        if rc != 0:
            post("fail", {"error": f"runner exited {rc}: "
                          + " | ".join(_tail[-6:])[:280]})
            return
        series = {}
        sj = os.path.join(outdir, "series.json")
        if os.path.exists(sj):
            series = json.load(open(sj))
        # final sweep: every game file (incl. matchup subdirs), memos and all
        for root, _dirs, files in os.walk(outdir):
            for fn in sorted(files):
                rel = os.path.relpath(os.path.join(root, fn), outdir)
                if (fn.startswith("g") and fn.endswith(".json")) \
                        or fn == "match.json":
                    try:
                        post_game(rel, os.path.join(root, fn), None)
                    except (ValueError, OSError):
                        pass
        tourn = None
        tj = os.path.join(outdir, "tournament.json")
        if os.path.exists(tj):
            try:
                tourn = json.load(open(tj))
            except ValueError:
                pass
        post("done", {"series": series, "tournament": tourn})
    except Exception as e:
        stop.set()
        post("fail", {"error": f"{type(e).__name__}: {e}"[:300]})


if __name__ == "__main__":
    main()
