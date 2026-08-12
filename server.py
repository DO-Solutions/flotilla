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
                             -> {job}   local jobs queue FIFO (one at a time);
                             auxiliary jobs run concurrently up to max_concurrent
  GET  /api/runs             recent jobs with state + log tail
  POST /api/cancel           {"id": <job id>} -> cancel a queued/running job
  POST /api/rename           {"series": <name>, "display_name": <text>} -> persist a
                             series display name (empty display_name clears it)
  POST /api/archive          {"series": <name> | "match": <file>, "archived": bool}
                             -> hide/show in the dashboard list (data untouched;
                             matches use the matches-meta.json sidecar)
  POST /api/delete-series    {"series": <name>} -> remove the series dir from the
                             library (refused while a job for it is live)
  POST /api/delete-match     {"match": <file>} -> remove a standalone match replay
  POST /api/bundle           {"series": <name>, "name": <title>?} -> build a
                             self-contained spoiler-free HTML bundle server-side
                             -> {file: "bundles/<x>.html"}
  POST /api/import           raw replay JSON in the body (?name=...) -> library

Run lifecycle:
  POST /api/pause            {"id"} -> checkpoint + freeze a match/series (not
                             tournaments); the run resumes exactly where it froze
  POST /api/resume           {"id"[, "where":"local"]} -> thaw a paused run
  GET  /api/live/<job>       tail the live stream: {lines, ofs, state, game,
                             games_expected, stream_game, more}. Poll with the
                             returned ofs; `more`=true means drain again now
  A model-API OUTAGE auto-pauses a run and a background prober resumes it every
  FLOTILLA_AUTORESUME_S (default 600) once the API recovers — local runs too.

Inference providers (dashboard "Server" tab — see docs/PROVIDERS.md):
  POST /api/providers        read the ladder (masked keys + aux summary)
  POST /api/providers-op     {"op": add|remove|toggle|move|fallback, ...}
  POST /api/provider-check   {"id"} -> verify the key + auto-discover/map models

Public showcase (see docs/FLEET_AUXILIARIES.md):
  POST /api/showcase-config  {access_key, secret_key, endpoint, bucket, region?}
  POST /api/showcase         {"series"|"match"} -> publish a public, no-login link
  GET  /api/showcase         {"enabled", "published": [{name, ident, url, ...}]}
  POST /api/showcase-delete  {"series"|"match"} -> retire that public link
  GET/POST /api/ships        operator ship classes for the Configure designer
                             (POST {"name","stats"} saves, {"name","delete"} removes)
  The public spectator player is player.html?livejsonl=<prefix> / ?replay=<path>
  / ?live=<job> / ?series=<name> (URL params are same-origin-only).

Fleet auxiliaries (disposable worker droplets — see docs/FLEET_AUXILIARIES.md):
  POST /api/aux-config       DO token + https callback_base (the ONE unauth lane
                             below is /api/aux/*, bearer-gated per job)
  POST /api/aux/<job>/<live|game|done|fail>   worker callbacks (X-Aux-Token)

  GET  /api/conn-reference   the conn order-language reference (also docs/CONN.md)
  GET  /api/base-prompt      the suggested admiral system prompt
  GET  /index.json           the library index (built on demand)

Auth: none on the loopback default — put a reverse proxy with auth in front for
public serving (see deploy/). The /api/aux/* worker lane is bearer-authed and
meant to stay OUTSIDE that basic auth; every other POST rejects a cross-origin
Origin (CSRF). Bind elsewhere with FLOTILLA_BIND=0.0.0.0:8080.

Environment: DO_INFERENCE_KEY (builtin provider), FLOTILLA_LIBRARY (data dir),
FLOTILLA_PROVIDERS (provider ladder JSON — normally injected, see providers.py),
FLOTILLA_CONCURRENT_RUNS (local queue width, default 1), FLOTILLA_AUTORESUME_S,
AUX_* (see docs/FLEET_AUXILIARIES.md), SHOWCASE_* (public bucket).
"""
import hashlib
import hmac
import json
import shlex
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
import run_config                       # noqa: E402,F401 — registers the
                                        # game (keelspring/contract.py) at boot
from keelspring import contract             # noqa: E402
import config_schema                    # noqa: E402
from libindex import build_index, matches_meta, save_matches_meta  # noqa: E402
from make_bundle import build_bundle    # noqa: E402

LIB = os.path.abspath(os.environ.get("FLOTILLA_LIBRARY",
                                     os.path.join(HERE, "library")))
BIND = os.environ.get("FLOTILLA_BIND", "127.0.0.1:8080")
VERSION = open(os.path.join(HERE, "VERSION")).read().strip() \
    if os.path.exists(os.path.join(HERE, "VERSION")) else "dev"

JOBS = []                               # newest last; dicts, lock-guarded
JOBS_LOCK = threading.Lock()
KS_LOCK = threading.RLock()             # provider key store read-modify-write
META_LOCK = threading.Lock()            # matches-meta sidecar read-modify-write
SJ_LOCK = threading.Lock()              # series.json read-modify-write
PROCS = {}                              # job id -> live Popen (never serialized)
# ---- inference-provider key store (dash "Server" tab) ----------------------
# server-keys.json (0600, never in git): ordered provider ladder + fallback
# thresholds. The DigitalOcean primary is BUILT IN — its key stays in the
# server environment and is injected at runner spawn, never stored on disk.
KEYSTORE = os.path.join(LIB, "server-keys.json")


def _keystore():
    try:
        with open(KEYSTORE) as fh:
            st = json.load(fh)
    except FileNotFoundError:
        st = {}
    except Exception:
        # NEVER silently reset the key store (it is deliberately kept out of
        # version control — a swallow-and-empty here would drop every stored
        # key). Park the bad bytes for recovery and start fresh.
        try:
            os.replace(KEYSTORE, KEYSTORE + ".corrupt")
        except OSError:
            pass
        st = {}
    st.setdefault("providers", [])
    st.setdefault("fallback", {"timeout_streak": 3,
                               "timeout_streak_pipelined": 5,
                               "error_streak": 2, "canary_minutes": 10})
    st.setdefault("limits", {"max_series_cost": 0})   # 0 = no ceiling
    if not any(p.get("id") == "digitalocean" for p in st["providers"]):
        st["providers"].insert(0, {
            "id": "digitalocean", "label": "DigitalOcean GenAI",
            "base_url": os.environ.get("DO_INFERENCE_BASE",
                                       "https://inference.do-ai.run/v1"),
            "builtin": True, "enabled": True, "order": -1, "models": []})
    st["providers"].sort(key=lambda p: p.get("order", 0))
    for i2, p in enumerate(st["providers"]):
        p["order"] = i2
    return st


def _write_secret_file(path, text):
    """Write a credential file 0600 FROM THE FIRST BYTE (os.open with the mode)
    + atomic replace. An open()-then-chmod leaves a window at the umask default
    where the DO token / Spaces key / worker bearers are world-readable."""
    tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(text)
    os.replace(tmp, path)


def _save_keystore(st):
    # unique temp name (two concurrent saves once interleaved in one shared
    # .tmp and could publish corrupt JSON) + 0600 from the first byte
    _write_secret_file(KEYSTORE, json.dumps(st, indent=1))


def _cfg_models(cfg):
    """The set of LLM model ids a job config references (dict specs + the
    `llm:model:label` string form)."""
    out = set()
    specs = (cfg.get("bots") or []) + (cfg.get("participants") or [])
    for spec in specs:
        if isinstance(spec, dict) and spec.get("model"):
            out.add(str(spec["model"]))
        elif isinstance(spec, str) and spec.startswith("llm:"):
            parts = spec.split(":")
            if len(parts) >= 2 and parts[1]:
                out.add(parts[1])
    return out


# high-side per-call token assumptions — must match the dash estimator so the
# UI preview and the server-side ceiling agree. Deliberately generous.
_EST_TIN, _EST_TOUT_THINK, _EST_TOUT_FLAT = 4000, 4000, 800


def _cfg_players(cfg):
    """[(model, think_override_or_None), …] for the config's LLM players."""
    out = []
    for spec in (cfg.get("bots") or []) + (cfg.get("participants") or []):
        if isinstance(spec, dict) and spec.get("model"):
            out.append((str(spec["model"]), spec.get("think")))
        elif isinstance(spec, str) and spec.startswith("llm:"):
            p = spec.split(":")
            if len(p) >= 2 and p[1]:
                out.append((p[1], None))
    return out


def _estimate_cost(cfg):
    """High-side $ estimate for a run — the same formula the dash shows, so the
    Server-tab ceiling can be enforced here (a runaway series is the whole
    reason the ceiling exists). Returns dollars (float)."""
    import llm as _llm
    import math
    scen = config_schema.resolve(cfg.get("scenario") or {})
    adm = config_schema.section_resolve("admirals", cfg.get("admirals"))
    win = scen.get("win")
    window = scen.get("window") or 100
    max_ticks = (scen.get("domination_cap") or 18000) if win == "domination" \
        else (scen.get("max_ticks") or 6000)
    mode = cfg.get("mode", "match")
    warmup = adm.get("warmup", True)
    think_default = adm.get("think", True)
    per_game = math.ceil(max_ticks / window) + (1 if warmup else 0)
    if mode != "match":
        ser = config_schema.section_resolve("series", cfg.get("series"))
        if ser.get("memos", True):
            per_game += 1
    prices = _llm.PRICES
    percosts = []
    for model, think_ovr in _cfg_players(cfg):
        pr = prices.get(model)
        if not pr:
            continue
        think = think_default if think_ovr is None else bool(think_ovr)
        tout = _EST_TOUT_THINK if think else _EST_TOUT_FLAT
        percosts.append(per_game * (_EST_TIN * pr[0] + tout * pr[1]) / 1e6)
    if not percosts:
        return 0.0
    if mode == "tournament":
        t = config_schema.section_resolve("tournament", cfg.get("tournament"))
        n = len(cfg.get("participants") or [])
        ppm = int(t.get("players_per_match", 2)) or 2
        gpm = int(t.get("games_per_match", 1)) or 1
        fmt = t.get("format", "round_robin")
        if fmt == "round_robin":
            matchups = math.comb(n, ppm) if n >= ppm else 0
        elif fmt == "single_elim":
            matchups = max(1, math.ceil((n - 1) / max(1, ppm - 1)))
        else:
            matchups = int(t.get("rounds", 1)) * max(1, n // ppm)
        avg = sum(percosts) / len(percosts)
        cost = avg * ppm * matchups * gpm
    elif mode == "series":
        ser = config_schema.section_resolve("series", cfg.get("series"))
        cost = sum(percosts) * int(ser.get("games", 3))
    else:
        cost = sum(percosts)
    return math.ceil(cost * 100) / 100


def _providers_json(models=None):
    """Runner-facing config: REAL keys included (builtin key from env).
    Rides FLOTILLA_PROVIDERS into local runners and the aux config channel.

    `models` scopes which third-party keys ship: a DISPOSABLE worker gets
    only the providers that serve ITS job's models (least privilege — a
    seized droplet then yields at most the keys that job used, not the whole
    ladder). The builtin DO key always ships (it serves everything). The
    local runner passes None (same trust boundary as the flagship → full
    ladder), and an empty/unknown `models` also ships all so a resume with a
    lost config never loses a fallback rung."""
    st = _keystore()
    out = []
    for p in st["providers"]:
        q = dict(p)
        if q.get("builtin"):
            q["key"] = os.environ.get("DO_INFERENCE_KEY", "")
        elif models:
            served = set(q.get("model_map") or {}) | set(q.get("models") or [])
            if not (served & models):
                continue
        out.append(q)
    return json.dumps({"providers": out, "fallback": st["fallback"]})


def _mask_key(k):
    return ("…" + k[-4:]) if k and len(k) > 7 else ("(set)" if k else "")


def _providers_op(d):
    """The /api/providers-op read-modify-write, serialized under KS_LOCK
    (concurrent ops on the shared snapshot silently lost each other's edits).
    Returns (http_code, payload); add returns the SERVER's derived id so the
    client never re-derives it (a non-ASCII label once made the two disagree
    and the follow-up check 404ed)."""
    with KS_LOCK:
        st = _keystore()
        op = d.get("op")
        newid = None
        if op == "add":
            label = str(d.get("label", "")).strip()[:40]
            base = str(d.get("base_url", "")).strip().rstrip("/")
            key = str(d.get("key", "")).strip()
            if not (label and base.startswith("https://") and key):
                return 400, {"error": "need label, an https:// base_url, "
                             "and a key"}
            pid = re.sub(r"[^a-z0-9-]", "-", label.lower())[:24]
            if any(p["id"] == pid for p in st["providers"]):
                return 400, {"error": f"id '{pid}' exists"}
            mm = d.get("model_map")
            st["providers"].append(dict(
                id=pid, label=label, base_url=base, key=key,
                enabled=True, order=len(st["providers"]),
                model_map=mm if isinstance(mm, dict) else {},
                models=[]))
            newid = pid
        elif op == "remove":
            p = next((x for x in st["providers"]
                      if x["id"] == d.get("id")), None)
            if not p:
                return 404, {"error": "no such provider"}
            if p.get("builtin"):
                return 400, {"error": "the primary is built in — disable it "
                             "via the environment instead"}
            st["providers"].remove(p)
        elif op == "toggle":
            p = next((x for x in st["providers"]
                      if x["id"] == d.get("id")), None)
            if not p:
                return 404, {"error": "no such provider"}
            p["enabled"] = bool(d.get("enabled"))
        elif op == "move":
            ids = [p["id"] for p in st["providers"]]
            if d.get("id") not in ids:
                return 404, {"error": "no such provider"}
            i2 = ids.index(d["id"])
            j2 = i2 + (1 if d.get("dir") == 1 else -1)
            if 0 <= j2 < len(st["providers"]):
                st["providers"][i2], st["providers"][j2] = \
                    st["providers"][j2], st["providers"][i2]
        elif op == "fallback":
            fb = d.get("fallback") or {}
            cur = st["fallback"]
            for k2, lo, hi in (("timeout_streak", 1, 20),
                               ("timeout_streak_pipelined", 1, 40),
                               ("error_streak", 1, 20),
                               ("canary_minutes", 1, 240)):
                if k2 in fb:
                    try:
                        cur[k2] = max(lo, min(hi, int(fb[k2])))
                    except (TypeError, ValueError):
                        pass
        elif op == "limits":
            lim = d.get("limits") or {}
            if "max_series_cost" in lim:
                try:
                    st.setdefault("limits", {})["max_series_cost"] = max(
                        0.0, float(lim["max_series_cost"]))
                except (TypeError, ValueError):
                    pass
        else:
            return 400, {"error": f"unknown op {op!r}"}
        for i2, p in enumerate(st["providers"]):
            p["order"] = i2
        _save_keystore(st)
        return 200, ({"ok": True, "id": newid} if newid else {"ok": True})


# ---- provider model discovery + automatic id mapping ------------------------
# The primary's admiral ids (DO flavor) map onto other providers' ids by
# normalized name — "kimi-k3" == "moonshotai/Kimi-K3" == "Kimi K3". Vendors
# prefix differently; strip the path and the known vendor tokens, compare
# what's left.
_VENDOR_TOKENS = ("openai", "anthropic", "alibaba", "moonshotai", "zaiorg",
                  "zai", "nvidia", "deepseekai", "deepseek", "google",
                  "metallama", "meta", "mistralai", "qwen")


def _norm_variants(mid):
    tail = str(mid).split("/")[-1].lower()
    flat = re.sub(r"[^a-z0-9]", "", tail)
    out = {flat}
    for v in _VENDOR_TOKENS:
        if flat.startswith(v) and len(flat) > len(v):
            out.add(flat[len(v):])
    return out


def _known_model_ids():
    """The admiral-facing model ids (the primary's catalog is the namespace)."""
    try:
        sys.path.insert(0, os.path.join(HERE, "sim"))
        from llm import PRICES
        return list(PRICES)
    except Exception:
        return []


def _auto_map(provider_models):
    by_norm = {}
    for pm in provider_models:
        for n in _norm_variants(pm):
            by_norm.setdefault(n, pm)
    mapping = {}
    for did in _known_model_ids():
        for n in _norm_variants(did):
            if n in by_norm:
                mapping[did] = by_norm[n]
                break
    return mapping


def _guard_provider_url(base_url):
    """A provider base_url is operator-supplied but the server fetches it with
    a key attached — refuse anything but https to a PUBLIC host so it can't be
    turned into an SSRF probe of the droplet's own metadata/VPC (169.254.*,
    loopback, RFC1918). Returns the parsed host."""
    import ipaddress
    import socket
    u = urllib.parse.urlparse(base_url)
    if u.scheme != "https" or not u.hostname:
        raise ValueError("provider base_url must be https:// with a host")
    host = u.hostname
    try:
        infos = socket.getaddrinfo(host, u.port or 443, proto=socket.IPPROTO_TCP)
    except socket.gaierror as e:
        raise ValueError(f"cannot resolve provider host: {e}")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
            raise ValueError("provider host resolves to a non-public address")
    return host


def _list_models(base_url, key):
    """Provider-aware model listing. Most inference APIs are OpenAI-compatible
    (GET /models, Bearer): Baseten, Fireworks, Together, Groq, OpenRouter,
    DeepInfra, Mistral, DigitalOcean. Anthropic and Google differ. The flavour
    is chosen by the parsed HOSTNAME, never a substring anywhere in the URL —
    `https://evil/?x=api.anthropic.com` must not coax the key into a header
    (or, for Google, the query string) aimed at an attacker's host."""
    import urllib.request as _ur
    host = _guard_provider_url(base_url)
    base = base_url.rstrip("/")
    if host == "api.anthropic.com":
        rq = _ur.Request(base + "/models" if base.endswith("/v1")
                         else base + "/v1/models",
                         headers={"x-api-key": key,
                                  "anthropic-version": "2023-06-01"})
        with _ur.urlopen(rq, timeout=20) as r:
            data = json.load(r)
        return sorted(str(m.get("id")) for m in (data.get("data") or [])
                      if m.get("id"))
    if host == "generativelanguage.googleapis.com":
        # key in the header, not the query string (query strings land in
        # intermediary + provider access logs)
        rq = _ur.Request(base + "/models",
                         headers={"x-goog-api-key": key})
        with _ur.urlopen(rq, timeout=20) as r:
            data = json.load(r)
        return sorted(str(m.get("name", "")).split("/")[-1]
                      for m in (data.get("models") or []) if m.get("name"))
    rq = _ur.Request(base + "/models",
                     headers={"Authorization": f"Bearer {key}"})
    with _ur.urlopen(rq, timeout=20) as r:
        data = json.load(r)
    return sorted(str(m.get("id")) for m in (data.get("data") or [])
                  if m.get("id"))


RUN_QUEUE = threading.Semaphore(int(os.environ.get("FLOTILLA_CONCURRENT_RUNS", "1")))


def _san(name):
    # strip to a safe basename AND neutralize the traversal forms: leading
    # dots (so ".."/"." can never survive). Returns "" when nothing safe is
    # left — callers that build paths MUST supply a fallback (a run named ".."
    # otherwise rmtree'd the whole library via os.path.join(LIB, "..")).
    n = re.sub(r"[^a-zA-Z0-9 _.-]", "", str(name)).strip().replace(" ", "-")
    return n.lstrip(".")[:60]


def _bearer_ok(sent, expected):
    """Constant-time bearer check (free hardening against timing recovery)."""
    return hmac.compare_digest(str(sent or ""), str(expected or ""))


def _aux_post_bearer_ok(path, token):
    """Resolve a POST /api/aux/<jid>/... path to its job's bearer and check
    it — used to authenticate BEFORE the request body is buffered (the aux
    lane is the one unauthenticated Caddy route, so an anon client could
    otherwise OOM a small droplet with concurrent max-size posts)."""
    m = re.match(r"^/api/aux/([A-Za-z0-9_.-]+)/(?:live|game|done|fail|paused)$",
                 path)
    if not m:
        return False
    with AUX_LOCK:
        rec = AUX.get(m.group(1))
    return bool(rec) and _bearer_ok(token, rec.get("bearer"))


def _safe_path(base, *parts):
    """Join under `base` and REFUSE anything that escapes it — the file
    routes' [^/]+ matched '..' and normpath's startswith('..') missed
    absolute components, together yielding an arbitrary-file read of the
    flagship's own 0600 secrets."""
    for p in parts:
        if not p or p in (".", "..") or p.startswith("/") or "\\" in p \
                or "/" in p.strip("/") and any(
                    seg in ("", ".", "..") for seg in p.split("/")):
            raise ValueError("unsafe path component")
    full = os.path.realpath(os.path.join(base, *parts))
    base = os.path.realpath(base)
    if full != base and not full.startswith(base + os.sep):
        raise ValueError("path escapes base")
    return full


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
        keep = _series_meta_keep(dst)
        # MERGE, never replace: a continuation's work dir holds only the NEW
        # games while its series.json lists the inherited ones too — an rmtree
        # here permanently destroyed the earlier games' replays
        os.makedirs(dst, exist_ok=True)
        for fn in os.listdir(outdir):
            os.replace(os.path.join(outdir, fn), os.path.join(dst, fn))
        shutil.rmtree(outdir, ignore_errors=True)
        if keep:
            try:
                _update_series_json(dst, lambda cur: {**keep, **cur})
            except Exception:
                pass
    else:
        dst = os.path.join(LIB, "tournaments", name)
        shutil.rmtree(dst, ignore_errors=True)
        shutil.move(outdir, dst)


def _series_meta_keep(dst):
    """Fields that must SURVIVE series.json rewrites (renames + start stamp).
    Every writer rebuilds the dict; without this, a rename or the start time
    vanished the next time a game landed."""
    try:
        with open(os.path.join(dst, "series.json")) as fh:
            old = json.load(fh)
        return _meta_of(old)
    except Exception:
        return {}


def _meta_of(ser):
    return {k: ser[k] for k in ("display_name", "started", "archived")
            if k in ser}


def _update_series_json(dst, mutate):
    """Serialized, atomic read-modify-write for a series.json. It has FOUR
    concurrent writer families (partial publisher, aux game callback, done
    callback, archive/rename API) — the bare load→mutate→write pattern lost
    whichever update ran second, and a truncated in-place write could hand
    build_index a half-file. mutate(cur) returns the dict to store."""
    with SJ_LOCK:
        path = os.path.join(dst, "series.json")
        try:
            with open(path) as fh:
                cur = json.load(fh)
        except Exception:
            cur = {}
        out = mutate(cur)
        tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
        with open(tmp, "w") as fh:
            json.dump(out, fh, indent=1)
        os.replace(tmp, path)


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
                dp = os.path.join(dst, fn)
                if _same_file(src, dp):
                    continue   # already mirrored: every game used to re-copy
                tmp = dp + ".tmp"        # + re-gzip + re-PUT on EVERY new game
                shutil.copy2(src, tmp)
                os.replace(tmp, dp)                        # atomic vs readers
                _showcase_put_file(job, fn, dp)
        grows = [dict(game=i + 1, seed=r.get("seed"),
                      file=os.path.basename(r["file"]),
                      winner=r.get("winner")) for i, r in enumerate(rows)]
        _update_series_json(dst, lambda cur: {
            **_meta_of(cur), "games": grows, "memos": {}, "partial": True})
        build_index(LIB)
    except Exception:
        pass


def _same_file(src, dst):
    """copy2 preserves (size, mtime) — equality means the mirror is current."""
    try:
        a, b = os.stat(src), os.stat(dst)
        return a.st_size == b.st_size and a.st_mtime == b.st_mtime
    except OSError:
        return False


def _publish_partial_tournament(job, outdir):
    """Local-executor tournaments: mirror finished games + the incremental
    bracket into the library (and the public prefix) as they land."""
    try:
        dst = os.path.join(LIB, "tournaments", job["name"])
        os.makedirs(dst, exist_ok=True)
        for root, _dirs, files in os.walk(outdir):
            for fn in files:
                if not ((fn.startswith("g") and fn.endswith(".json"))
                        or fn == "tournament.json"):
                    continue
                rel = os.path.relpath(os.path.join(root, fn), outdir)
                if rel.startswith(".."):
                    continue
                dp = os.path.join(dst, rel)
                os.makedirs(os.path.dirname(dp), exist_ok=True)
                sp = os.path.join(root, fn)
                if _same_file(sp, dp):
                    continue
                tmp = dp + ".tmp"
                shutil.copy2(sp, tmp)
                os.replace(tmp, dp)
                _showcase_put_file(job, rel, dp)
        _show_build_hub(job) if _showcase_auto(job) else None
        build_index(LIB)
    except Exception:
        pass


def _mark_cancelled(job, error=None):
    """A cancelled/failed run's COMPLETED games stay in the library — the
    series/tournament file just says so instead of pretending it's still
    live. (champions-cup-1 sat '⏳ live' on the Tournaments tab for hours
    after its worker died — the fail paths never finalized the file.)"""
    try:
        paths = {"series": os.path.join(LIB, "series", job["name"], "series.json"),
                 "tournament": os.path.join(LIB, "tournaments", job["name"],
                                            "tournament.json")}
        p = paths.get(job["mode"])
        if not p or not os.path.isfile(p):
            return
        with open(p) as fh:
            d = json.load(fh)
        d.pop("partial", None)
        d["cancelled"] = True
        if error:
            d["error"] = str(error)[:200]
        d["games_completed"] = len(d.get("games", d.get("matchups", [])))
        with open(p, "w") as fh:
            json.dump(d, fh, indent=1)
        build_index(LIB)
    except Exception:
        pass


def _heal_stale_tournaments():
    """Boot sweep: a tournament file claiming to be live with NO live job
    behind it gets finalized — restarts and old failures otherwise leave
    permanent '⏳ live' ghosts."""
    tdir = os.path.join(LIB, "tournaments")
    if not os.path.isdir(tdir):
        return
    with JOBS_LOCK:
        alive = {j["name"] for j in JOBS
                 if j["mode"] == "tournament"
                 and j["state"] in ("queued", "running", "paused")}
    for name in os.listdir(tdir):
        tp = os.path.join(tdir, name, "tournament.json")
        if name in alive or not os.path.isfile(tp):
            continue
        try:
            with open(tp) as fh:
                d = json.load(fh)
        except Exception:
            continue
        if d.get("partial"):
            _mark_cancelled({"mode": "tournament", "name": name},
                            error="run ended without finishing the bracket")


def _run_job(job, cfg, resume=False):
    with RUN_QUEUE:
        if job.get("cancel"):                     # cancelled while queued
            job["state"] = "cancelled"
            job["finished"] = time.time()
            _persist_jobs()
            return
        job["state"] = "running"
        job["started"] = job.get("started") or time.time()
        _persist_jobs()
        outdir = os.path.join(LIB, "_work", job["id"])
        os.makedirs(outdir, exist_ok=True)
        if resume:
            argv = [sys.executable, os.path.join(HERE, "sim", "run_config.py"),
                    "--resume", outdir]
        else:
            cfg["outdir"] = outdir
            cfgpath = os.path.join(outdir, "run-config.json")
            with open(cfgpath, "w") as fh:
                json.dump(cfg, fh, indent=1)
            argv = [sys.executable, os.path.join(HERE, "sim", "run_config.py"),
                    cfgpath]
        try:
            p = subprocess.Popen(argv,
                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                 text=True, cwd=HERE,
                                 env={**os.environ,
                                      "FLOTILLA_PROVIDERS": _providers_json(),
                                      "FLOTILLA_LIVE": os.path.join(outdir,
                                                                    "live.jsonl")})
            PROCS[job["id"]] = p
            # REGISTER, THEN RE-CHECK: state went "running" above, but PROCS was
            # empty until this line — a /api/cancel landing in that window found
            # no process to terminate and matched no branch in the handler, so
            # the API answered 200 while the runner sailed on (and kept spending
            # inference money) until it finished on its own. Closing the window
            # here keeps ONE settle path: p.wait() below sees job["cancel"] and
            # settles the state exactly once.
            if job.get("cancel"):
                try:
                    p.terminate()
                except Exception:
                    pass
            for line in p.stdout:
                job["log"].append(line.rstrip()[:400])
                if '"winner"' in line:
                    job["games_done"] += 1
                    if job["mode"] == "series":
                        _publish_partial_series(job, outdir)
                    elif job["mode"] == "tournament":
                        _publish_partial_tournament(job, outdir)
                _persist_jobs()
            rc = p.wait()
            if job.get("cancel"):
                job["state"] = "cancelled"
                _mark_cancelled(job)
            elif rc == 75:                 # the runner froze itself: paused
                job["state"] = "paused"
                job["log"].append("⏸ paused — checkpoint on disk; resume any time")
            elif rc != 0:
                raise RuntimeError(f"runner exited {rc}: {job['log'][-1] if job['log'] else ''}")
            else:
                _normalize_results(job, outdir)
                build_index(LIB)
                job["state"] = "done"
                _showcase_job_done(job)
        except Exception as e:
            job["state"] = "cancelled" if job.get("cancel") else "failed"
            if job["state"] == "failed":
                job["error"] = str(e)[:300]
            _mark_cancelled(job)
            _showcase_job_done(job)
        finally:
            PROCS.pop(job["id"], None)
            wd = os.path.join(LIB, "_work", job["id"])
            has_ck = any(os.path.isfile(os.path.join(wd, n))
                         for n in ("checkpoint.json", "checkpoint.json.gz"))
            if job["state"] == "paused":   # the checkpoint LIVES in _work
                _persist_jobs()
            elif job["state"] == "failed" and has_ck:
                # a failed RUN must never destroy a checkpoint (a version-
                # stale one cost domination-5 its game 5): the run is still
                # frozen on disk, so the honest state is paused-with-error —
                # fix the cause and resume again
                job["state"] = "paused"
                job["log"].append("⚠ runner failed but a checkpoint is on "
                                  "disk — preserved; job back to PAUSED "
                                  f"(error: {job.get('error', '')})")
                _persist_jobs()
            else:
                job["finished"] = time.time()
                shutil.rmtree(wd, ignore_errors=True)
                _persist_jobs()


def submit_run(cfg):
    mode = cfg.get("mode", "match")
    if mode not in ("match", "series", "tournament"):
        raise ValueError(f"mode must be match|series|tournament, got {mode!r}")
    config_schema.resolve(cfg.get("scenario") or {})        # loud validation up front
    bots = cfg.get("participants" if mode == "tournament" else "bots") or []
    if not (2 <= len(bots) <= 8) and mode != "tournament":
        raise ValueError("need 2-8 fleets")
    if mode == "tournament" and len(bots) < 2:
        raise ValueError("need >=2 participants")
    # cost ceiling: refuse a run whose high-side estimate exceeds the
    # operator's Server-tab limit — the real guard against a runaway series
    # (the UI warning is only advisory; this is what actually stops it). 0/off
    # is the default. Bypass with cfg["ack_cost"] for a deliberate override.
    _max = float(_keystore().get("limits", {}).get("max_series_cost", 0) or 0)
    if _max > 0 and not cfg.get("ack_cost"):
        est = _estimate_cost(cfg)
        if est > _max:
            raise ValueError(
                f"estimated cost ${est:.2f} exceeds the ${_max:g} ceiling — "
                f"raise it on the Server tab, or resubmit with ack_cost=true")
    # 48 bits of randomness: the jid is the droplet name AND the /api/live
    # key, so a 16-bit suffix was guessable within ~65k tries for a known
    # submit second
    jid = time.strftime("%Y%m%d-%H%M%S") + f"-{os.urandom(6).hex()}"
    # _san can return "" (e.g. a name of ".." or all-punctuation); never let
    # an empty name reach a path join — fall back to the always-safe jid form
    name = _san(cfg.get("name") or "") or f"{mode}-{jid}"
    exp = 1
    if mode == "series":
        exp = int((cfg.get("series") or {}).get("games", 3))
    elif mode == "tournament":
        t = cfg.get("tournament") or {}
        n = len(bots)
        gpm = int(t.get("games_per_match", 3))
        ppm = 2 if t.get("format") == "single_elim" else int(t.get("players_per_match", 2))
        try:
            import math
            if t.get("format") == "single_elim":
                exp = (n - 1) * gpm
            elif t.get("format") == "random_pairs":
                exp = int(t.get("rounds", 3)) * (n // ppm) * gpm
            else:
                exp = math.comb(n, ppm) * gpm
        except Exception:
            exp = 1
    job = dict(id=jid, name=name, mode=mode, state="queued", games_done=0,
               games_expected=exp, submitted=time.time(), started=None,
               finished=None, error=None, log=[],
               public=bool(cfg.pop("public", False)),
               aux_size=str(cfg.pop("aux_size", "") or "") or None)
    if job["public"] and _showcase_cfg() is None:
        job["public"] = False              # not configured: quietly private
    _showcase_job_start(job)
    # an in-flight run is VISIBLE from launch: stub the library entry so the
    # Series/Tournaments views list it as ⏳ live before any game lands
    # (thinking-era games run long — an invisible first hour reads as a bug)
    try:
        if mode == "series":
            sdir = os.path.join(LIB, "series", name)
            sj = os.path.join(sdir, "series.json")
            fresh = not os.path.exists(sj)
            if not fresh:                  # same-name relaunch SUPERSEDES a
                try:                       # cancelled leftover (take-3 sat
                    with open(sj) as fh:   # invisible behind take-2's ✖)
                        old = json.load(fh)
                    with JOBS_LOCK:
                        live = any(j.get("name") == name and j.get("state")
                                   in ("queued", "running", "paused")
                                   for j in JOBS)
                    # cancelled leftovers AND zero-game stubs from dead runs
                    # both supersede — a stale stub's `started` stamp made a
                    # relaunched series sort hours out of place
                    fresh = bool(old.get("cancelled")) or \
                        (not old.get("games") and not live)
                except Exception:
                    fresh = True
            # a continuation resumes an EXISTING series — never re-stub it
            # (the cancel-supersede path otherwise wipes the games list and
            # resets `started` when reviving a cancelled/failed series)
            if fresh and not cfg.get("continue"):
                os.makedirs(sdir, exist_ok=True)
                # `started` is deliberately re-stamped (the stale-stub fix);
                # the operator's rename + archived flag still carry over
                keepm = {k: v for k, v in _series_meta_keep(sdir).items()
                         if k != "started"}
                _update_series_json(sdir, lambda cur: {
                    **keepm, "games": [], "memos": {}, "partial": True,
                    "started": time.time()})
                build_index(LIB)
            cont = cfg.get("continue") or {}
            if cont.get("memos") and int(cont.get("game", 0)) > 1:
                # a continuation carries the admirals' memos INTO game N —
                # backfill them onto game N-1's stored replay so the library
                # shows the memos the bots actually resume with
                gp = os.path.join(sdir, f"g{int(cont['game']) - 1}.json")
                if os.path.isfile(gp):
                    try:
                        with open(gp) as fh:
                            rp = json.load(fh)
                        rp.setdefault("memos", {})
                        for n, m in cont["memos"].items():
                            rp["memos"][n] = {"memo": m, "err": None}
                        tmp = gp + ".tmp"
                        with open(tmp, "w") as fh:
                            json.dump(rp, fh, separators=(",", ":"))
                        os.replace(tmp, gp)
                    except (ValueError, OSError):
                        pass
        elif mode == "tournament":
            tdir = os.path.join(LIB, "tournaments", name)
            tj = os.path.join(tdir, "tournament.json")
            if not os.path.exists(tj):
                os.makedirs(tdir, exist_ok=True)
                with open(tj, "w") as fh:
                    json.dump({"config": {"tournament": cfg.get("tournament",
                                                                {})},
                               "matchups": [], "standings": {},
                               "partial": True}, fh, indent=1)
                build_index(LIB)
    except Exception:
        pass
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
    "/tournament.html": ("dash/tournament.html", "text/html"),
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


_PUBLIC_STRIP = ("sim_feedback", "memos_final")


def _public_redact(key, data):
    """Public mirrors of series.json / tournament.json drop the end-of-series
    designer interviews (sim_feedback) and the final private memos
    (memos_final). No page renders either, the admirals themselves never see
    them, and a public bucket must not hand a future rival team an admiral's
    own candid read on the meta. The private library keeps both fields."""
    if os.path.basename(key) not in ("series.json", "tournament.json"):
        return data
    try:
        d = json.loads(data)
        if not any(k in d for k in _PUBLIC_STRIP):
            return data
        for k in _PUBLIC_STRIP:
            d.pop(k, None)
        return json.dumps(d, separators=(",", ":")).encode()
    except Exception:
        return data                      # unparseable → mirror as-is


def _s3_put_public(cfg, key, data, content_type="text/html"):
    """Minimal SigV4 S3 PUT with public-read ACL — stdlib only, no boto.
    Text payloads over 2KB upload gzipped with Content-Encoding: gzip —
    browsers decompress transparently and a 14MB replay ships as ~2MB.
    series.json / tournament.json pass through _public_redact on the way."""
    data = _public_redact(key, data)
    import datetime as _dt
    import gzip as _gz
    import hmac
    import urllib.request as _rq
    host = f"{cfg['bucket']}.{cfg['endpoint']}"
    now = _dt.datetime.now(_dt.timezone.utc)
    amzdate = now.strftime("%Y%m%dT%H%M%SZ")
    datestamp = now.strftime("%Y%m%d")
    region = cfg.get("region", "nyc3")
    encoding = None
    if len(data) > 2048 and ("json" in content_type or "html" in content_type
                             or content_type.startswith("text/")):
        data = _gz.compress(data, 6)
        encoding = "gzip"
    payload_hash = hashlib.sha256(data).hexdigest()
    uri = "/" + urllib.parse.quote(key)
    headers = {"host": host, "x-amz-acl": "public-read",
               "x-amz-content-sha256": payload_hash, "x-amz-date": amzdate,
               "content-type": content_type}
    if encoding:
        headers["content-encoding"] = encoding
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


# ---------------- saved ship classes (the Configure page's designer) ---------
SHIPS_LOCK = threading.Lock()
SHIP_STATS = tuple(contract.game().ship_stats)   # the game's designer stats


def _ships_path():
    return os.path.join(LIB, "ships.json")


def _load_ships():
    """Operator-saved ship classes. Read FRESH on every call — a hand-edited
    ships.json shows up on the next page load, no restart needed."""
    try:
        with open(_ships_path()) as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _clean_ship(name, st):
    """(name, stats) validated for storage: safe name, every stat an int 1-40.
    Point-total rules are per-run (design_points) — the engine enforces them
    at match start, where the run's own budget is known."""
    name = re.sub(r"[^A-Za-z0-9_-]", "", str(name))[:24]
    if not name or name in contract.game().presets or not isinstance(st, dict):
        return None, None
    clean = {}
    for k in SHIP_STATS:
        try:
            v = int(st.get(k, 0))
        except (TypeError, ValueError):
            return None, None
        if not 1 <= v <= 40:
            return None, None
        clean[k] = v
    return name, clean


def _showcase_list_path():
    return os.path.join(LIB, "showcase-list.json")


SHOWLIST_LOCK = threading.Lock()   # read-modify-write: two aux jobs finishing
                                   # together used to drop one entry


def _showcase_list():
    try:
        with open(_showcase_list_path()) as fh:
            d = json.load(fh)
        return d if isinstance(d, list) else []
    except Exception:
        return []


def _showcase_list_update(mutate):
    """Apply `mutate(list) -> list` under the lock, write atomically."""
    with SHOWLIST_LOCK:
        pub = mutate(_showcase_list())
        tmp = _showcase_list_path() + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(pub, fh, indent=1)
        os.replace(tmp, _showcase_list_path())


def _s3_delete_public(cfg, key):
    """SigV4 S3 DELETE — the inverse of _s3_put_public, for retiring a public
    link. A 404 counts as deleted (the object was already gone)."""
    import datetime as _dt
    import hmac
    import urllib.request as _rq
    host = f"{cfg['bucket']}.{cfg['endpoint']}"
    now = _dt.datetime.now(_dt.timezone.utc)
    amzdate = now.strftime("%Y%m%dT%H%M%SZ")
    datestamp = now.strftime("%Y%m%d")
    region = cfg.get("region", "nyc3")
    payload_hash = hashlib.sha256(b"").hexdigest()
    uri = "/" + urllib.parse.quote(key)
    headers = {"host": host, "x-amz-content-sha256": payload_hash,
               "x-amz-date": amzdate}
    signed = ";".join(sorted(headers))
    canonical = ("DELETE\n" + uri + "\n\n"
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
    req = _rq.Request(f"https://{host}{uri}", method="DELETE",
                      headers={**{k: v for k, v in headers.items()
                                  if k != "host"}, "Authorization": auth})
    try:
        with _rq.urlopen(req, timeout=60) as r:
            return r.status
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return 404
        raise


def _s3_list_public(cfg, prefix):
    """SigV4 S3 ListObjectsV2 — every key under a prefix, paginated. Needed
    because a tournament showcase is a PREFIX of objects (index, player,
    logos, one replay per matchup game, series-index), and retiring the link
    must remove all of them, not just the entry points."""
    import datetime as _dt
    import hmac
    import urllib.request as _rq
    import xml.etree.ElementTree as _ET
    host = f"{cfg['bucket']}.{cfg['endpoint']}"
    region = cfg.get("region", "nyc3")
    payload_hash = hashlib.sha256(b"").hexdigest()
    keys, token = [], None
    while True:
        q = [("list-type", "2"), ("prefix", prefix)]
        if token:
            q.append(("continuation-token", token))
        q.sort()
        qs = "&".join(f"{urllib.parse.quote(k, safe='')}="
                      f"{urllib.parse.quote(v, safe='')}" for k, v in q)
        now = _dt.datetime.now(_dt.timezone.utc)
        amzdate = now.strftime("%Y%m%dT%H%M%SZ")
        datestamp = now.strftime("%Y%m%d")
        headers = {"host": host, "x-amz-content-sha256": payload_hash,
                   "x-amz-date": amzdate}
        signed = ";".join(sorted(headers))
        canonical = ("GET\n/\n" + qs + "\n"
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
        req = _rq.Request(f"https://{host}/?{qs}",
                          headers={**{k: v for k, v in headers.items()
                                      if k != "host"}, "Authorization": auth})
        with _rq.urlopen(req, timeout=60) as r:
            root = _ET.fromstring(r.read())
        ns = root.tag.split("}")[0] + "}" if root.tag.startswith("{") else ""
        keys += [c.findtext(f"{ns}Key") for c in root.iter(f"{ns}Contents")]
        token = root.findtext(f"{ns}NextContinuationToken")
        if root.findtext(f"{ns}IsTruncated") != "true" or not token:
            return keys


def _showcase_purge(ident):
    """Retire a public link COMPLETELY: every bucket object behind it plus its
    registry entry. Single-object links (series/match bundles) are one key; a
    tournament is a whole prefix, and leaving "unlisted" objects behind is a
    slow object-storage leak — a deleted link must stop costing money.
    Returns (deleted_count, failed_keys). On any failure the registry entry is
    KEPT so the leak stays visible and retryable instead of silently orphaned.
    Showcase unconfigured = clean no-op."""
    cfg = _showcase_cfg()
    if cfg is None:
        return 0, []
    try:
        if ident.startswith("tournament-"):
            keys = _s3_list_public(cfg, f"showcase/{ident}/")
        else:
            # pre-ident publishes named the object after the bare series/match
            # name (showcase/<name>.html, no type prefix) — delete BOTH forms
            # or retiring an old link leaves its real object billing forever.
            # _s3_delete_public counts a 404 as deleted, so the miss is free.
            bare = ident.split("-", 1)[1] if "-" in ident else ident
            keys = [f"showcase/{ident}.html", f"showcase/{bare}.html"]
    except Exception as e:
        return 0, [f"list: {type(e).__name__}: {e}"[:120]]
    deleted, failed = 0, []
    for key in keys:
        try:
            _s3_delete_public(cfg, key)
            deleted += 1
        except Exception as e:
            failed.append(f"{key}: {type(e).__name__}"[:120])
    if not failed:
        # drop by ident AND by the deleted keys' URLs — pre-ident registry
        # entries carry no ident, and matching only ident left them dangling
        # (a copy-link button pointing at an object that no longer exists)
        gone = {f"https://{cfg['bucket']}.{cfg['endpoint']}/{k}" for k in keys}
        _showcase_list_update(
            lambda pub: [x for x in pub if x.get("ident") != ident
                         and x.get("url") not in gone])
    return deleted, failed


# ---------------- public show: auto-mirror a PUBLIC job to the bucket ---------
# A job launched with "public": true mirrors itself to the showcase bucket as it
# runs: game replays as they land, a hub page (bracket/standings + links), the
# viewer itself, and — for auxiliary jobs — the live stream as numbered jsonl
# chunks the public player tails without any login. Everything is best-effort:
# a bucket hiccup must never touch the run.

def _show_prefix(job):
    return f"show/{_san(job['name'])}"


def _show_url(job, key=""):
    cfg = _showcase_cfg()
    return f"https://{cfg['bucket']}.{cfg['endpoint']}/{_show_prefix(job)}/{key}"


def _showcase_auto(job):
    return bool(job.get("public")) and _showcase_cfg() is not None


def _showcase_put(job, key, data, ctype):
    try:
        _s3_put_public(_showcase_cfg(), f"{_show_prefix(job)}/{key}", data, ctype)
        return True
    except Exception:
        return False


def _showcase_job_start(job):
    """Seed the public prefix: the player page + an empty hub."""
    if not _showcase_auto(job):
        return
    try:
        with open(os.path.join(HERE, "viewer", "index.html"), "rb") as fh:
            _showcase_put(job, "player.html", fh.read(), "text/html")
        for lp in (os.path.join(LIB, "logos.json"),
                   os.path.join(HERE, "assets", "logos.json")):
            if os.path.isfile(lp):
                _showcase_put(job, "logos.json", open(lp, "rb").read(),
                              "application/json")
                break
        _showcase_put(job, "config-schema.json",
                      (lambda sj: sj.encode() if isinstance(sj, str) else sj)(
                          config_schema.schema_json()), "application/json")
        _show_build_hub(job)
        job["log"].append(f"public showcase: {_show_url(job, 'index.html')}")
        _persist_jobs()
    except Exception:
        pass


def _showcase_put_file(job, rel, path):
    """Mirror one landed artifact (already sanitized, library-side)."""
    if not _showcase_auto(job):
        return
    try:
        with open(path, "rb") as fh:
            _showcase_put(job, rel, fh.read(), "application/json")
    except Exception:
        pass


def _showcase_refresh_tournament(tname, rel=None, blob=None):
    """A REGISTERED tournament link (the 🌐 prefix under showcase/tournament-*)
    auto-updates as games land: put only the DELTA — the replay that just
    arrived plus the two small status files (tournament.json, the
    series-index slice) — never the whole prefix. A 30-game tournament
    re-publishing wholesale on every landing would move hundreds of MB; the
    delta is one replay + a few KB. No registered link = no-op."""
    cfg = _showcase_cfg()
    if cfg is None:
        return
    ident = f"tournament-{tname}"
    if not any(x.get("ident") == ident for x in _showcase_list()):
        return
    prefix = f"showcase/{ident}"
    try:
        if rel and blob and rel != "tournament.json":
            _s3_put_public(cfg, f"{prefix}/{rel}", blob, "application/json")
        tp = os.path.join(LIB, "tournaments", tname, "tournament.json")
        if os.path.isfile(tp):
            with open(tp, "rb") as fh:
                _s3_put_public(cfg, f"{prefix}/tournament.json", fh.read(),
                               "application/json")
        with open(os.path.join(LIB, "index.json")) as fh:
            full = json.load(fh)
        slc = [x for x in full.get("series", [])
               if x.get("tournament") == tname]
        _s3_put_public(cfg, f"{prefix}/series-index.json",
                       json.dumps({"series": slc}).encode(),
                       "application/json")
    except Exception:
        pass                     # refresh is best-effort; publish still syncs


def _showcase_job_game(job, rel, rp, blob=None):
    """Aux path: the replay dict is already in hand. Pass `blob` (the compact
    bytes the caller just wrote to disk) to skip a second multi-MB dumps —
    CPython JSON work holds the GIL and stalls the whole 1-2 vCPU box."""
    if not _showcase_auto(job):
        return
    try:
        if blob is None:
            blob = json.dumps(rp, separators=(",", ":")).encode()
        _showcase_put(job, rel, blob, "application/json")
        _show_build_hub(job)
    except Exception:
        pass


def _showcase_job_live(job, rec, lines):
    """Aux live stream -> numbered public chunks + a tiny cursor object. The
    public player polls state.json (bytes, not megabytes) and fetches only the
    chunks it hasn't seen."""
    if not _showcase_auto(job) or not lines:
        return
    try:
        n = rec.get("show_chunks", 0) + 1
        rec["show_chunks"] = n
        payload = "\n".join(json.dumps(x, separators=(",", ":"))
                             for x in lines) + "\n"
        _showcase_put(job, f"live/{n:05d}.jsonl", payload.encode(),
                      "application/json")
        _showcase_put(job, "live/state.json", json.dumps(
            {"chunks": n, "state": "running",
             "game": job["games_done"] + 1,
             "games_expected": job.get("games_expected")}).encode(),
            "application/json")
    except Exception:
        pass


def _showcase_job_done(job):
    """Final mirror: re-put every artifact from the library (memo-final replay
    versions included), close the live cursor, rebuild the hub, and register the
    hub in the showcase list."""
    if not _showcase_auto(job):
        return
    try:
        roots = {"series": os.path.join(LIB, "series", job["name"]),
                 "tournament": os.path.join(LIB, "tournaments", job["name"])}
        root = roots.get(job["mode"])
        if root and os.path.isdir(root):
            for r, _d, files in os.walk(root):
                for fn in files:
                    if fn.endswith(".json"):
                        rel = os.path.relpath(os.path.join(r, fn), root)
                        _showcase_put_file(job, rel, os.path.join(r, fn))
        elif job["mode"] == "match":
            mf = os.path.join(LIB, "matches", f"{job['name']}.json")
            if os.path.isfile(mf):
                _showcase_put_file(job, f"{job['name']}.json", mf)
        _showcase_put(job, "live/state.json", json.dumps(
            {"chunks": 0, "state": job.get("state", "done"),
             "game": job["games_done"]}).encode(), "application/json")
        _show_build_hub(job)
        url = _show_url(job, "index.html")
        entry = {"name": job["name"], "url": url, "bytes": 0,
                 "when": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        _showcase_list_update(
            lambda pub: [x for x in pub if x.get("url") != url] + [entry])
    except Exception:
        pass


def _esc(s):
    """HTML-escape — the public hub embeds model-controlled names/winners; a
    raw admiral name was stored XSS on an anonymous-viewer page."""
    return (str("" if s is None else s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;").replace("'", "&#39;"))


def _show_build_hub(job):
    """The public hub page: standings/bracket + one link per game, plus a live
    link while the job runs. Self-contained, relative links only."""
    try:
        name, mode = job["name"], job["mode"]
        running = job.get("state") in ("queued", "running")
        rows = []
        title = _esc(name.replace("-", " "))
        if mode == "tournament":
            tj = {}
            tp = os.path.join(LIB, "tournaments", name, "tournament.json")
            if os.path.isfile(tp):
                tj = json.load(open(tp))
            st = tj.get("standings", {})
            if st:
                rows.append("<h2>Standings</h2><table><tr><th>admiral</th>"
                            "<th>wins</th><th>games</th><th>score</th></tr>" +
                            "".join(f"<tr><td>{_esc(n)}</td><td>{_esc(v['wins'])}</td>"
                                    f"<td>{_esc(v['games'])}</td><td>{_esc(v['score'])}</td></tr>"
                                    for n, v in sorted(
                                        st.items(),
                                        key=lambda kv: (-kv[1]["wins"],
                                                        -kv[1]["score"]))) +
                            "</table>")
            if tj.get("champion"):
                rows.insert(0, f"<p class='champ'>🏆 champion: "
                               f"<b>{_esc(tj['champion'])}</b></p>")
            for m in tj.get("matchups", []):
                # m['dir'] is a server-built path component; still url-encode it
                links = " ".join(
                    f"<a href='player.html?replay="
                    f"{urllib.parse.quote(str(m['dir']) + '/' + os.path.basename(g['file']))}'>"
                    f"▶ g{_esc(g['game'])} — {_esc(g['winner'])}</a>"
                    for g in m.get("games", []))
                rows.append(f"<div class='m'>round {_esc(m['round'])} · "
                            f"<b>{' vs '.join(_esc(p) for p in m['players'])}</b> → "
                            f"{_esc(m.get('winner', '?'))}<br>{links}</div>")
        else:
            sp = os.path.join(LIB, "series", name, "series.json")
            games = []
            if os.path.isfile(sp):
                games = json.load(open(sp)).get("games", [])
            for g in games:
                rows.append(f"<div class='m'><a href='player.html?replay="
                            f"{urllib.parse.quote(os.path.basename(g['file']))}'>"
                            f"▶ game {_esc(g['game'])}</a> — winner: "
                            f"{_esc(g.get('winner', '?'))}</div>")
        live = ("<p class='live'>🔴 <a href='player.html?livejsonl=live'>"
                "WATCH LIVE</a> — a game is under way</p>" if running else "")
        html = f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Flotilla — {title}</title><style>
 body {{ background:#0b1220; color:#e6edf7; font:15px system-ui;
        max-width:780px; margin:0 auto; padding:24px; }}
 a {{ color:#4da3ff; }} h1 {{ font-size:22px; }} h2 {{ font-size:16px; }}
 table {{ border-collapse:collapse; }} td,th {{ border:1px solid #1e2a44;
        padding:4px 10px; text-align:left; }}
 .m {{ background:#111a2e; border:1px solid #1e2a44; border-radius:8px;
      padding:8px 12px; margin:8px 0; }}
 .live {{ background:#3a1020; border:1px solid #ff6b6b55; border-radius:8px;
      padding:8px 12px; }} .champ {{ font-size:18px; }}
 .dim {{ color:#8b9bb8; }}</style></head><body>
<h1>⛵ Flotilla — {title}</h1>
<p class="dim">{_esc(mode)} · {"in progress" if running else "complete"} ·
LLM admirals command deterministic fleets — click any game to watch.</p>
{live}
{"".join(rows) if rows else "<p class='dim'>first game is under way — refresh soon</p>"}
</body></html>"""
        _showcase_put(job, "index.html", html.encode(), "text/html")
    except Exception:
        pass



# ---- fleet auxiliaries: disposable worker droplets (docs/FLEET_AUXILIARIES.md) ----
AUX = {}                                # job id -> {"bearer", "droplet_id", "born"}
AUX_LOCK = threading.Lock()
# stagger parallel aux launches so their first-window LLM bursts + droplet
# provisioning don't land at once (default = one window timeout, 8 min). 0=off.
_AUX_STAGGER_S = int(os.environ.get("FLOTILLA_AUX_STAGGER_S", "480"))
_AUX_STAGGER_LOCK = threading.Lock()
_aux_last_start = [0.0]                  # mutable cell for the last launch time


def _aux_stagger_gate(job):
    """Block until at least _AUX_STAGGER_S has passed since the previous aux
    launch, so N parallel series don't all start game 1 (and hammer inference)
    on the same tick. Cheap: the wait is per-LAUNCH, not per-window."""
    if _AUX_STAGGER_S <= 0:
        return
    with _AUX_STAGGER_LOCK:
        wait = _aux_last_start[0] + _AUX_STAGGER_S - time.time()
        if wait > 0:
            job["log"].append(f"staggering aux start by {int(wait)}s "
                              f"(parallel-launch spacing)")
            _persist_jobs()
            time.sleep(wait)
        _aux_last_start[0] = time.time()


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
    # workers fetch job.json (inference key + the provider keys serving THIS
    # job's models — see _providers_json scoping) + the app
    # over callback_base — an http:// base ships them cleartext to every
    # booting droplet. Enforce TLS on BOTH config paths (the /api/aux-config
    # handler also checks; the env path used to slip through).
    if not str(env["callback_base"]).lower().startswith("https://"):
        return None
    env.setdefault("size", "s-1vcpu-1gb")
    env.setdefault("region", "nyc3")
    env.setdefault("max_concurrent", 3)
    env.setdefault("max_age_h", 8)
    # tournaments have NO checkpoint path, so the pause-rotation cap cannot
    # apply to them — they run to completion under this hard runaway ceiling
    # instead (champions-cup-1 died at 8h to the rotation it couldn't answer)
    env.setdefault("tournament_max_age_h", 72)
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
    # base is operator-supplied — bind every interpolated value to a
    # shlex.quote'd shell var so a callback_base carrying " or $(...) can't
    # inject into the droplet's boot script (jid/bearer are charset-
    # constrained, quoted anyway)
    return f"""#!/bin/bash
BASE={shlex.quote(base)}
TOK={shlex.quote(f"X-Aux-Token: {bearer}")}
JID={shlex.quote(jid)}
mkdir -p /opt/flotilla /etc/flotilla-aux
for i in $(seq 1 60); do
  curl -fsS -H "$TOK" "$BASE/api/aux/$JID/app.tar.gz" -o /tmp/app.tgz && break
  sleep 5
done
tar xzf /tmp/app.tgz -C /opt/flotilla
curl -fsS -H "$TOK" "$BASE/api/aux/$JID/job.json" -o /etc/flotilla-aux/job.json
chmod 600 /etc/flotilla-aux/job.json
nohup python3 /opt/flotilla/scripts/aux_agent.py >/var/log/flotilla-aux.log 2>&1 &
"""


_APP_TAR = None


def _app_tarball():
    """The running app, packed for a worker: code only, never the library.
    Cached for the process lifetime — the code cannot change under a running
    server (deploys restart it), and the worker bootstrap retries this URL
    up to 60x while each rebuild cost ~50ms of gzip."""
    global _APP_TAR
    if _APP_TAR is not None:
        return _APP_TAR
    import io
    import tarfile
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        # every top-level import root ships, or the worker crash-loops on a
        # ModuleNotFoundError the flagship never sees (engine/ was missing
        # here for three resume attempts after the split — the deploy tarball
        # and THIS one are separate manifests, each needing the smoke test)
        for item in ("server.py", "VERSION", "keelspring", "sim", "scripts",
                     "viewer", "dash"):
            p = os.path.join(HERE, item)
            if os.path.exists(p):
                tar.add(p, arcname=item, filter=lambda ti:
                        None if "__pycache__" in ti.name else ti)
    _APP_TAR = buf.getvalue()
    return _APP_TAR


def _aux_state_path():
    return os.path.join(LIB, "aux-state.json")


def _persist_aux():
    """Bearers + droplet ids survive a flagship restart (0600 — they are
    credentials). Without this, a server bounce stranded every running aux job:
    callbacks 401'd against an empty AUX map and the reaper killed the workers."""
    try:
        with AUX_LOCK:
            a = {k: dict(v) for k, v in AUX.items()}
        with POOL_LOCK:
            p2 = {k: dict(v) for k, v in POOL.items()}
        _write_secret_file(_aux_state_path(),
                           json.dumps({"aux": a, "pool": p2}))
    except Exception:
        pass


def _restore_state():
    """Startup: reload jobs + aux/pool state. Running AUX jobs get their
    watcher back (the worker never noticed the restart); running LOCAL jobs
    died with our subprocess — mark them failed, loudly."""
    try:
        with open(os.path.join(LIB, "jobs.json")) as fh:
            rows = json.load(fh)
        if isinstance(rows, list):
            with JOBS_LOCK:
                JOBS.extend(rows)
    except Exception:
        pass
    try:
        with open(_aux_state_path()) as fh:
            st = json.load(fh)
        with AUX_LOCK:
            AUX.update(st.get("aux", {}))
        with POOL_LOCK:
            POOL.update(st.get("pool", {}))
    except Exception:
        pass
    aux = _aux_cfg()
    with JOBS_LOCK:
        live = [j for j in JOBS if j.get("state") in ("queued", "running")]
    for j in live:
        with AUX_LOCK:
            has_rec = j["id"] in AUX
        if j.get("aux") and aux is not None and has_rec:
            j.setdefault("log", []).append(
                "flagship restarted — auxiliary unaffected, callbacks resume")
            threading.Thread(target=_aux_watch, args=(j, aux),
                             daemon=True).start()
            # visibility backfill: a restored in-flight series/tournament gets
            # its ⏳ stub if it predates stub-on-submit (or the stub was lost)
            try:
                if j["mode"] == "series":
                    sj = os.path.join(LIB, "series", j["name"], "series.json")
                    stale = not os.path.exists(sj)
                    if not stale:
                        try:
                            with open(sj) as fh:
                                stale = bool(json.load(fh).get("cancelled"))
                        except Exception:
                            stale = True
                    if stale:
                        os.makedirs(os.path.dirname(sj), exist_ok=True)
                        with open(sj, "w") as fh:
                            json.dump({"games": [], "memos": {},
                                       "partial": True,
                                       "started": j.get("started")
                                       or time.time()}, fh, indent=1)
                elif j["mode"] == "tournament":
                    tj = os.path.join(LIB, "tournaments", j["name"],
                                      "tournament.json")
                    if not os.path.exists(tj):
                        os.makedirs(os.path.dirname(tj), exist_ok=True)
                        with open(tj, "w") as fh:
                            json.dump({"config": {}, "matchups": [],
                                       "standings": {}, "partial": True},
                                      fh, indent=1)
                build_index(LIB)
            except Exception:
                pass
        else:
            j["state"] = "failed"
            j["error"] = "flagship restarted mid-run (local executor)"
            j["finished"] = time.time()
    _persist_jobs()


def _aux_destroy(jid):
    cfg = _aux_cfg()
    with AUX_LOCK:
        rec = AUX.pop(jid, None)
    if rec:
        with POOL_LOCK:                 # a warm pooler that ran this job dies with it
            for pid in [k for k, v in POOL.items()
                        if v.get("droplet_id") == rec.get("droplet_id")]:
                POOL.pop(pid, None)
    if cfg and rec and rec.get("droplet_id"):
        try:
            _do(cfg, "DELETE", f"/droplets/{rec['droplet_id']}")
        except Exception:
            pass                        # the reaper sweeps stragglers by tag
    _persist_aux()


# ---------------- warm pool (aux v2): pre-provisioned idle workers ------------
# OFF by default. aux-config: {"warm_pool": N, "warm_size": "s-1vcpu-2gb"}.
# Poolers boot with a POOL bearer and poll /api/aux/pool/<pid>/job until a job
# is assigned; the response is the same job.json a cold worker fetches, so the
# agent bootstrap is identical from there. Claiming a pooler skips droplet
# provisioning entirely (~60-90s saved per job).
POOL = {}                               # pool id -> {bearer, droplet_id, size, born, job}
POOL_LOCK = threading.Lock()


def _pool_user_data(pid, bearer, cfg):
    base = cfg["callback_base"].rstrip("/")
    # bind operator-supplied base to a shlex.quote'd shell var (see
    # _aux_user_data); the $JID app URL is assembled on the droplet from
    # job.json, so it uses the same $BASE var
    return f"""#!/bin/bash
BASE={shlex.quote(base)}
TOK={shlex.quote(f"X-Aux-Token: {bearer}")}
PID={shlex.quote(pid)}
mkdir -p /opt/flotilla /etc/flotilla-aux
while true; do
  code=$(curl -s -H "$TOK" \\
    -o /etc/flotilla-aux/job.json -w "%{{http_code}}" \\
    "$BASE/api/aux/pool/$PID/job")
  [ "$code" = "200" ] && break
  sleep 8
done
chmod 600 /etc/flotilla-aux/job.json
JB=$(python3 -c "import json;print(json.load(open('/etc/flotilla-aux/job.json'))['bearer'])")
JID=$(python3 -c "import json;print(json.load(open('/etc/flotilla-aux/job.json'))['job_id'])")
for i in $(seq 1 60); do
  curl -fsS -H "X-Aux-Token: $JB" \\
    "$BASE/api/aux/$JID/app.tar.gz" -o /tmp/app.tgz && break
  sleep 5
done
tar xzf /tmp/app.tgz -C /opt/flotilla
nohup python3 /opt/flotilla/scripts/aux_agent.py >/var/log/flotilla-aux.log 2>&1 &
"""


def _pool_claim(size):
    """Take an idle pooler of this size; returns (pid, rec) or (None, None)."""
    with POOL_LOCK:
        for pid, rec in POOL.items():
            if rec.get("job") is None and rec.get("size") == size \
                    and rec.get("droplet_id"):
                rec["job"] = "claiming"
                return pid, rec
    return None, None


def _pool_tick():
    """One maintenance pass: top the pool up to warm_pool idle workers of
    warm_size, retire mis-sized or stale idles. Called by the daemon loop and
    directly by tests."""
    cfg = _aux_cfg()
    if cfg is None:
        return
    want = int(cfg.get("warm_pool", 0) or 0)
    size = cfg.get("warm_size") or cfg.get("size", "s-1vcpu-2gb")
    max_idle_s = float(cfg.get("max_age_h", 8)) * 1800    # idle half the job cap
    with POOL_LOCK:
        idle = [(pid, r) for pid, r in POOL.items() if r.get("job") is None]
    for pid, r in idle:                 # retire wrong-size or stale idles
        if r.get("size") != size or time.time() - r["born"] > max_idle_s:
            with POOL_LOCK:
                POOL.pop(pid, None)
            if r.get("droplet_id"):
                try:
                    _do(cfg, "DELETE", f"/droplets/{r['droplet_id']}")
                except Exception:
                    pass
    with POOL_LOCK:
        have = sum(1 for r in POOL.values()
                   if r.get("job") is None and r.get("size") == size)
    for _ in range(max(0, want - have)):
        import secrets as _sec
        pid = "pool-" + _sec.token_hex(4)
        bearer = "kpool_" + _sec.token_urlsafe(24)
        with POOL_LOCK:
            POOL[pid] = {"bearer": bearer, "droplet_id": None, "size": size,
                         "born": time.time(), "job": None}
        try:
            d = _do(cfg, "POST", "/droplets", {
                "name": f"flotilla-aux-{pid}", "region": cfg["region"],
                "size": size, "image": "debian-13-x64",
                "ssh_keys": [],   # no interactive login on a disposable worker
                "tags": ["flotilla-aux"],
                "user_data": _pool_user_data(pid, bearer, cfg)})
            with POOL_LOCK:
                if pid in POOL:
                    POOL[pid]["droplet_id"] = d["droplet"]["id"]
            _persist_aux()
        except Exception:
            with POOL_LOCK:
                POOL.pop(pid, None)
            break                       # provider trouble: try again next pass


def _pool_maintain():
    while True:
        time.sleep(45)
        try:
            _pool_tick()
        except Exception:
            pass


def _run_job_aux(job, cfg):
    aux = _aux_cfg()
    jid = job["id"]
    import secrets as _sec
    bearer = "kaux_" + _sec.token_urlsafe(24)
    # a game-boundary continuation inherits the already-played rows so
    # series.json rebuilds with correct game numbers as new games land
    prior = list((cfg.get("continue") or {}).get("rows") or [])
    with AUX_LOCK:
        AUX[jid] = {"bearer": bearer, "droplet_id": None, "born": time.time(),
                    "config": cfg, "rows": prior}
    if prior:
        job["games_done"] = len(prior)
    job["state"] = "running"
    job["started"] = time.time()
    job["aux"] = True
    _persist_jobs()
    _aux_stagger_gate(job)             # space parallel launches ≥8 min apart
    size = job.get("aux_size") or aux.get("size", "s-1vcpu-2gb")
    pid, pooler = _pool_claim(size)
    if pooler is not None:              # a warm worker is idling: hand it the job
        with AUX_LOCK:
            AUX[jid]["droplet_id"] = pooler["droplet_id"]
        with POOL_LOCK:
            pooler["job"] = jid
        job["log"].append(f"warm auxiliary {pooler['droplet_id']} assigned "
                          f"(pool {pid})")
        _persist_jobs()
        _persist_aux()
    else:
        # droplet-create retries with backoff: a transient DO API outage used
        # to kill the job at the starting line (it killed territory-5 on
        # 2026-08-08) — a launch is worth ~8 minutes of patience
        for attempt in range(4):
            try:
                if attempt:
                    # the FAILED attempt may still have created a droplet
                    # server-side (a create that succeeded but whose response
                    # was lost — the exact DO-brownout symptom this retry
                    # exists for). An unswept orphan boots with the same
                    # bearer and double-runs the job: sweep by name first.
                    try:
                        ds = _do(aux, "GET",
                                 "/droplets?tag_name=flotilla-aux&per_page=100")
                        for orp in ds.get("droplets", []):
                            if orp.get("name") == f"flotilla-aux-{jid}":
                                _do(aux, "DELETE", f"/droplets/{orp['id']}")
                                job["log"].append(
                                    f"swept orphan droplet {orp['id']} from "
                                    "the failed create before retrying")
                    except Exception:
                        pass
                d = _do(aux, "POST", "/droplets", {
                    "name": f"flotilla-aux-{jid}", "region": aux["region"],
                    "size": size, "image": "debian-13-x64",
                    "ssh_keys": [],   # no interactive login on a disposable worker
                    "tags": ["flotilla-aux"],
                    "user_data": _aux_user_data(jid, bearer, aux)})
                with AUX_LOCK:
                    rec = AUX.get(jid)
                    if rec is None:
                        cancelled_mid = True     # cancel ran during the create
                    else:
                        cancelled_mid = False
                        rec["droplet_id"] = d["droplet"]["id"]
                if cancelled_mid:
                    # /api/cancel already destroyed-and-released this job —
                    # the droplet we just created must not outlive it
                    try:
                        _do(aux, "DELETE", f"/droplets/{d['droplet']['id']}")
                    except Exception:
                        pass               # the reaper sweeps stragglers by tag
                    return
                job["log"].append(f"auxiliary droplet {d['droplet']['id']} "
                                  f"({size}) provisioning")
                _persist_jobs()
                break
            except Exception as e:
                if job.get("cancel") or attempt == 3:
                    job["state"] = "cancelled" if job.get("cancel") else "failed"
                    job["error"] = (f"aux provision failed after "
                                    f"{attempt + 1} attempts: "
                                    f"{type(e).__name__}: {e}")[:250]
                    job["finished"] = time.time()
                    _persist_jobs()
                    # supersede the library stub too — a dead stub otherwise
                    # keeps its `started` stamp and a same-name relaunch
                    # inherits it (territory-5 sorted hours out of place)
                    _mark_cancelled(job)
                    _aux_destroy(jid)
                    return
                wait = 60 * 2 ** attempt
                job["log"].append(f"aux provision failed "
                                  f"({type(e).__name__}) — retry "
                                  f"{attempt + 2}/4 in {wait}s "
                                  "(DO API outage?)")
                _persist_jobs()
                time.sleep(wait)
    _persist_aux()
    _aux_watch(job, aux)


def _aux_resume(job, aux):
    """Thaw a paused aux job on a FRESH worker: same bearer + job record, new
    droplet; its bootstrap probes for the checkpoint and resumes mid-game."""
    jid = job["id"]
    with AUX_LOCK:
        rec = AUX.get(jid)
        bearer = rec["bearer"]
        rec["pause_by"] = None             # this pause is spent
    size = job.get("aux_size") or aux.get("size", "s-1vcpu-2gb")
    job["state"] = "running"
    job["started"] = time.time()           # the age cap re-arms from the thaw
    _persist_jobs()
    try:
        d = _do(aux, "POST", "/droplets", {
            "name": f"flotilla-aux-{jid}", "region": aux["region"],
            "size": size, "image": "debian-13-x64",
            "ssh_keys": [],   # no interactive login on a disposable worker
            "tags": ["flotilla-aux"],
            "user_data": _aux_user_data(jid, bearer, aux)})
        with AUX_LOCK:
            AUX[jid]["droplet_id"] = d["droplet"]["id"]
        job["log"].append(f"▶ resumed on fresh auxiliary {d['droplet']['id']}")
        _persist_jobs()
        _persist_aux()
    except Exception as e:
        job["state"] = "paused"            # checkpoint intact: try again later
        job["log"].append(f"resume provision failed ({type(e).__name__}) — "
                          "still paused, checkpoint safe")
        _persist_jobs()
        return
    _aux_watch(job, aux)


def _dispatch_resume(j, where=None):
    """The ONE resume path — /api/resume and the auto-resume loop both land
    here. Returns (http_status, payload)."""
    jid = j["id"]
    with JOBS_LOCK:
        if j["state"] != "paused":
            return 400, {"error": f"job is {j['state']}"}
        # claim NOW, atomically: while a resumed job waits on the run-queue
        # semaphore it used to sit in "paused", so the auto-resume prober (or
        # a user's ▶) dispatched a SECOND runner onto the same work dir
        j["state"] = "queued"
    _persist_jobs()

    def _bail(msg):
        with JOBS_LOCK:
            j["state"] = "paused"
        _persist_jobs()
        return 400, {"error": msg}
    if j.get("aux") and where == "local":
        # OPERATOR-CHOSEN handoff: finish an aux job on the flagship box
        # (local executor) when droplet provisioning is down. Checkpoints are
        # plain JSON (tolerantly rehydrated, never unpickled), so this carries
        # no deserialization risk; it stays gated behind an explicit
        # where=local because running a series on the flagship box is an
        # operator trade-off, not a default.
        wd = os.path.join(LIB, "_work", jid)
        gz = os.path.join(wd, "checkpoint.json.gz")
        if not os.path.isfile(gz):
            return _bail("checkpoint missing")
        import gzip as _gz
        with open(gz, "rb") as fh:
            raw = _gz.decompress(fh.read())
        with open(os.path.join(wd, "checkpoint.json"), "wb") as fh:
            fh.write(raw)
        j["aux"] = False               # hand it to the local executor
        with AUX_LOCK:
            AUX.pop(jid, None)         # release the aux record
        _persist_aux()
        j["log"].append("▶ resumed on the FLAGSHIP box (local "
                        "executor) — droplet provisioning bypassed")
        _persist_jobs()
        threading.Thread(target=_run_job, args=(j, {}, True),
                         daemon=True).start()
        return 200, {"ok": True, "resuming": True, "where": "local"}
    if j.get("aux"):
        aux = _aux_cfg()
        if aux is None:
            return _bail("aux not configured")
        if not os.path.isfile(os.path.join(LIB, "_work", jid,
                                           "checkpoint.json.gz")):
            return _bail("checkpoint missing")
        with AUX_LOCK:
            rec = AUX.get(jid)
        if rec is None:
            # the checkpoint holds the whole game — a lost record just needs
            # a fresh bearer (rows reseed from the partial series so the live
            # list stays whole)
            import secrets as _sec
            rows = []
            try:
                with open(os.path.join(LIB, "series", j["name"],
                                       "series.json")) as fh:
                    rows = [dict(seed=g.get("seed"),
                                 file=g.get("file", ""),
                                 winner=g.get("winner"))
                            for g in json.load(fh).get("games", [])]
            except Exception:
                pass
            # public live numbering must CONTINUE, not restart — a reset
            # show_chunks overwrote chunks public viewers already held. The
            # count lives in the bucket's own state.json; games_done is the
            # safe floor otherwise.
            chunks = 0
            try:
                import urllib.request as _ur
                with _ur.urlopen(_show_url(j, "live/state.json"),
                                 timeout=10) as r:
                    chunks = int(json.load(r).get("chunks", 0))
            except Exception:
                pass
            with AUX_LOCK:
                AUX[jid] = {"bearer": "kaux_" + _sec.token_urlsafe(24),
                            "droplet_id": None,
                            "born": time.time(), "config": {},
                            "rows": rows,
                            "show_chunks": chunks}
            _persist_aux()
        threading.Thread(target=_aux_resume, args=(j, aux),
                         daemon=True).start()
        return 200, {"ok": True, "resuming": True,
                     "note": "provisioning a fresh worker for the checkpoint"}
    if not os.path.isfile(os.path.join(LIB, "_work", jid, "checkpoint.json")):
        return _bail("checkpoint missing")
    threading.Thread(target=_run_job, args=(j, None, True),
                     daemon=True).start()
    return 200, {"ok": True, "resuming": True}


# ---- api-outage auto-resume: probe outage-paused runs back to life ----
_AUTORESUME_S = int(os.environ.get("FLOTILLA_AUTORESUME_S", "600"))
_AUTOPAUSE_CACHE = {}                      # jid -> (checkpoint mtime, info)
_AUTORESUME_LAST = {}                      # jid -> last probe time


def _autopause_info(jid):
    """auto_pause field from the job's checkpoint (None = human pause or no
    checkpoint). Parsed once per checkpoint write — cached by mtime."""
    wd = os.path.join(LIB, "_work", jid)
    for name in ("checkpoint.json", "checkpoint.json.gz"):
        p = os.path.join(wd, name)
        if not os.path.isfile(p):
            continue
        try:
            mt = os.path.getmtime(p)
            hit = _AUTOPAUSE_CACHE.get(jid)
            if hit and hit[0] == mt:
                return hit[1]
            if name.endswith(".gz"):
                import gzip as _gz
                with open(p, "rb") as fh:
                    ck = json.loads(_gz.decompress(fh.read()))
            else:
                with open(p) as fh:
                    ck = json.load(fh)
            info = ck.get("auto_pause")
            _AUTOPAUSE_CACHE[jid] = (mt, info)
            return info
        except Exception:
            return None
    return None


def _auto_resume_loop():
    """A run that froze itself via the api-outage circuit breaker (its
    checkpoint carries auto_pause) is probed back to life every
    FLOTILLA_AUTORESUME_S (default 600s). If the outage persists, the resumed
    run re-pauses after ONE more bad window (the streak is frozen with it) and
    the probe repeats; once a window succeeds it just keeps playing. A human
    pause (no auto_pause field) is never touched. Provisioning failures during
    a probe leave the job paused — the next probe retries."""
    while True:
        time.sleep(min(30, max(2, _AUTORESUME_S // 4)))
        try:
            _auto_resume_pass()
        except Exception:
            pass                           # the prober must never die


def _auto_resume_pass():
    """One sweep of the prober — factored out so tests can drive it without
    the sleep loop."""
    with JOBS_LOCK:
        paused = [j for j in JOBS if j.get("state") == "paused"]
    for j in paused:
        info = _autopause_info(j["id"])
        if not info:
            continue
        last = max(_AUTORESUME_LAST.get(j["id"], 0),
                   float(info.get("at", 0)))
        if time.time() - last < _AUTORESUME_S:
            continue
        _AUTORESUME_LAST[j["id"]] = time.time()
        j["log"].append("⟳ auto-resume probe (run paused itself: "
                        f"{info.get('reason', 'api-outage')})")
        _persist_jobs()
        code, payload = _dispatch_resume(j)
        if code != 200:
            j["log"].append("auto-resume not possible: "
                            f"{payload.get('error', '?')}")
            _persist_jobs()


def _aux_watch(job, aux):
    """Babysit a running aux job to its age cap; also respawned on restart."""
    if job.get("mode") == "tournament":
        # a tournament cannot pause (no checkpoint path — the runner has
        # nothing to freeze into), so the rotate-at-max_age_h machinery below
        # would only ever fail it: request a pause it can't answer, wait out
        # the grace, mark it failed. That is exactly how champions-cup-1 died
        # 6 games in. Tournaments instead run to completion under a HARD
        # runaway ceiling — generous, because a 4-lane best-of-5 bracket of
        # thinking models legitimately runs a day or two.
        cap_h = float(aux.get("tournament_max_age_h", 72))
        deadline = (job.get("started") or time.time()) + cap_h * 3600
        job["log"].append(f"tournament worker: rotation cap does not apply "
                          f"(no checkpoint path) — runaway ceiling {cap_h:g}h")
        _persist_jobs()
        while job["state"] == "running" and time.time() < deadline:
            time.sleep(20)
        if job["state"] == "running":
            job["state"] = "failed"
            job["error"] = (f"tournament exceeded the {cap_h:g}h runaway "
                            "ceiling (tournament_max_age_h)")
            job["finished"] = time.time()
            _persist_jobs()
            _mark_cancelled(job, error=job["error"])
        _aux_destroy(job["id"])
        return
    deadline = (job.get("started") or time.time()) \
        + float(aux["max_age_h"]) * 3600
    while job["state"] == "running" and time.time() < deadline:
        time.sleep(20)
    if job["state"] == "paused":
        # the droplet is already released; the AUX record (bearer + config)
        # MUST survive for resume — reaping it here stranded a paused job
        # (field bug: second pause/resume cycle 400'd 'record lost')
        return
    if job["state"] == "running":
        # Age cap reached with the job still healthy. Rotate the WORKER, not
        # the job: request a graceful pause (checkpoint ships home, droplet
        # released) and thaw on a fresh box — the cap re-arms from the resume.
        # Thinking-era series legitimately run 10-15h; the old fail+destroy
        # here killed domination-5 take 3 mid-game-4 with zero checkpoint.
        with AUX_LOCK:
            rec = AUX.get(job["id"])
            if rec is not None:
                rec["command"] = "pause"
                rec.setdefault("pause_by", "rotation")  # never override a
        _persist_aux()                                  # user's own pause
        job["log"].append(f"♻ age cap {aux['max_age_h']}h reached — rotating "
                          "worker (pause → fresh droplet)")
        _persist_jobs()
        grace = time.time() + 45 * 60      # a thinking window can run 5+ min;
        while job["state"] == "running" and time.time() < grace:
            time.sleep(20)                 # the checkpoint follows the window
        if job["state"] == "paused":
            with AUX_LOCK:
                rec = AUX.get(job["id"])
                by = rec.get("pause_by") if rec else None
                if rec is not None:
                    rec["pause_by"] = None
            if by == "user":               # the human paused during the grace
                return                     # window: STAY paused
            _aux_resume(job, aux)          # re-enters _aux_watch, cap re-armed
            return
        if job["state"] == "running":      # pause never landed: worker is gone
            job["state"] = "failed"        # or wedged — NOW reap for real
            job["error"] = (f"auxiliary exceeded max_age_h={aux['max_age_h']} "
                            "and did not answer a pause request")
            job["finished"] = time.time()
            _persist_jobs()
            _mark_cancelled(job, error=job["error"])
    _aux_destroy(job["id"])


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
            with POOL_LOCK:
                live |= {r["droplet_id"] for r in POOL.values()
                         if r.get("droplet_id")}
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


def _presets_path():
    return os.path.join(LIB, "presets.json")


def _load_presets():
    try:
        with open(_presets_path()) as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


class H(BaseHTTPRequestHandler):
    # HTTP/1.1: Caddy reuses loopback connections instead of opening a fresh
    # TCP conn per request (incl. every live poll). Safe: _send is the only
    # write path and always sets Content-Length.
    protocol_version = "HTTP/1.1"
    timeout = 30                # a slow client must not hold a thread forever
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
        if path == "/api/conn-reference":
            # the same conn DSL reference injected into admiral prompts —
            # exposed so the ⌨ program inspector and docs can link to it
            import conn as _conn
            return self._send(200, _conn.api_reference(), "text/plain")
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
            import llm as _llm
            return self._send(200, {"models": _models(),
                                    "scripted": SCRIPTED_BOTS,
                                    "prices": {k: list(v) for k, v
                                               in _llm.PRICES.items()},
                                    "max_series_cost": _keystore().get(
                                        "limits", {}).get("max_series_cost", 0)})
        if path == "/api/prompts":
            return self._send(200, _load_prompts())
        if path == "/api/presets":
            return self._send(200, _load_presets())
        if path == "/api/base-prompt":
            import llm as _llm
            return self._send(200, {"suggested": _llm.SYSTEM})
        if path == "/api/showcase":
            return self._send(200, {"enabled": _showcase_cfg() is not None,
                                    "published": _showcase_list()})
        if path == "/api/ships":
            return self._send(200, {"builtin": {k: dict(v) for k, v
                                                in contract.game().presets.items()},
                                    "saved": _load_ships(),
                                    "stats": list(SHIP_STATS)})
        if path == "/api/runs":
            with JOBS_LOCK:
                out = [dict(j, log=j["log"][-8:]) for j in JOBS[-20:]][::-1]
            return self._send(200, {"jobs": out})
        if path == "/favicon.ico":
            svg = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 '
                   '100"><text y=".9em" font-size="90">⛵</text></svg>')
            return self._send(200, svg.encode(), "image/svg+xml")
        if path == "/logos.json":
            for lp in (os.path.join(LIB, "logos.json"),
                       os.path.join(HERE, "assets", "logos.json")):
                if os.path.isfile(lp):
                    return self._send(200, open(lp, "rb").read())
            return self._send(404, {"error": "no logos.json — monograms used"})
        if path == "/index.json":
            p = os.path.join(LIB, "index.json")
            if not os.path.exists(p):
                build_index(LIB)
            return self._send(200, open(p, "rb").read())
        m = re.match(r"^/api/aux/pool/([A-Za-z0-9_.-]+)/job$", path)
        if m:
            pid = m.group(1)
            with POOL_LOCK:
                prec = POOL.get(pid)
            if prec is None or not _bearer_ok(self.headers.get("X-Aux-Token"), prec["bearer"]):
                return self._send(401, {"error": "bad pool token"})
            jid = prec.get("job")
            if not jid or jid == "claiming":
                return self._send(204, b"")
            with AUX_LOCK:
                rec = AUX.get(jid)
            if rec is None:
                return self._send(204, b"")
            aux = _aux_cfg() or {}
            return self._send(200, {
                "job_id": jid, "bearer": rec["bearer"],
                "callback_base": aux.get("callback_base", ""),
                "callback_auth": "",   # F2: never ship the dashboard password to a worker
                "config": rec["config"],
                "inference_key": os.environ.get("DO_INFERENCE_KEY", ""),
                "providers_env": _providers_json(_cfg_models(rec["config"]))})
        m = re.match(r"^/api/aux/([A-Za-z0-9_.-]+)/"
                     r"(app\.tar\.gz|job\.json|checkpoint\.json\.gz)$", path)
        if m:
            jid, what = m.groups()
            with AUX_LOCK:
                rec = AUX.get(jid)
            if rec is None or not _bearer_ok(self.headers.get("X-Aux-Token"), rec["bearer"]):
                return self._send(401, {"error": "bad aux token"})
            if what == "checkpoint.json.gz":
                ckp = os.path.join(LIB, "_work", jid, "checkpoint.json.gz")
                if not os.path.isfile(ckp):
                    return self._send(404, {"error": "no checkpoint"})
                return self._send(200, open(ckp, "rb").read(),
                                  "application/gzip")
            if what == "app.tar.gz":
                return self._send(200, _app_tarball(), "application/gzip")
            aux = _aux_cfg() or {}
            return self._send(200, {
                "job_id": jid, "bearer": rec["bearer"],
                "callback_base": aux.get("callback_base", ""),
                "callback_auth": "",   # F2: never ship the dashboard password to a worker
                "config": rec["config"],
                "inference_key": os.environ.get("DO_INFERENCE_KEY", ""),
                "providers_env": _providers_json(_cfg_models(rec["config"]))})
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
                   "games_expected": j.get("games_expected"),
                   "game": min(j["games_done"] + (0 if j["state"] != "running" else 1),
                               j.get("games_expected", 1))}
            try:                          # renamed series show their real title
                with open(os.path.join(LIB, "series", j["name"],
                                       "series.json")) as fh:
                    dn = json.load(fh).get("display_name")
                if dn:
                    out["display_name"] = dn
            except Exception:
                pass
            if os.path.isfile(livef):
                size = os.path.getsize(livef)
                # stream generation = the header game number on the file's
                # FIRST line. `ofs > size` alone misses a truncation whose
                # replacement already outgrew the reader's offset — the reader
                # would silently glue mid-line bytes of the NEW game onto the
                # old timeline. The viewer resets when stream_game changes.
                try:
                    with open(livef) as fh:
                        d1 = json.loads(fh.readline() or "null")
                    if isinstance(d1, dict) and d1.get("header"):
                        out["stream_game"] = d1.get("game")
                except ValueError:
                    pass
                if ofs > size:
                    ofs = 0                      # file truncated: a new game began
                # 2 MB per poll, with a `more` flag so the client re-polls
                # immediately. The old 12 MB cap parsed + re-serialized the
                # whole backlog in one request: 0.7 s CPU and +122 MB RSS per
                # cold-joining spectator on a 30 MB stream — an OOM risk on a
                # small droplet, and threads mean it multiplies per viewer.
                with open(livef) as fh:
                    fh.seek(ofs)
                    chunk = fh.read(2_000_000)
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
                if size > ofs:
                    out["more"] = True
            return self._send(200, out)
        # library files: replays/<match.json> | replays/<series>/<g.json> |
        # tournaments/... | bundles/...
        m = re.match(r"^/replays/([^/]+)$", path)
        if m:
            try:
                return self._file(_safe_path(os.path.join(LIB, "matches"),
                                             m.group(1)))
            except ValueError:
                return self._send(404, {"error": "not found"})
        m = re.match(r"^/replays/([^/]+)/([^/]+)$", path)
        if m:
            try:
                return self._file(_safe_path(os.path.join(LIB, "series"),
                                             m.group(1), m.group(2)))
            except ValueError:
                return self._send(404, {"error": "not found"})
        m = re.match(r"^/(tournaments|bundles)/(.+)$", path)
        if m:
            try:
                dst = _safe_path(os.path.join(LIB, m.group(1)),
                                 *m.group(2).split("/"))
            except ValueError:
                return self._send(404, {"error": "not found"})
            ct = "text/html" if dst.endswith(".html") else "application/json"
            return self._file(dst, ct)
        return self._send(404, {"error": "not found"})

    def _file(self, p, ctype="application/json"):
        if not os.path.isfile(p):
            return self._send(404, {"error": "not found"})
        return self._send(200, open(p, "rb").read(), ctype)

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        try:
            n = int(self.headers.get("Content-Length", 0))
            # the ONLY unauthenticated Caddy lane is /api/aux/* (worker
            # callbacks). Everything else sits behind basic auth.
            is_aux = path.startswith("/api/aux/")
            # CSRF: a browser-driven state change must originate from our own
            # site. do_POST parses JSON regardless of Content-Type, so these
            # are "simple" requests with no preflight and the browser attaches
            # the cached basic-auth credential. Reject a cross-origin Origin;
            # machine clients (workers, curl) send none and are unaffected.
            if not is_aux:
                origin = self.headers.get("Origin")
                if origin and (urllib.parse.urlparse(origin).netloc
                               != self.headers.get("Host", "")):
                    return self._send(403, {"error": "cross-origin POST refused"})
            # aux lane: AUTHENTICATE before buffering the body, and cap it far
            # below the 90 MB general ceiling — an anon flood of max-size posts
            # would otherwise exhaust a 1 GB droplet's RAM (threads = 1 buffer
            # each). Legit worker posts (a game replay) are a few MB.
            if is_aux:
                if not _aux_post_bearer_ok(
                        path, self.headers.get("X-Aux-Token")):
                    return self._send(401, {"error": "bad aux token"})
                if n > 48_000_000:
                    return self._send(413, {"error": "too large"})
            elif n > 90_000_000:
                return self._send(413, {"error": "too large"})
            body = self.rfile.read(n)
            if path == "/api/run":
                cfg = json.loads(body)
                job = submit_run(cfg)
                return self._send(202, {"job": dict(job, log=[])})
            if path == "/api/pause":
                jid = json.loads(body or b"{}").get("id", "")
                j = _job(jid)
                if not j:
                    return self._send(404, {"error": "no such job"})
                if j.get("mode") == "tournament":
                    # the tournament runner has no checkpoint path — accepting
                    # this used to return a success toast while the run burned
                    # to completion unpaused
                    return self._send(400, {"error": "tournaments cannot be "
                                            "paused yet — only matches and "
                                            "series checkpoint"})
                if j.get("aux"):
                    if j["state"] != "running":
                        return self._send(400, {"error": f"job is {j['state']}"})
                    with AUX_LOCK:
                        rec = AUX.get(jid)
                        if rec is None:
                            return self._send(400, {"error": "no aux record"})
                        rec["command"] = "pause"
                        rec["pause_by"] = "user"   # the rotation loop must not
                    _persist_aux()                 # auto-resume a HUMAN pause
                    return self._send(200, {"ok": True, "pausing": True,
                                            "note": "delivered on the worker's "
                                                    "next callback; the droplet "
                                                    "is released once the "
                                                    "checkpoint lands"})
                if j["state"] != "running" or jid not in PROCS:
                    return self._send(400, {"error": f"job is {j['state']}"})
                wd = os.path.join(LIB, "_work", jid)
                os.makedirs(wd, exist_ok=True)
                with open(os.path.join(wd, "pause.flag"), "w") as fh:
                    fh.write("1")
                return self._send(200, {"ok": True, "pausing": True,
                                        "note": "freezes at the next window "
                                                "boundary"})
            if path == "/api/resume":
                _rb = json.loads(body or b"{}")
                jid = _rb.get("id", "")
                j = _job(jid)
                if not j:
                    return self._send(404, {"error": "no such job"})
                code, payload = _dispatch_resume(j, _rb.get("where"))
                return self._send(code, payload)
            if path == "/api/cancel":
                jid = json.loads(body or b"{}").get("id", "")
                j = _job(jid)
                if not j:
                    return self._send(404, {"error": "no such job"})
                if j["state"] == "paused":     # kept games stay; checkpoint goes
                    j["state"] = "cancelled"
                    j["finished"] = time.time()
                    _mark_cancelled(j)
                    shutil.rmtree(os.path.join(LIB, "_work", jid),
                                  ignore_errors=True)
                    _persist_jobs()
                    return self._send(200, {"ok": True, "state": "cancelled"})
                if j["state"] not in ("queued", "running"):
                    return self._send(400, {"error": f"job already {j['state']}"})
                j["cancel"] = True
                p = PROCS.get(jid)
                if p:
                    try:
                        p.terminate()
                    except Exception:
                        pass
                elif j.get("aux") and j["state"] == "running":
                    j["state"] = "cancelled"       # the watcher sees it + reaps
                    j["finished"] = time.time()
                    _mark_cancelled(j)
                    _showcase_job_done(j)
                    threading.Thread(target=_aux_destroy, args=(jid,),
                                     daemon=True).start()
                elif j["state"] == "queued":       # not started yet: settle it now
                    j["state"] = "cancelled"
                    j["finished"] = time.time()
                _persist_jobs()
                return self._send(200, {"ok": True, "state": j["state"]})
            if path == "/api/presets":
                d = json.loads(body)
                name = _san(str(d.get("name", "")))[:40]
                if not name:
                    return self._send(400, {"error": "preset needs a name"})
                presets = _load_presets()
                knobs = d.get("knobs")
                if isinstance(knobs, dict) and knobs:
                    rec = {"knobs": {str(k)[:40]: v for k, v in
                                     list(knobs.items())[:80]}}
                    if isinstance(d.get("players"), list):
                        rec["players"] = [str(x)[:80] for x in d["players"][:8]]
                    if d.get("desc"):
                        rec["desc"] = str(d["desc"])[:300]
                    presets[name] = rec
                else:
                    presets.pop(name, None)
                with open(_presets_path(), "w") as fh:
                    json.dump(presets, fh, indent=1)
                return self._send(200, presets)
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
            m = re.match(r"^/api/aux/([A-Za-z0-9_.-]+)/paused$", path)
            if m:
                jid = m.group(1)
                with AUX_LOCK:
                    rec = AUX.get(jid)
                j = _job(jid)
                if rec is None or j is None \
                        or not _bearer_ok(self.headers.get("X-Aux-Token"), rec["bearer"]):
                    return self._send(401, {"error": "bad aux token"})
                wd = os.path.join(LIB, "_work", jid)
                os.makedirs(wd, exist_ok=True)
                # stored as data, served back to the (next) worker — plain
                # JSON inside, no side ever deserializes executable state
                with open(os.path.join(wd, "checkpoint.json.gz"), "wb") as fh:
                    fh.write(body or b"")
                j["state"] = "paused"
                j["log"].append("⏸ paused — checkpoint shipped home; worker "
                                "released (a paused auxiliary costs nothing)")
                _persist_jobs()
                cfg2 = _aux_cfg()
                did = rec.get("droplet_id")
                with AUX_LOCK:
                    rec["droplet_id"] = None
                _persist_aux()
                if cfg2 and did:
                    def _reap(did=did):
                        try:
                            _do(cfg2, "DELETE", f"/droplets/{did}")
                        except Exception:
                            pass
                    threading.Thread(target=_reap, daemon=True).start()
                return self._send(200, {"ok": True, "paused": True})
            m = re.match(r"^/api/aux/([A-Za-z0-9_.-]+)/(live|game|done|fail)$", path)
            if m:
                jid, what = m.groups()
                with AUX_LOCK:
                    rec = AUX.get(jid)
                j = _job(jid)
                if rec is None or j is None \
                        or not _bearer_ok(self.headers.get("X-Aux-Token"), rec["bearer"]):
                    return self._send(401, {"error": "bad aux token"})
                d = json.loads(body or b"{}")
                wd = os.path.join(LIB, "_work", jid)
                os.makedirs(wd, exist_ok=True)
                if what == "live":
                    # a header line = a NEW game: truncate first, matching the
                    # local runner's per-game file semantics. The old blind
                    # append accumulated every game — a cold Live click then
                    # replayed the whole series mislabeled as the current game
                    lines = d.get("lines", [])
                    lp = os.path.join(wd, "live.jsonl")
                    # truncate at the LAST header in the batch: one chunk can
                    # carry two game boundaries (a short game ending fast),
                    # and breaking at the first re-created exactly the
                    # multi-game concatenation this truncation exists to kill.
                    # isinstance guard: live lines are not all dicts.
                    last = None
                    for i2, line in enumerate(lines):
                        if isinstance(line, dict) and line.get("header"):
                            last = i2
                    if last is not None:
                        with open(lp, "w") as fh:
                            for l2 in lines[last:]:
                                fh.write(json.dumps(
                                    l2, separators=(",", ":")) + "\n")
                    else:
                        with open(lp, "a") as fh:
                            for line in lines:
                                fh.write(json.dumps(
                                    line, separators=(",", ":")) + "\n")
                    _showcase_job_live(j, rec, d.get("lines") or [])
                    cmd = None
                    with AUX_LOCK:
                        if rec.get("command"):
                            cmd = rec["command"]   # one-shot delivery
                            rec["command"] = None
                    if cmd:
                        _persist_aux()
                    return self._send(200, {"ok": True, "command": cmd})
                if what == "game":
                    # file may carry ONE matchup-dir level (tournaments); every
                    # component is basename-sanitized — no traversal, ever
                    parts = [os.path.basename(x)
                             for x in str(d.get("file", "")).split("/")
                             if x and x != ".."][-2:]
                    fn = "/".join(parts)
                    rp = d.get("replay")
                    if not fn.endswith(".json") or not isinstance(rp, dict):
                        return self._send(400, {"error": "bad game payload"})
                    # a bearer-holding worker can't fill the disk by posting
                    # unbounded games: cap at the expected count + slack
                    if j["games_done"] > int(j.get("games_expected", 1)) + 4:
                        return self._send(409, {"error": "game count exceeded"})
                    if len(j["log"]) > 400:          # display tail only
                        del j["log"][:-200]
                    if j["mode"] == "tournament":
                        dst = os.path.join(LIB, "tournaments", j["name"], *parts)
                        os.makedirs(os.path.dirname(dst), exist_ok=True)
                        blob = json.dumps(rp, separators=(",", ":")).encode()
                        tmp = dst + ".tmp"
                        with open(tmp, "wb") as fh:
                            fh.write(blob)
                        os.replace(tmp, dst)
                        if d.get("row"):
                            rec["rows"].append(d["row"])
                            j["games_done"] += 1
                            j["log"].append(json.dumps(d["row"])[:400])
                            _persist_aux()   # a restart mid-tournament must
                                             # not renumber later matchups
                        _showcase_job_game(j, fn, rp, blob=blob)
                        build_index(LIB)
                        _persist_jobs()
                        # registered public link tracks the bracket live —
                        # threaded: a multi-MB S3 put must not stall the
                        # worker's callback
                        threading.Thread(
                            target=_showcase_refresh_tournament,
                            args=(j["name"], fn, blob), daemon=True).start()
                        return self._send(200, {"ok": True})
                    if j["mode"] == "series":
                        dst = os.path.join(LIB, "series", j["name"])
                        os.makedirs(dst, exist_ok=True)
                        blob = json.dumps(rp, separators=(",", ":")).encode()
                        tmp = os.path.join(dst, fn + ".tmp")
                        with open(tmp, "wb") as fh:
                            fh.write(blob)
                        os.replace(tmp, os.path.join(dst, fn))
                        if d.get("row"):
                            rec["rows"].append(d["row"])
                            j["games_done"] += 1
                            j["log"].append(json.dumps(d["row"])[:400])
                            _persist_aux()
                        grows = [dict(game=i + 1, seed=r.get("seed"),
                                      file=os.path.basename(r["file"]),
                                      winner=r.get("winner"))
                                 for i, r in enumerate(rec["rows"])]
                        _update_series_json(dst, lambda cur: {
                            **_meta_of(cur), "games": grows,
                            "memos": {}, "partial": True})
                        _showcase_job_game(j, fn, rp, blob=blob)
                    else:
                        os.makedirs(os.path.join(LIB, "matches"), exist_ok=True)
                        blob = json.dumps(rp, separators=(",", ":")).encode()
                        with open(os.path.join(LIB, "matches",
                                               f"{j['name']}.json"), "wb") as fh:
                            fh.write(blob)
                        if d.get("row"):   # the final sweep re-posts with
                            j["games_done"] += 1   # row=None: count each once
                        _showcase_job_game(j, f"{j['name']}.json", rp, blob=blob)
                    build_index(LIB)
                    _persist_jobs()
                    return self._send(200, {"ok": True})
                if what == "done":
                    if j["mode"] == "series":
                        dst = os.path.join(LIB, "series", j["name"])
                        os.makedirs(dst, exist_ok=True)
                        ser = d.get("series") or {}
                        ser.pop("partial", None)
                        _update_series_json(dst, lambda cur:
                                            {**_meta_of(cur), **ser})
                    if j["mode"] == "tournament" and isinstance(d.get("tournament"),
                                                                dict):
                        dst = os.path.join(LIB, "tournaments", j["name"])
                        os.makedirs(dst, exist_ok=True)
                        tj = d["tournament"]
                        tj.pop("partial", None)
                        with open(os.path.join(dst, "tournament.json"), "w") as fh:
                            json.dump(tj, fh, indent=1)
                    _showcase_job_done(j)
                    build_index(LIB)
                    j["state"] = "done"
                    j["finished"] = time.time()
                    _persist_jobs()
                    threading.Thread(target=_aux_destroy, args=(jid,),
                                     daemon=True).start()
                    return self._send(200, {"ok": True})
                j["error"] = str(d.get("error", "aux failed"))[:250]  # fail
                if os.path.isfile(os.path.join(wd, "checkpoint.json.gz")):
                    # a checkpoint from an earlier rotation is on disk: the
                    # job is recoverable from that point — paused-with-error
                    # beats failed-and-doomed (never delete a checkpoint on
                    # failure)
                    j["state"] = "paused"
                    j["log"].append("⚠ worker failed but a checkpoint is on "
                                    "disk — preserved; job back to PAUSED "
                                    f"(error: {j['error']})")
                else:
                    j["state"] = "failed"
                    j["finished"] = time.time()
                    _showcase_job_done(j)
                    _mark_cancelled(j, error=j.get("error"))
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
                # workers fetch their checkpoint + app over callback_base and
                # carry DO_INFERENCE_KEY — an http:// base would let a MitM
                # own every worker. Require TLS.
                if not str(d["callback_base"]).lower().startswith("https://"):
                    return self._send(400, {"error": "callback_base must be https"})
                _write_secret_file(os.path.join(LIB, "aux.json"),
                                   json.dumps(d))
                return self._send(200, {"ok": True, "aux": True})
            if path == "/api/showcase-config":
                d = json.loads(body)
                need = {"access_key", "secret_key", "endpoint", "bucket"}
                if not need <= set(d):
                    return self._send(400, {"error": f"need {sorted(need)}"})
                _write_secret_file(os.path.join(LIB, "showcase.json"),
                                   json.dumps({k: str(d[k]) for k in
                                               ("access_key", "secret_key",
                                                "endpoint", "bucket", "region")
                                               if k in d}))
                return self._send(200, {"ok": True, "showcase": True})
            if path == "/api/showcase":
                cfg = _showcase_cfg()
                if cfg is None:
                    return self._send(400, {"error": "showcase not configured — "
                                            "POST /api/showcase-config or set "
                                            "SHOWCASE_* env"})
                d = json.loads(body)
                title = str(d.get("name") or "").strip()
                if d.get("tournament"):
                    # a tournament link is a PREFIX, not one file: the
                    # tournament page (as index.html), the player, and every
                    # matchup replay + the bracket json. Re-publishing syncs
                    # newly landed games to the same URL.
                    tname = _san(str(d["tournament"]))
                    tdir = os.path.join(LIB, "tournaments", tname)
                    if not os.path.isfile(os.path.join(tdir, "tournament.json")):
                        return self._send(404, {"error": "no such tournament"})
                    prefix = f"showcase/tournament-{tname}"
                    try:
                        with open(os.path.join(HERE, "dash",
                                               "tournament.html"), "rb") as fh:
                            _s3_put_public(cfg, f"{prefix}/index.html",
                                           fh.read())
                        with open(os.path.join(HERE, "viewer",
                                               "index.html"), "rb") as fh:
                            _s3_put_public(cfg, f"{prefix}/player.html",
                                           fh.read())
                        for lp in (os.path.join(LIB, "logos.json"),
                                   os.path.join(HERE, "assets", "logos.json")):
                            if os.path.isfile(lp):
                                _s3_put_public(cfg, f"{prefix}/logos.json",
                                               open(lp, "rb").read(),
                                               "application/json")
                                break
                        nfiles = 0
                        for root, _dirs, files in os.walk(tdir):
                            for fn in files:
                                if not fn.endswith(".json"):
                                    continue
                                rel = os.path.relpath(os.path.join(root, fn),
                                                      tdir)
                                with open(os.path.join(root, fn), "rb") as fh:
                                    _s3_put_public(cfg, f"{prefix}/{rel}",
                                                   fh.read(),
                                                   "application/json")
                                nfiles += 1
                        # the matchup-series slice of the index rides along so
                        # the PUBLIC page can show in-flight lanes + their
                        # landed games, same as the flagship view
                        try:
                            with open(os.path.join(LIB, "index.json")) as fh:
                                full = json.load(fh)
                            slc = [s for s in full.get("series", [])
                                   if s.get("tournament") == tname]
                            _s3_put_public(cfg, f"{prefix}/series-index.json",
                                           json.dumps({"series": slc}).encode(),
                                           "application/json")
                        except Exception:
                            pass
                    except Exception as e:
                        return self._send(502, {"error": f"upload failed: "
                                                f"{type(e).__name__}: {e}"[:200]})
                    url = (f"https://{cfg['bucket']}.{cfg['endpoint']}/"
                           f"{prefix}/index.html")
                    entry = {"name": title or tname,
                             "ident": f"tournament-{tname}", "url": url,
                             "bytes": 0, "when": time.strftime(
                                 "%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
                    _showcase_list_update(
                        lambda pub: [x for x in pub if x.get("url") != url]
                        + [entry])
                    return self._send(200, {"ok": True, "url": url,
                                            "ident": f"tournament-{tname}",
                                            "files": nfiles})
                if d.get("series"):
                    sname = _san(str(d["series"]))
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
                                         _san(str(d["match"])))
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
                        import make_bundle
                        payload = tpl.replace(
                            "/*" + "EMBED_REPLAY" + "*/null",
                            make_bundle.js_embed_safe(fh.read()), 1).encode()
                else:
                    return self._send(400, {"error": "give series or match"})
                # key on the SERIES/MATCH IDENTITY (not the free-form title) so
                # a repeat request for the same series/game always resolves to
                # the SAME public URL — re-publishing overwrites in place and the
                # list dedups by url, so we never mint a duplicate link
                ident = (f"series-{_san(str(d['series']))}" if d.get("series")
                         else f"match-{_san(os.path.basename(str(d['match'])).rsplit('.', 1)[0])}")
                key = f"showcase/{ident or 'match'}.html"
                try:
                    _s3_put_public(cfg, key, payload)
                except Exception as e:
                    return self._send(502, {"error": f"upload failed: "
                                            f"{type(e).__name__}: {e}"[:200]})
                url = f"https://{cfg['bucket']}.{cfg['endpoint']}/{key}"
                entry = {"name": title, "ident": ident, "url": url,
                         "bytes": len(payload), "when": time.strftime(
                             "%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
                _showcase_list_update(
                    lambda pub: [x for x in pub if x.get("url") != url]
                    + [entry])
                return self._send(200, {"ok": True, "url": url,
                                        "ident": ident,
                                        "bytes": len(payload)})
            if path == "/api/showcase-delete":
                # retire a public link: delete the bucket object AND drop the
                # registry entry — the dashboard's 🌐 button comes back
                cfg = _showcase_cfg()
                if cfg is None:
                    return self._send(400, {"error": "showcase not configured"})
                d = json.loads(body)
                ident = (f"tournament-{_san(str(d['tournament']))}"
                         if d.get("tournament")
                         else f"series-{_san(str(d['series']))}" if d.get("series")
                         else f"match-{_san(os.path.basename(str(d.get('match', ''))).rsplit('.', 1)[0])}")
                if ident in ("series-", "match-", "tournament-"):
                    return self._send(400, {"error": "give series, match, or "
                                            "tournament"})
                deleted, failed = _showcase_purge(ident)
                if failed:
                    return self._send(502, {"error": "delete incomplete — "
                                            "kept the registry entry so it "
                                            "stays retryable",
                                            "deleted": deleted,
                                            "failed": failed})
                return self._send(200, {"ok": True, "ident": ident,
                                        "deleted": deleted})
            if path == "/api/ships":
                # save/remove an operator ship class (the Configure designer).
                # ships.json is hand-editable; the GET reads it fresh.
                d = json.loads(body)
                nm = str(d.get("name", ""))
                with SHIPS_LOCK:
                    ships = _load_ships()
                    if d.get("delete"):
                        ships.pop(re.sub(r"[^A-Za-z0-9_-]", "", nm)[:24], None)
                    else:
                        name, clean = _clean_ship(nm, d.get("stats") or {})
                        if name is None:
                            return self._send(400, {
                                "error": "invalid ship: name must be letters/"
                                "digits/-/_ (not a built-in class name) and "
                                "every stat an integer 1-40"})
                        ships[name] = clean
                    tmp = _ships_path() + ".tmp"
                    with open(tmp, "w") as fh:
                        json.dump(ships, fh, indent=1, sort_keys=True)
                    os.replace(tmp, _ships_path())
                return self._send(200, {"ok": True,
                                        "builtin": {k: dict(v) for k, v
                                                    in contract.game().presets.items()},
                                        "saved": ships,
                                        "stats": list(SHIP_STATS)})
            if path == "/api/rename":
                d = json.loads(body)
                disp = str(d.get("display_name", "")).strip()[:120]
                if d.get("match"):         # standalone match: sidecar meta
                    fn = _san(str(d["match"]))
                    if not os.path.isfile(os.path.join(LIB, "matches", fn)):
                        return self._send(404, {"error": "no such match"})
                    with META_LOCK:
                        meta = matches_meta(LIB)
                        ent = meta.setdefault(fn, {})
                        if disp:
                            ent["display_name"] = disp
                        else:
                            ent.pop("display_name", None)
                        save_matches_meta(LIB, meta)
                    build_index(LIB)
                    return self._send(200, {"ok": True,
                                            "display_name": disp or None})
                name = _san(str(d.get("series", "")))
                sdir = os.path.join(LIB, "series", name)
                if not name or not os.path.isfile(os.path.join(sdir,
                                                               "series.json")):
                    return self._send(404, {"error": "no such series"})

                def _ren(cur, disp=disp):
                    if disp:
                        cur["display_name"] = disp
                    else:
                        cur.pop("display_name", None)
                    return cur
                _update_series_json(sdir, _ren)
                build_index(LIB)
                return self._send(200, {"ok": True, "display_name": disp or None})
            if path == "/api/archive":
                # hide/show in the dashboard list — the data on disk is
                # untouched: series flip a flag in series.json, standalone
                # matches flip one in the matches-meta sidecar
                d = json.loads(body)
                if d.get("tournament"):
                    # archiving a TOURNAMENT hides the whole thing: its row on
                    # the Tournaments tab AND every matchup series/game on the
                    # Games page (the index inherits the flag)
                    tn = _san(str(d["tournament"]))
                    tp = os.path.join(LIB, "tournaments", tn, "tournament.json")
                    if not os.path.isfile(tp):
                        return self._send(404, {"error": "no such tournament"})
                    with META_LOCK:
                        with open(tp) as fh:
                            t = json.load(fh)
                        if d.get("archived"):
                            t["archived"] = True
                        else:
                            t.pop("archived", None)
                        tmp = tp + ".tmp"
                        with open(tmp, "w") as fh:
                            json.dump(t, fh, indent=1)
                        os.replace(tmp, tp)
                    build_index(LIB)
                    return self._send(200, {"ok": True,
                                            "archived": bool(d.get("archived"))})
                if d.get("match"):
                    fn = _san(str(d["match"]))
                    if not os.path.isfile(os.path.join(LIB, "matches", fn)):
                        return self._send(404, {"error": "no such match"})
                    with META_LOCK:
                        meta = matches_meta(LIB)
                        ent = meta.setdefault(fn, {})
                        if d.get("archived"):
                            ent["archived"] = True
                        else:
                            ent.pop("archived", None)
                        save_matches_meta(LIB, meta)
                    build_index(LIB)
                    return self._send(200, {"ok": True,
                                            "archived": bool(d.get("archived"))})
                name = _san(str(d.get("series", "")))
                sdir = os.path.join(LIB, "series", name)
                if not name or not os.path.isfile(os.path.join(sdir,
                                                               "series.json")):
                    return self._send(404, {"error": "no such series"})
                want = bool(d.get("archived"))

                def _arch(cur, want=want):
                    if want:
                        cur["archived"] = True
                    else:
                        cur.pop("archived", None)
                    return cur
                _update_series_json(sdir, _arch)
                build_index(LIB)
                return self._send(200, {"ok": True, "archived": want})
            if path == "/api/delete-match":
                # permanent removal of a standalone match replay (dashboard
                # confirms first; the daily restic backup is the only undo)
                d = json.loads(body)
                fn = _san(str(d.get("match", "")))
                p = os.path.join(LIB, "matches", fn)
                if not fn or fn == "matches-meta.json" or not os.path.isfile(p):
                    return self._send(404, {"error": "no such match"})
                os.remove(p)
                for ext in (".gz",):
                    if os.path.isfile(p + ext):
                        os.remove(p + ext)
                with META_LOCK:
                    meta = matches_meta(LIB)
                    if meta.pop(fn, None) is not None:
                        save_matches_meta(LIB, meta)
                # a deleted replay's public link must die with it (archive
                # keeps its link; DELETE is the full-removal path)
                purged, pfail = _showcase_purge(
                    f"match-{_san(fn.rsplit('.', 1)[0])}")
                build_index(LIB)
                return self._send(200, {"ok": True, "deleted": fn,
                                        "showcase_purged": purged,
                                        **({"showcase_failed": pfail}
                                           if pfail else {})})
            if path == "/api/delete-tournament":
                # permanent removal of the WHOLE tournament — bracket + every
                # matchup series + every game (the dashboard confirms first;
                # the daily restic backup is the only undo). Live job = refuse.
                d = json.loads(body)
                tn = _san(str(d.get("tournament", "")))
                tdir = os.path.join(LIB, "tournaments", tn)
                if not tn or not os.path.isfile(os.path.join(
                        tdir, "tournament.json")):
                    return self._send(404, {"error": "no such tournament"})
                with JOBS_LOCK:
                    live = any(j.get("name") == tn and
                               j.get("state") in ("queued", "running", "paused")
                               for j in JOBS)
                if live:
                    return self._send(400, {"error": "this tournament is "
                                            "queued/running/paused — cancel "
                                            "it first"})
                shutil.rmtree(tdir, ignore_errors=True)
                purged, pfail = _showcase_purge(f"tournament-{tn}")
                build_index(LIB)
                return self._send(200, {"ok": True, "showcase_purged": purged,
                                        **({"showcase_failed": pfail}
                                           if pfail else {})})
            if path == "/api/delete-series":
                # permanent removal from the library (the dashboard confirms
                # first; the daily restic backup is the only undo). A series
                # with a queued/running/paused job is refused — cancel first.
                d = json.loads(body)
                name = _san(str(d.get("series", "")))
                sdir = os.path.join(LIB, "series", name)
                if not name or not os.path.isfile(os.path.join(sdir,
                                                               "series.json")):
                    return self._send(404, {"error": "no such series"})
                with JOBS_LOCK:
                    live = any(j.get("name") == name and
                               j.get("state") in ("queued", "running", "paused")
                               for j in JOBS)
                if live:
                    return self._send(400, {"error": "a job for this series "
                                            "is queued/running/paused — "
                                            "cancel it first"})
                shutil.rmtree(sdir, ignore_errors=True)
                purged, pfail = _showcase_purge(f"series-{name}")
                build_index(LIB)
                return self._send(200, {"ok": True, "deleted": name,
                                        "showcase_purged": purged,
                                        **({"showcase_failed": pfail}
                                           if pfail else {})})
            if path == "/api/providers":
                st = _keystore()
                masked = []
                for p in st["providers"]:
                    q = {k: v for k, v in p.items() if k != "key"}
                    q["key_hint"] = "server environment" if p.get("builtin") \
                        else _mask_key(p.get("key", ""))
                    masked.append(q)
                ax = _aux_cfg() or {}
                return self._send(200, {"providers": masked,
                                        "fallback": st["fallback"],
                                        "limits": st.get("limits", {}),
                                        "aux": {"configured": bool(ax),
                                                "region": ax.get("region"),
                                                "size": ax.get("size"),
                                                "pool": ax.get(
                                                    "max_concurrent")}})
            if path == "/api/providers-op":
                code, payload = _providers_op(json.loads(body))
                return self._send(code, payload)
            if path == "/api/provider-check":
                d = json.loads(body)
                with KS_LOCK:
                    st = _keystore()
                    p = next((x for x in st["providers"]
                              if x["id"] == d.get("id")), None)
                    p = dict(p) if p else None
                if not p:
                    return self._send(404, {"error": "no such provider"})
                key = os.environ.get("DO_INFERENCE_KEY", "") \
                    if p.get("builtin") else p.get("key", "")
                try:
                    models = _list_models(p["base_url"], key)[:200]
                except Exception as e:
                    return self._send(200, {"ok": False, "error":
                                            f"{type(e).__name__}: {e}"[:200]})
                # merge against a FRESH read, under the lock: the network call
                # above takes up to 20s and used to write back its whole
                # pre-call snapshot, silently reverting any add/remove/toggle
                # that landed meanwhile
                with KS_LOCK:
                    st = _keystore()
                    p = next((x for x in st["providers"]
                              if x["id"] == d.get("id")), None)
                    if not p:
                        return self._send(404, {"error":
                                                "provider removed mid-check"})
                    p["models"] = models
                    p["checked"] = time.time()
                    if not p.get("builtin"):
                        # auto-map the primary's admiral ids onto this
                        # provider's names; manual entries (if any) win
                        p["model_map"] = {**_auto_map(models),
                                          **(p.get("model_map") or {})}
                    _save_keystore(st)
                    mapped = len(p.get("model_map") or {})
                return self._send(200, {"ok": True, "models": models,
                                        "mapped": mapped})
            if path == "/api/bundle":
                d = json.loads(body)
                name = _san(str(d.get("series", "")))
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
                if not (isinstance(rp, dict) and "frames" in rp
                        and "result" in rp):     # not assert (no-op under -O)
                    return self._send(400, {"error": "not a flotilla replay"})
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
    def _boot_index():
        # behind the socket: a cold build on a big library is ~20s and the
        # updater restarts the service on every deploy — that was 20s of hard
        # downtime per deploy. The on-disk index.json serves meanwhile (at
        # worst one build stale for a few seconds), and this warms the
        # (mtime,size) parse cache so every later rebuild is milliseconds.
        try:
            build_index(LIB)      # a damaged library entry must never stop the
        except Exception as e:    # flagship from serving — log, then repair
            print(f"WARNING: index build failed ({e}); serving stale index")
    threading.Thread(target=_boot_index, daemon=True).start()
    host, port = BIND.rsplit(":", 1)
    if not os.environ.get("DO_INFERENCE_KEY"):
        print("NOTE: DO_INFERENCE_KEY not set — scripted-bot runs only until you export it.")
    # This process authenticates NOTHING. Every mutating route (/api/run,
    # /api/delete-series, /api/providers-op, …) is open to whoever can reach the
    # socket; the CSRF Origin check only bites browsers, so any script sending no
    # Origin header sails straight through. That is fine on loopback and fine
    # behind the documented auth proxy — it is a full compromise on 0.0.0.0 with
    # nothing in front, so say so loudly rather than leaving it to the docs.
    if host not in ("127.0.0.1", "::1", "localhost"):
        print(f"⚠ WARNING: binding {host} — NOT loopback. This server has no "
              "authentication of its own:\n"
              "⚠   anyone who can reach this port can start runs, delete "
              "library entries, and read/rotate\n"
              "⚠   provider config. Put an authenticating reverse proxy in "
              "front of it (see the module docstring),\n"
              "⚠   or bind FLOTILLA_BIND=127.0.0.1:8080 and tunnel in.")
    _restore_state()
    _heal_stale_tournaments()          # no live job -> no '⏳ live' ghost
    threading.Thread(target=_aux_reaper, daemon=True).start()
    threading.Thread(target=_pool_maintain, daemon=True).start()
    threading.Thread(target=_auto_resume_loop, daemon=True).start()
    srv = ThreadingHTTPServer((host, int(port)), H)
    print(f"Flotilla server {VERSION} — http://{BIND}  (library: {LIB})")
    srv.serve_forever()


if __name__ == "__main__":
    main()
