#!/usr/bin/env python3
"""Flotilla server — the whole package in one process, stdlib only.

    export DO_INFERENCE_KEY=...        # your serverless-inference key
    python3 server.py                  # http://127.0.0.1:8080

Serves the dashboard + replay player + library, and RUNS games: the dashboard's
Configure tab (or any agent) POSTs a run-config to /api/run and the server executes
it with sim/run_config.py, files the results into the library, and refreshes the
index. Agent-first API (all JSON; same schema as the GUI):

  GET  /config-schema.json   every knob: type, default, bounds, doc
  GET  /CONFIG.md            the same, human-readable
  GET  /api/health           {ok, version, queue}
  GET  /api/models           {models: [...ids from the inference endpoint...],
                             scripted: [...built-in bots...]}  (cached 10 min)
  GET  /api/prompts          saved operator prompts {name: text}
  POST /api/prompts          {"name": ..., "text": ...} saves (empty text deletes)
  POST /api/run              run-config JSON (see sim/run_config.py docstring;
                             optional "name" labels the result in the library)
                             -> {job}   jobs queue FIFO, one runs at a time
  GET  /api/runs             recent jobs with state + log tail
  POST /api/cancel           {"id": <job id>} -> cancel a queued/running job
  POST /api/rename           {"series": <name>, "display_name": <text>} -> persist a
                             series display name (empty display_name clears it)
  POST /api/bundle           {"series": <name>, "name": <title>?} -> build a
                             self-contained spoiler-free HTML bundle server-side
                             -> {file: "bundles/<x>.html"}
  POST /api/import           raw replay JSON in the body (?name=...) -> library

Auth: none on the loopback default — put a reverse proxy with auth in front for
public serving (see deploy/). Bind elsewhere with FLOTILLA_BIND=0.0.0.0:8080.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "sim"))
sys.path.insert(0, os.path.join(HERE, "scripts"))
import config_schema                    # noqa: E402
from libindex import build_index        # noqa: E402
from make_bundle import build_bundle    # noqa: E402

LIB = os.path.abspath(os.environ.get("FLOTILLA_LIBRARY",
                                     os.path.join(HERE, "library")))
BIND = os.environ.get("FLOTILLA_BIND", "127.0.0.1:8080")
VERSION = open(os.path.join(HERE, "VERSION")).read().strip() \
    if os.path.exists(os.path.join(HERE, "VERSION")) else "dev"

JOBS = []                               # newest last; dicts, lock-guarded
JOBS_LOCK = threading.Lock()
PROCS = {}                              # job id -> live Popen (never serialized)
RUN_QUEUE = threading.Semaphore(int(os.environ.get("FLOTILLA_CONCURRENT_RUNS", "1")))


def _san(name):
    return re.sub(r"[^a-zA-Z0-9 _.-]", "", str(name)).strip().replace(" ", "-")[:60]


def _job(jid):
    with JOBS_LOCK:
        return next((j for j in JOBS if j["id"] == jid), None)


def _persist_jobs():
    with JOBS_LOCK:
        snap = [dict(j, log=j["log"][-30:]) for j in JOBS[-100:]]
    with open(os.path.join(LIB, "jobs.json"), "w") as fh:
        json.dump(snap, fh, indent=1)


def _normalize_results(job, outdir):
    """File a finished run's artifacts into the library under its name."""
    mode, name = job["mode"], job["name"]
    if mode == "match":
        dst = os.path.join(LIB, "matches", f"{name}.json")
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.move(os.path.join(outdir, "match.json"), dst)
    elif mode == "series":
        dst = os.path.join(LIB, "series", name)
        shutil.rmtree(dst, ignore_errors=True)
        shutil.move(outdir, dst)
    else:
        dst = os.path.join(LIB, "tournaments", name)
        shutil.rmtree(dst, ignore_errors=True)
        shutil.move(outdir, dst)


def _publish_partial_series(job, outdir):
    """Live publishing: each finished game of a series lands in the library as it
    completes (with a partial series.json), so spectators follow along instead of
    waiting for the whole job. The final _normalize_results replaces it all with
    the canonical result. Must NEVER break the run — best-effort only."""
    try:
        dst = os.path.join(LIB, "series", job["name"])
        os.makedirs(dst, exist_ok=True)
        rows = []
        for line in job["log"]:
            if '"winner"' in line:
                try:
                    r = json.loads(line)
                    if "winner" in r and "file" in r:
                        rows.append(r)
                except ValueError:
                    pass
        for i, r in enumerate(rows):
            fn = os.path.basename(r["file"])
            src = os.path.join(outdir, fn)
            if os.path.isfile(src):
                tmp = os.path.join(dst, fn + ".tmp")
                shutil.copy2(src, tmp)
                os.replace(tmp, os.path.join(dst, fn))     # atomic vs readers
        with open(os.path.join(dst, "series.json"), "w") as fh:
            json.dump({"games": [dict(game=i + 1, seed=r.get("seed"),
                                      file=os.path.basename(r["file"]),
                                      winner=r.get("winner"))
                                 for i, r in enumerate(rows)],
                       "memos": {}, "partial": True}, fh, indent=1)
        build_index(LIB)
    except Exception:
        pass


def _run_job(job, cfg):
    with RUN_QUEUE:
        if job.get("cancel"):                     # cancelled while queued
            job["state"] = "cancelled"
            job["finished"] = time.time()
            _persist_jobs()
            return
        job["state"] = "running"
        job["started"] = time.time()
        _persist_jobs()
        outdir = os.path.join(LIB, "_work", job["id"])
        os.makedirs(outdir, exist_ok=True)
        cfg["outdir"] = outdir
        cfgpath = os.path.join(outdir, "run-config.json")
        with open(cfgpath, "w") as fh:
            json.dump(cfg, fh, indent=1)
        try:
            p = subprocess.Popen([sys.executable,
                                  os.path.join(HERE, "sim", "run_config.py"), cfgpath],
                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                 text=True, cwd=HERE)
            PROCS[job["id"]] = p
            for line in p.stdout:
                job["log"].append(line.rstrip()[:400])
                if '"winner"' in line:
                    job["games_done"] += 1
                    if job["mode"] == "series":
                        _publish_partial_series(job, outdir)
                _persist_jobs()
            rc = p.wait()
            if job.get("cancel"):
                job["state"] = "cancelled"
            elif rc != 0:
                raise RuntimeError(f"runner exited {rc}: {job['log'][-1] if job['log'] else ''}")
            else:
                _normalize_results(job, outdir)
                build_index(LIB)
                job["state"] = "done"
        except Exception as e:
            job["state"] = "cancelled" if job.get("cancel") else "failed"
            if job["state"] == "failed":
                job["error"] = str(e)[:300]
        finally:
            PROCS.pop(job["id"], None)
            job["finished"] = time.time()
            shutil.rmtree(os.path.join(LIB, "_work", job["id"]), ignore_errors=True)
            _persist_jobs()


def submit_run(cfg):
    mode = cfg.get("mode", "match")
    if mode not in ("match", "series", "tournament"):
        raise ValueError(f"mode must be match|series|tournament, got {mode!r}")
    config_schema.resolve(cfg.get("scenario") or {})        # loud validation up front
    bots = cfg.get("participants" if mode == "tournament" else "bots") or []
    if not (2 <= len(bots) <= 4) and mode != "tournament":
        raise ValueError("need 2-4 fleets")
    if mode == "tournament" and len(bots) < 2:
        raise ValueError("need >=2 participants")
    jid = time.strftime("%Y%m%d-%H%M%S") + f"-{os.urandom(2).hex()}"
    name = _san(cfg.get("name") or f"{mode}-{jid}")
    exp = 1
    if mode == "series":
        exp = int((cfg.get("series") or {}).get("games", 3))
    job = dict(id=jid, name=name, mode=mode, state="queued", games_done=0,
               games_expected=exp, submitted=time.time(), started=None,
               finished=None, error=None, log=[])
    with JOBS_LOCK:
        JOBS.append(job)
    _persist_jobs()
    threading.Thread(target=_run_job, args=(job, dict(cfg)), daemon=True).start()
    return job


ROUTES_STATIC = {
    "/": ("dash/dashboard.html", "text/html"),
    "/index.html": ("dash/dashboard.html", "text/html"),
    "/player.html": ("viewer/index.html", "text/html"),
}

MODELS_CACHE = {"at": 0.0, "ids": []}
SCRIPTED_BOTS = ["merchant", "corsair", "admiralty", "turtle"]


def _models():
    """Model ids available on the configured inference endpoint (cached 10 min)."""
    import urllib.request
    key = os.environ.get("DO_INFERENCE_KEY", "")
    now = time.time()
    if now - MODELS_CACHE["at"] < 600:
        return MODELS_CACHE["ids"]
    ids = []
    if key:
        try:
            base = os.environ.get("DO_INFERENCE_BASE", "https://inference.do-ai.run/v1")
            req = urllib.request.Request(base + "/models",
                                         headers={"Authorization": f"Bearer {key}"})
            with urllib.request.urlopen(req, timeout=15) as r:
                d = json.load(r)
            ids = sorted(m.get("id", "") for m in d.get("data", []) if m.get("id"))
        except Exception:
            ids = []
    MODELS_CACHE.update(at=now, ids=ids)
    return ids


def _prompts_path():
    return os.path.join(LIB, "prompts.json")


def _load_prompts():
    try:
        with open(_prompts_path()) as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


class H(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else \
            (body if isinstance(body, str) else json.dumps(body)).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path in ROUTES_STATIC:
            rel, ct = ROUTES_STATIC[path]
            return self._send(200, open(os.path.join(HERE, rel), "rb").read(), ct)
        if path == "/config-schema.json":
            return self._send(200, config_schema.schema_json())
        if path == "/CONFIG.md":
            return self._send(200, config_schema.config_md(), "text/markdown")
        if path == "/api/health":
            with JOBS_LOCK:
                q = sum(1 for j in JOBS if j["state"] in ("queued", "running"))
            return self._send(200, {"ok": True, "version": VERSION, "queue": q,
                                    "runs_enabled": bool(os.environ.get("DO_INFERENCE_KEY"))
                                    or True})
        if path == "/api/stats":
            # self-reported host stats — no monitoring agent needed, stdlib only
            stats = {"jobs_running": 0, "version": VERSION}
            try:
                with JOBS_LOCK:
                    stats["jobs_running"] = sum(1 for j in JOBS
                                                if j["state"] in ("queued", "running"))
                with open("/proc/loadavg") as fh:
                    stats["load_1m"] = float(fh.read().split()[0])
                mem = {}
                with open("/proc/meminfo") as fh:
                    for ln in fh:
                        k, v = ln.split(":", 1)
                        mem[k] = int(v.split()[0])
                stats["mem_total_mb"] = mem.get("MemTotal", 0) // 1024
                stats["mem_available_mb"] = mem.get("MemAvailable", 0) // 1024
                du = shutil.disk_usage(LIB)
                stats["disk_free_gb"] = round(du.free / 1e9, 2)
                stats["library_files"] = sum(len(fs) for _, _, fs in os.walk(LIB))
            except Exception as e:
                stats["error"] = str(e)[:100]
            return self._send(200, stats)
        if path == "/api/models":
            return self._send(200, {"models": _models(), "scripted": SCRIPTED_BOTS})
        if path == "/api/prompts":
            return self._send(200, _load_prompts())
        if path == "/api/runs":
            with JOBS_LOCK:
                out = [dict(j, log=j["log"][-8:]) for j in JOBS[-20:]][::-1]
            return self._send(200, {"jobs": out})
        if path == "/index.json":
            p = os.path.join(LIB, "index.json")
            if not os.path.exists(p):
                build_index(LIB)
            return self._send(200, open(p, "rb").read())
        # library files: replays/<match.json> | replays/<series>/<g.json> |
        # tournaments/... | bundles/...
        m = re.match(r"^/replays/([^/]+)$", path)
        if m:
            return self._file(os.path.join(LIB, "matches", m.group(1)))
        m = re.match(r"^/replays/([^/]+)/([^/]+)$", path)
        if m:
            return self._file(os.path.join(LIB, "series", m.group(1), m.group(2)))
        m = re.match(r"^/(tournaments|bundles)/(.+)$", path)
        if m:
            safe = os.path.normpath(m.group(2))
            if safe.startswith(".."):
                return self._send(404, {"error": "no"})
            ct = "text/html" if safe.endswith(".html") else "application/json"
            return self._file(os.path.join(LIB, m.group(1), safe), ct)
        return self._send(404, {"error": "not found"})

    def _file(self, p, ctype="application/json"):
        if not os.path.isfile(p):
            return self._send(404, {"error": "not found"})
        return self._send(200, open(p, "rb").read(), ctype)

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        try:
            n = int(self.headers.get("Content-Length", 0))
            if n > 90_000_000:
                return self._send(413, {"error": "too large"})
            body = self.rfile.read(n)
            if path == "/api/run":
                cfg = json.loads(body)
                job = submit_run(cfg)
                return self._send(202, {"job": dict(job, log=[])})
            if path == "/api/cancel":
                jid = json.loads(body or b"{}").get("id", "")
                j = _job(jid)
                if not j:
                    return self._send(404, {"error": "no such job"})
                if j["state"] not in ("queued", "running"):
                    return self._send(400, {"error": f"job already {j['state']}"})
                j["cancel"] = True
                p = PROCS.get(jid)
                if p:
                    try:
                        p.terminate()
                    except Exception:
                        pass
                elif j["state"] == "queued":       # not started yet: settle it now
                    j["state"] = "cancelled"
                    j["finished"] = time.time()
                _persist_jobs()
                return self._send(200, {"ok": True, "state": j["state"]})
            if path == "/api/prompts":
                d = json.loads(body)
                name = _san(str(d.get("name", "")))[:40]
                text = str(d.get("text", ""))[:8000]
                if not name:
                    return self._send(400, {"error": "prompt needs a name"})
                prompts = _load_prompts()
                if text.strip():
                    prompts[name] = text
                else:
                    prompts.pop(name, None)
                with open(_prompts_path(), "w") as fh:
                    json.dump(prompts, fh, indent=1)
                return self._send(200, prompts)
            if path == "/api/rename":
                d = json.loads(body)
                name = os.path.basename(str(d.get("series", "")))
                disp = str(d.get("display_name", "")).strip()[:120]
                spath = os.path.join(LIB, "series", name, "series.json")
                if not name or not os.path.isfile(spath):
                    return self._send(404, {"error": "no such series"})
                s = json.load(open(spath))
                if disp:
                    s["display_name"] = disp
                else:
                    s.pop("display_name", None)
                with open(spath, "w") as fh:
                    json.dump(s, fh, indent=1)
                build_index(LIB)
                return self._send(200, {"ok": True, "display_name": disp or None})
            if path == "/api/bundle":
                d = json.loads(body)
                name = os.path.basename(str(d.get("series", "")))
                sdir = os.path.join(LIB, "series", name)
                if not name or not os.path.isfile(os.path.join(sdir, "series.json")):
                    return self._send(404, {"error": "no such series"})
                title = str(d.get("name") or "").strip() or name
                safe = _san(title) or name
                os.makedirs(os.path.join(LIB, "bundles"), exist_ok=True)
                out = os.path.join(LIB, "bundles", f"{safe}.html")
                build_bundle(sdir, title, out)
                build_index(LIB)
                return self._send(200, {"ok": True, "file": f"bundles/{safe}.html"})
            if path == "/api/import":
                qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                name = _san((qs.get("name") or ["imported"])[0]) or "imported"
                rp = json.loads(body)
                assert "frames" in rp and "result" in rp, "not a flotilla replay"
                os.makedirs(os.path.join(LIB, "matches"), exist_ok=True)
                with open(os.path.join(LIB, "matches", f"{name}.json"), "w") as fh:
                    json.dump(rp, fh, separators=(",", ":"))
                build_index(LIB)
                return self._send(200, {"ok": True, "file": f"replays/{name}.json"})
            return self._send(404, {"error": "not found"})
        except (ValueError, KeyError, AssertionError) as e:
            return self._send(400, {"error": str(e)})
        except Exception as e:
            return self._send(500, {"error": str(e)[:300]})


def main():
    os.makedirs(LIB, exist_ok=True)
    for d in ("matches", "series", "tournaments", "bundles"):
        os.makedirs(os.path.join(LIB, d), exist_ok=True)
    build_index(LIB)
    host, port = BIND.rsplit(":", 1)
    if not os.environ.get("DO_INFERENCE_KEY"):
        print("NOTE: DO_INFERENCE_KEY not set — scripted-bot runs only until you export it.")
    srv = ThreadingHTTPServer((host, int(port)), H)
    print(f"Flotilla server {VERSION} — http://{BIND}  (library: {LIB})")
    srv.serve_forever()


if __name__ == "__main__":
    main()
