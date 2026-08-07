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
import hashlib
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
                                 text=True, cwd=HERE,
                                 env={**os.environ,
                                      "FLOTILLA_LIVE": os.path.join(outdir,
                                                                    "live.jsonl")})
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
    if cfg.pop("executor", None) == "auxiliary":
        if _aux_cfg() is None:
            raise ValueError("auxiliary executor not configured — "
                             "POST /api/aux-config first")
        with AUX_LOCK:
            active = len(AUX)
        if active >= int(_aux_cfg().get("max_concurrent", 3)):
            raise ValueError(f"auxiliary fleet at capacity ({active})")
        with JOBS_LOCK:
            JOBS.append(job)
        _persist_jobs()
        threading.Thread(target=_run_job_aux, args=(job, dict(cfg)),
                         daemon=True).start()
        return job
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

def _showcase_cfg():
    """Showcase publishing config: env first (self-hosters), else the stored file
    (set once via POST /api/showcase-config; never served by any route)."""
    env = {k.lower().replace("showcase_", ""): os.environ[k]
           for k in ("SHOWCASE_ACCESS_KEY", "SHOWCASE_SECRET_KEY",
                     "SHOWCASE_ENDPOINT", "SHOWCASE_BUCKET", "SHOWCASE_REGION")
           if os.environ.get(k)}
    if {"access_key", "secret_key", "endpoint", "bucket"} <= set(env):
        env.setdefault("region", "nyc3")
        return env
    try:
        with open(os.path.join(LIB, "showcase.json")) as fh:
            d = json.load(fh)
        if {"access_key", "secret_key", "endpoint", "bucket"} <= set(d):
            d.setdefault("region", "nyc3")
            return d
    except Exception:
        pass
    return None


def _s3_put_public(cfg, key, data, content_type="text/html"):
    """Minimal SigV4 S3 PUT with public-read ACL — stdlib only, no boto."""
    import datetime as _dt
    import hmac
    import urllib.request as _rq
    host = f"{cfg['bucket']}.{cfg['endpoint']}"
    now = _dt.datetime.now(_dt.timezone.utc)
    amzdate = now.strftime("%Y%m%dT%H%M%SZ")
    datestamp = now.strftime("%Y%m%d")
    region = cfg.get("region", "nyc3")
    payload_hash = hashlib.sha256(data).hexdigest()
    uri = "/" + urllib.parse.quote(key)
    headers = {"host": host, "x-amz-acl": "public-read",
               "x-amz-content-sha256": payload_hash, "x-amz-date": amzdate,
               "content-type": content_type}
    signed = ";".join(sorted(headers))
    canonical = ("PUT\n" + uri + "\n\n"
                 + "".join(f"{k}:{headers[k]}\n" for k in sorted(headers))
                 + "\n" + signed + "\n" + payload_hash)
    scope = f"{datestamp}/{region}/s3/aws4_request"
    sts = ("AWS4-HMAC-SHA256\n" + amzdate + "\n" + scope + "\n"
           + hashlib.sha256(canonical.encode()).hexdigest())

    def _hm(k, m):
        return hmac.new(k, m.encode(), hashlib.sha256).digest()

    sk = _hm(_hm(_hm(_hm(("AWS4" + cfg["secret_key"]).encode(), datestamp),
                     region), "s3"), "aws4_request")
    sig = hmac.new(sk, sts.encode(), hashlib.sha256).hexdigest()
    auth = (f"AWS4-HMAC-SHA256 Credential={cfg['access_key']}/{scope}, "
            f"SignedHeaders={signed}, Signature={sig}")
    req = _rq.Request(f"https://{host}{uri}", data=data, method="PUT",
                      headers={**{k: v for k, v in headers.items() if k != "host"},
                               "Authorization": auth})
    with _rq.urlopen(req, timeout=180) as r:
        return r.status


def _showcase_list_path():
    return os.path.join(LIB, "showcase-list.json")


def _showcase_list():
    try:
        with open(_showcase_list_path()) as fh:
            d = json.load(fh)
        return d if isinstance(d, list) else []
    except Exception:
        return []


# ---- fleet auxiliaries: disposable worker droplets (docs/FLEET_AUXILIARIES.md) ----
AUX = {}                                # job id -> {"bearer", "droplet_id", "born"}
AUX_LOCK = threading.Lock()


def _aux_cfg():
    env = {k.lower().replace("aux_", ""): os.environ[k]
           for k in ("AUX_DO_TOKEN", "AUX_CALLBACK_BASE", "AUX_CALLBACK_AUTH",
                     "AUX_SIZE", "AUX_REGION", "AUX_MAX_CONCURRENT",
                     "AUX_MAX_AGE_H") if os.environ.get(k)}
    if not {"do_token", "callback_base"} <= set(env):
        try:
            with open(os.path.join(LIB, "aux.json")) as fh:
                env = json.load(fh)
        except Exception:
            return None
    if not {"do_token", "callback_base"} <= set(env):
        return None
    env.setdefault("size", "s-1vcpu-1gb")
    env.setdefault("region", "nyc3")
    env.setdefault("max_concurrent", 3)
    env.setdefault("max_age_h", 8)
    return env


def _do(cfg, method, path, body=None):
    import urllib.request as _rq
    req = _rq.Request("https://api.digitalocean.com/v2" + path,
                      data=json.dumps(body).encode() if body is not None else None,
                      method=method,
                      headers={"Authorization": f"Bearer {cfg['do_token']}",
                               "Content-Type": "application/json"})
    with _rq.urlopen(req, timeout=45) as r:
        raw = r.read()
        return json.loads(raw) if raw else {}


def _aux_user_data(jid, bearer, cfg):
    base = cfg["callback_base"].rstrip("/")
    basic = f'-u "{cfg["callback_auth"]}" ' if cfg.get("callback_auth") else ""
    return f"""#!/bin/bash
mkdir -p /opt/flotilla /etc/flotilla-aux
for i in $(seq 1 60); do
  curl -fsS {basic}-H "X-Aux-Token: {bearer}" \\
    "{base}/api/aux/{jid}/app.tar.gz" -o /tmp/app.tgz && break
  sleep 5
done
tar xzf /tmp/app.tgz -C /opt/flotilla
curl -fsS {basic}-H "X-Aux-Token: {bearer}" \\
  "{base}/api/aux/{jid}/job.json" -o /etc/flotilla-aux/job.json
chmod 600 /etc/flotilla-aux/job.json
nohup python3 /opt/flotilla/scripts/aux_agent.py >/var/log/flotilla-aux.log 2>&1 &
"""


def _app_tarball():
    """The running app, packed for a worker: code only, never the library."""
    import io
    import tarfile
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for item in ("server.py", "VERSION", "sim", "scripts", "viewer", "dash"):
            p = os.path.join(HERE, item)
            if os.path.exists(p):
                tar.add(p, arcname=item, filter=lambda ti:
                        None if "__pycache__" in ti.name else ti)
    return buf.getvalue()


def _aux_destroy(jid):
    cfg = _aux_cfg()
    with AUX_LOCK:
        rec = AUX.pop(jid, None)
    if cfg and rec and rec.get("droplet_id"):
        try:
            _do(cfg, "DELETE", f"/droplets/{rec['droplet_id']}")
        except Exception:
            pass                        # the reaper sweeps stragglers by tag


def _run_job_aux(job, cfg):
    aux = _aux_cfg()
    jid = job["id"]
    import secrets as _sec
    bearer = "kaux_" + _sec.token_urlsafe(24)
    with AUX_LOCK:
        AUX[jid] = {"bearer": bearer, "droplet_id": None, "born": time.time(),
                    "config": cfg, "rows": []}
    job["state"] = "running"
    job["started"] = time.time()
    job["aux"] = True
    _persist_jobs()
    try:
        d = _do(aux, "POST", "/droplets", {
            "name": f"flotilla-aux-{jid}", "region": aux["region"],
            "size": aux["size"], "image": "debian-13-x64",
            "tags": ["flotilla-aux"],
            "user_data": _aux_user_data(jid, bearer, aux)})
        with AUX_LOCK:
            AUX[jid]["droplet_id"] = d["droplet"]["id"]
        job["log"].append(f"auxiliary droplet {d['droplet']['id']} provisioning")
        _persist_jobs()
    except Exception as e:
        job["state"] = "failed"
        job["error"] = f"aux provision failed: {type(e).__name__}: {e}"[:250]
        job["finished"] = time.time()
        _persist_jobs()
        _aux_destroy(jid)
        return
    deadline = time.time() + float(aux["max_age_h"]) * 3600
    while job["state"] == "running" and time.time() < deadline:
        time.sleep(20)
    if job["state"] == "running":       # blew the age cap — reap it
        job["state"] = "failed"
        job["error"] = f"auxiliary exceeded max_age_h={aux['max_age_h']}"
        job["finished"] = time.time()
        _persist_jobs()
    _aux_destroy(jid)


def _aux_reaper():
    while True:
        time.sleep(300)
        cfg = _aux_cfg()
        if not cfg:
            continue
        try:
            d = _do(cfg, "GET", "/droplets?tag_name=flotilla-aux&per_page=100")
            with AUX_LOCK:
                live = {r["droplet_id"] for r in AUX.values() if r.get("droplet_id")}
            for dr in d.get("droplets", []):
                if dr["id"] not in live:
                    _do(cfg, "DELETE", f"/droplets/{dr['id']}")
        except Exception:
            pass


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
                                    "showcase": _showcase_cfg() is not None,
                                    "aux": _aux_cfg() is not None,
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
        if path == "/api/showcase":
            return self._send(200, {"enabled": _showcase_cfg() is not None,
                                    "published": _showcase_list()})
        if path == "/api/runs":
            with JOBS_LOCK:
                out = [dict(j, log=j["log"][-8:]) for j in JOBS[-20:]][::-1]
            return self._send(200, {"jobs": out})
        if path == "/index.json":
            p = os.path.join(LIB, "index.json")
            if not os.path.exists(p):
                build_index(LIB)
            return self._send(200, open(p, "rb").read())
        m = re.match(r"^/api/aux/([A-Za-z0-9_.-]+)/(app\.tar\.gz|job\.json)$", path)
        if m:
            jid, what = m.groups()
            with AUX_LOCK:
                rec = AUX.get(jid)
            if rec is None or self.headers.get("X-Aux-Token") != rec["bearer"]:
                return self._send(401, {"error": "bad aux token"})
            if what == "app.tar.gz":
                return self._send(200, _app_tarball(), "application/gzip")
            aux = _aux_cfg() or {}
            return self._send(200, {
                "job_id": jid, "bearer": rec["bearer"],
                "callback_base": aux.get("callback_base", ""),
                "callback_auth": aux.get("callback_auth", ""),
                "config": rec["config"],
                "inference_key": os.environ.get("DO_INFERENCE_KEY", "")})
        m = re.match(r"^/api/live/([A-Za-z0-9_.-]+)$", path)
        if m:
            jid = m.group(1)
            j = _job(jid)
            if not j:
                return self._send(404, {"error": "no such job"})
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            try:
                ofs = max(0, int((qs.get("ofs") or ["0"])[0]))
            except ValueError:
                ofs = 0
            livef = os.path.join(LIB, "_work", jid, "live.jsonl")
            out = {"ofs": ofs, "lines": [], "state": j["state"],
                   "name": j["name"], "mode": j["mode"],
                   "game": min(j["games_done"] + (0 if j["state"] != "running" else 1),
                               j.get("games_expected", 1))}
            if os.path.isfile(livef):
                size = os.path.getsize(livef)
                if ofs > size:
                    ofs = 0                      # file truncated: a new game began
                with open(livef) as fh:
                    fh.seek(ofs)
                    chunk = fh.read(4_000_000)
                nl = chunk.rfind("\n")
                if nl >= 0:                      # only complete lines ship
                    for ln in chunk[:nl].split("\n"):
                        if ln:
                            try:
                                out["lines"].append(json.loads(ln))
                            except ValueError:
                                pass
                    ofs += nl + 1
                out["ofs"] = ofs
            return self._send(200, out)
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
            m = re.match(r"^/api/aux/([A-Za-z0-9_.-]+)/(live|game|done|fail)$", path)
            if m:
                jid, what = m.groups()
                with AUX_LOCK:
                    rec = AUX.get(jid)
                j = _job(jid)
                if rec is None or j is None \
                        or self.headers.get("X-Aux-Token") != rec["bearer"]:
                    return self._send(401, {"error": "bad aux token"})
                d = json.loads(body or b"{}")
                wd = os.path.join(LIB, "_work", jid)
                os.makedirs(wd, exist_ok=True)
                if what == "live":
                    with open(os.path.join(wd, "live.jsonl"), "a") as fh:
                        for line in d.get("lines", []):
                            fh.write(json.dumps(line, separators=(",", ":")) + "\n")
                    return self._send(200, {"ok": True})
                if what == "game":
                    fn = os.path.basename(str(d.get("file", "")))
                    rp = d.get("replay")
                    if not fn.endswith(".json") or not isinstance(rp, dict):
                        return self._send(400, {"error": "bad game payload"})
                    if j["mode"] == "series":
                        dst = os.path.join(LIB, "series", j["name"])
                        os.makedirs(dst, exist_ok=True)
                        tmp = os.path.join(dst, fn + ".tmp")
                        with open(tmp, "w") as fh:
                            json.dump(rp, fh, separators=(",", ":"))
                        os.replace(tmp, os.path.join(dst, fn))
                        if d.get("row"):
                            rec["rows"].append(d["row"])
                            j["games_done"] += 1
                            j["log"].append(json.dumps(d["row"])[:400])
                        with open(os.path.join(dst, "series.json"), "w") as fh:
                            json.dump({"games": [
                                dict(game=i + 1, seed=r.get("seed"),
                                     file=os.path.basename(r["file"]),
                                     winner=r.get("winner"))
                                for i, r in enumerate(rec["rows"])],
                                "memos": {}, "partial": True}, fh, indent=1)
                    else:
                        os.makedirs(os.path.join(LIB, "matches"), exist_ok=True)
                        with open(os.path.join(LIB, "matches",
                                               f"{j['name']}.json"), "w") as fh:
                            json.dump(rp, fh, separators=(",", ":"))
                        j["games_done"] += 1
                    build_index(LIB)
                    _persist_jobs()
                    return self._send(200, {"ok": True})
                if what == "done":
                    if j["mode"] == "series":
                        dst = os.path.join(LIB, "series", j["name"])
                        os.makedirs(dst, exist_ok=True)
                        ser = d.get("series") or {}
                        ser.pop("partial", None)
                        with open(os.path.join(dst, "series.json"), "w") as fh:
                            json.dump(ser, fh, indent=1)
                    build_index(LIB)
                    j["state"] = "done"
                    j["finished"] = time.time()
                    _persist_jobs()
                    threading.Thread(target=_aux_destroy, args=(jid,),
                                     daemon=True).start()
                    return self._send(200, {"ok": True})
                j["state"] = "failed"                      # fail
                j["error"] = str(d.get("error", "aux failed"))[:250]
                j["finished"] = time.time()
                _persist_jobs()
                threading.Thread(target=_aux_destroy, args=(jid,),
                                 daemon=True).start()
                return self._send(200, {"ok": True})
            if path == "/api/aux-config":
                d = json.loads(body)
                if not {"do_token", "callback_base"} <= set(d):
                    return self._send(400, {"error": "need do_token + callback_base "
                                            "(+ optional callback_auth/size/region/"
                                            "max_concurrent/max_age_h)"})
                p = os.path.join(LIB, "aux.json")
                with open(p, "w") as fh:
                    json.dump(d, fh)
                os.chmod(p, 0o600)
                return self._send(200, {"ok": True, "aux": True})
            if path == "/api/showcase-config":
                d = json.loads(body)
                need = {"access_key", "secret_key", "endpoint", "bucket"}
                if not need <= set(d):
                    return self._send(400, {"error": f"need {sorted(need)}"})
                p = os.path.join(LIB, "showcase.json")
                with open(p, "w") as fh:
                    json.dump({k: str(d[k]) for k in
                               ("access_key", "secret_key", "endpoint", "bucket",
                                "region") if k in d}, fh)
                os.chmod(p, 0o600)
                return self._send(200, {"ok": True, "showcase": True})
            if path == "/api/showcase":
                cfg = _showcase_cfg()
                if cfg is None:
                    return self._send(400, {"error": "showcase not configured — "
                                            "POST /api/showcase-config or set "
                                            "SHOWCASE_* env"})
                d = json.loads(body)
                title = str(d.get("name") or "").strip()
                if d.get("series"):
                    sname = os.path.basename(str(d["series"]))
                    sdir = os.path.join(LIB, "series", sname)
                    if not os.path.isfile(os.path.join(sdir, "series.json")):
                        return self._send(404, {"error": "no such series"})
                    title = title or sname
                    tmp = os.path.join(LIB, "_work", f"showcase-{_san(title)}.html")
                    os.makedirs(os.path.dirname(tmp), exist_ok=True)
                    build_bundle(sdir, title, tmp)
                    with open(tmp, "rb") as fh:
                        payload = fh.read()
                    os.remove(tmp)
                elif d.get("match"):
                    mfile = os.path.join(LIB, "matches",
                                         os.path.basename(str(d["match"])))
                    if not os.path.isfile(mfile):
                        # series game path: replays/<series>/<g>.json
                        parts = str(d["match"]).split("/")
                        mfile = os.path.join(LIB, "series", *[os.path.basename(p)
                                                              for p in parts[-2:]])
                    if not os.path.isfile(mfile):
                        return self._send(404, {"error": "no such replay"})
                    title = title or os.path.basename(mfile).rsplit(".", 1)[0]
                    with open(os.path.join(HERE, "viewer", "index.html")) as fh:
                        tpl = fh.read()
                    with open(mfile) as fh:
                        payload = tpl.replace("/*" + "EMBED_REPLAY" + "*/null",
                                              fh.read(), 1).encode()
                else:
                    return self._send(400, {"error": "give series or match"})
                key = f"showcase/{_san(title) or 'match'}.html"
                try:
                    _s3_put_public(cfg, key, payload)
                except Exception as e:
                    return self._send(502, {"error": f"upload failed: "
                                            f"{type(e).__name__}: {e}"[:200]})
                url = f"https://{cfg['bucket']}.{cfg['endpoint']}/{key}"
                pub = _showcase_list()
                pub = [x for x in pub if x.get("url") != url]
                pub.append({"name": title, "url": url,
                            "bytes": len(payload), "when": time.strftime(
                                "%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
                with open(_showcase_list_path(), "w") as fh:
                    json.dump(pub, fh, indent=1)
                return self._send(200, {"ok": True, "url": url,
                                        "bytes": len(payload)})
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
    threading.Thread(target=_aux_reaper, daemon=True).start()
    srv = ThreadingHTTPServer((host, int(port)), H)
    print(f"Flotilla server {VERSION} — http://{BIND}  (library: {LIB})")
    srv.serve_forever()


if __name__ == "__main__":
    main()
