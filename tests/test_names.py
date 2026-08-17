#!/usr/bin/env python3
"""Run names are DIRECTORIES: collision refusal, and a real tournament rename.

Two runs sharing a name write into the same directory and blend their games.
That is not theoretical — a cancelled cup's results ended up mixed with the run
that replaced it, because relaunching under the same name silently reused its
directory. Nothing refused it, and nothing could move the old data out of the
way: /api/rename set a display label only (and did not handle tournaments at
all), and delete-tournament is a permanent rmtree.

So both halves live here:
  * name_conflict / submit_run  — a taken name is refused before anything runs
  * /api/rename {"tournament", "new_name"} — a REAL rename that moves the dir

Archiving must NOT free a name (the data is still on disk and would be written
into); deleting must (it is gone). Both are asserted below.
"""
import json
import os
import shutil
import sys
import tempfile
import threading
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "sim"))
sys.path.insert(0, ROOT)
TMP = tempfile.mkdtemp(prefix="flotilla-names-")
os.environ["FLOTILLA_LIBRARY"] = TMP

import server                                                # noqa: E402
from http.server import ThreadingHTTPServer                  # noqa: E402

fails = 0


def ok(cond, msg):
    global fails
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        fails += 1


for d in ("matches", "series", "tournaments", "bundles"):
    os.makedirs(os.path.join(TMP, d), exist_ok=True)


def make_tournament(name, archived=False, games=2):
    tdir = os.path.join(TMP, "tournaments", name)
    mdir = os.path.join(tdir, "m01_A_v_B")
    os.makedirs(mdir, exist_ok=True)
    json.dump({"config": {"name": name, "tournament": {"format": "round_robin"}},
               "matchups": [], "standings": {}, "archived": archived},
              open(os.path.join(tdir, "tournament.json"), "w"))
    for i in range(1, games + 1):
        json.dump({"meta": {"seed": i}, "frames": [{"t": 0}],
                   "result": {"names": {"0": "A", "1": "B"},
                              "scores": {"0": 1, "1": 0}, "winner": 0,
                              "ticks": 10}},
                  open(os.path.join(mdir, f"g{i}.json"), "w"))
    return tdir


make_tournament("cup-taken")
os.makedirs(os.path.join(TMP, "series", "series-taken"), exist_ok=True)
json.dump({"games": []},
          open(os.path.join(TMP, "series", "series-taken", "series.json"), "w"))
json.dump({"meta": {}, "frames": [], "result": {}},
          open(os.path.join(TMP, "matches", "match-taken.json"), "w"))
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


# ---- what owns a name ----
ok(server.library_name_owner("cup-taken") == "tournament",
   "a tournament owns its name")
ok(server.library_name_owner("series-taken") == "series", "a series owns its name")
ok(server.library_name_owner("match-taken") == "match", "a match owns its name")
ok(server.library_name_owner("nobody-here") is None, "a free name is free")

# ---- archiving keeps the name; deleting frees it ----
make_tournament("cup-archived", archived=True)
server.build_index(TMP)
ok(server.name_conflict("cup-archived"),
   "an ARCHIVED tournament still holds its name — the data is on disk and would "
   "be written into")
st, r = req("/api/delete-tournament", {"tournament": "cup-archived"})
ok(st == 200, f"the archived tournament deletes ({st} {r})")
ok(server.name_conflict("cup-archived") is None,
   "DELETING frees the name, because the data is gone")

# ---- submit refuses a taken name, before provisioning anything ----
base = {"mode": "match", "seed": 1, "bots": ["merchant", "corsair"],
        "scenario": {"width": 48, "height": 30, "max_ticks": 60}}
try:
    server.submit_run(dict(base, name="cup-taken"))
    ok(False, "submit_run accepted a name the library already owns")
except ValueError as e:
    ok("cup-taken" in str(e) and "archiv" in str(e).lower(),
       f"submit_run refuses it and explains archiving vs deleting "
       f"({str(e)[:70]!r})")
st, r = req("/api/run", dict(base, name="series-taken"))
ok(st == 400 and "series" in r.get("error", ""),
   f"/api/run returns a clean 400 for a taken name ({st} {r.get('error','')[:60]!r})")
ok(server.name_conflict("") is None and server.name_conflict(None) is None,
   "a blank name is fine — the server auto-names that run")

# ---- the pre-flight endpoint the Configure form uses ----
st, r = req("/api/name-check?name=cup-taken")
ok(st == 200 and r.get("conflict"), "name-check reports a clash")
st, r = req("/api/name-check?name=totally-free")
ok(st == 200 and r.get("conflict") is None, "name-check passes a free name")

# ---- a REAL tournament rename ----
make_tournament("cup-rename-me", games=3)
server.build_index(TMP)
st, r = req("/api/rename", {"tournament": "cup-rename-me",
                            "new_name": "cup-renamed"})
ok(st == 200 and r.get("tournament") == "cup-renamed",
   f"the tournament renames ({st} {r})")
old = os.path.join(TMP, "tournaments", "cup-rename-me")
new = os.path.join(TMP, "tournaments", "cup-renamed")
ok(not os.path.exists(old) and os.path.isdir(new),
   "the DIRECTORY moved — this is a real rename, not a display label")
ok(sorted(os.listdir(os.path.join(new, "m01_A_v_B"))) ==
   ["g1.json", "g2.json", "g3.json"],
   "every game moved with it (they live inside the tournament directory)")
doc = json.load(open(os.path.join(new, "tournament.json")))
ok(doc["config"]["name"] == "cup-renamed",
   "the embedded config.name was rewritten, so the files and the record agree")
idx = json.load(open(os.path.join(TMP, "index.json")))
names = [t["name"] for t in idx["tournaments"]]
ok("cup-renamed" in names and "cup-rename-me" not in names,
   f"the index shows the new name only (got {names})")
paths = [g["file"] for s in idx["series"] if s.get("tournament") == "cup-renamed"
         for g in s["games"]]
ok(paths and all(p.startswith("tournaments/cup-renamed/") for p in paths),
   f"every game PATH moved too — they are derived from the dir ({paths[:2]})")

# ---- rename guards ----
st, r = req("/api/rename", {"tournament": "cup-renamed", "new_name": "cup-taken"})
ok(st == 400 and "already used" in r.get("error", ""),
   f"renaming ONTO a taken name is refused ({r.get('error','')[:60]!r})")
st, r = req("/api/rename", {"tournament": "nope", "new_name": "x"})
ok(st == 404, "renaming a tournament that does not exist is a 404")
st, r = req("/api/rename", {"tournament": "cup-renamed", "new_name": ""})
ok(st == 400, "an empty new_name is refused")

# a live job holding the name must block the rename — otherwise the job keeps
# writing into a directory that no longer exists under that name
with server.JOBS_LOCK:
    server.JOBS.append({"id": "j-live", "name": "cup-renamed", "state": "running",
                        "mode": "tournament", "log": []})
st, r = req("/api/rename", {"tournament": "cup-renamed", "new_name": "cup-later"})
ok(st == 400 and "cancel it first" in r.get("error", ""),
   f"a live job blocks the rename ({r.get('error','')[:70]!r})")
ok(server.name_conflict("cup-renamed"),
   "and that live job also makes the name unavailable to a NEW run")

srv.shutdown()
shutil.rmtree(TMP, ignore_errors=True)
print(f"FAILURES: {fails}")
sys.exit(1 if fails else 0)
