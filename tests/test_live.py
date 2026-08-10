#!/usr/bin/env python3
"""Live streaming: engine per-window flushes + the server's tail endpoint."""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "sim"))
from core import Engine, WINDOW          # noqa: E402

fails = 0


def ok(cond, msg):
    global fails
    print(("PASS " if cond else "FAIL ") + msg)
    if not cond:
        fails += 1


class Idle:
    name = "idle"

    def decide(self, summary, rng):
        return {}


# --- engine flushes: complete, ordered, lossless ---
eng = Engine([("A", Idle()), ("B", Idle())], seed=3, max_ticks=WINDOW * 4)
flushes = []
eng.live = flushes.append
hdr = eng.live_header()
ok(hdr["header"] and hdr["meta"]["w"] == eng.W and len(hdr["fleets"]) == 2
   and len(hdr["nodes"]) == len(eng.nodes), "header carries meta/fleets/nodes")
result = eng.run()
ok(len(flushes) >= 4 and flushes[-1]["final"],
   f"one flush per window + final ({len(flushes)})")
streamed = [f for p in flushes for f in p["frames"]]
ok(streamed == eng.frames, "streamed frames == replay frames, in order, lossless")
sev = [e for p in flushes for e in p["events"]]
ok(sev == eng.events, "streamed events lossless")
ok(all("scores" in p for p in flushes), "every flush carries live scores")
ok(json.dumps(hdr), "header is JSON-serializable")

# a broken sink must never hurt the sim
eng2 = Engine([("A", Idle()), ("B", Idle())], seed=3, max_ticks=WINDOW * 2)
eng2.live = lambda p: 1 / 0
r2 = eng2.run()
ok(r2["ticks"] == WINDOW * 2 and eng2.live is None,
   "broken sink detaches; the match completes")

# --- server tail endpoint ---
import tempfile
import threading
import urllib.request

TMP = tempfile.mkdtemp(prefix="flotilla-live-")
os.environ["FLOTILLA_LIBRARY"] = TMP
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import server                            # noqa: E402
from http.server import ThreadingHTTPServer  # noqa: E402

server.JOBS.append(dict(id="livejob", name="live-series", mode="series",
                        state="running", games_done=0, games_expected=3, log=[]))
wd = os.path.join(TMP, "_work", "livejob")
os.makedirs(wd)
lf = os.path.join(wd, "live.jsonl")
with open(lf, "w") as fh:
    fh.write(json.dumps(dict(hdr, header=True)) + "\n")
    fh.write(json.dumps(flushes[0]) + "\n")
    fh.write('{"partial": tru')                 # torn write: must not ship

srv = ThreadingHTTPServer(("127.0.0.1", 0), server.H)
threading.Thread(target=srv.serve_forever, daemon=True).start()
PORT = srv.server_address[1]


def get(path):
    with urllib.request.urlopen(f"http://127.0.0.1:{PORT}{path}", timeout=10) as r:
        return json.loads(r.read())


d = get("/api/live/livejob?ofs=0")
ok(len(d["lines"]) == 2 and d["lines"][0].get("header"),
   "tail returns complete lines only (torn line held back)")
ok(d["state"] == "running" and d["name"] == "live-series" and d["game"] == 1,
   "tail carries job state/name/game")
ofs = d["ofs"]
d2 = get(f"/api/live/livejob?ofs={ofs}")
ok(d2["lines"] == [] and d2["ofs"] == ofs, "caught up: no lines, stable offset")
with open(lf, "r+") as fh:
    fh.seek(ofs)
    fh.write(json.dumps(flushes[1]) + "\n")
    fh.truncate()
d3 = get(f"/api/live/livejob?ofs={ofs}")
ok(len(d3["lines"]) == 1 and d3["lines"][0]["t"] == flushes[1]["t"],
   "new flush picked up from the offset")
with open(lf, "w") as fh:                       # new game: file truncated
    fh.write(json.dumps(dict(hdr, header=True)) + "\n")
d4 = get(f"/api/live/livejob?ofs={d3['ofs']}")
ok(len(d4["lines"]) == 1 and d4["lines"][0].get("header"),
   "truncation (new game) resets the reader to the fresh header")
ok(get("/api/live/livejob?ofs=999999")["lines"][0].get("header"),
   "absurd offset also resets cleanly")

# --- 2 MB chunking + `more` + stream_game (wringer pass 2/3): a cold join
# on a big backlog must drain in bounded chunks, delivering every line
# exactly once in order, and carry the file's first-header game number ---
big_hdr = dict(hdr, header=True, game=2)
with open(lf, "w") as fh:
    fh.write(json.dumps(big_hdr) + "\n")
    pad = "x" * 6000
    for i in range(500):                          # ~3 MB total
        fh.write(json.dumps({"t": i, "pad": pad}) + "\n")
d5 = get("/api/live/livejob?ofs=0")
ok(d5.get("more") is True and d5["ofs"] < os.path.getsize(lf),
   "oversize backlog ships chunked with more=True")
ok(d5.get("stream_game") == 2,
   f"stream_game carries the first header's game ({d5.get('stream_game')})")
got = list(d5["lines"])
ofs5 = d5["ofs"]
for _ in range(10):
    dn = get(f"/api/live/livejob?ofs={ofs5}")
    got.extend(dn["lines"])
    ofs5 = dn["ofs"]
    if not dn.get("more"):
        break
ok(len(got) == 501 and got[0].get("header")
   and [l.get("t") for l in got[1:]] == list(range(500)),
   f"draining the chunks yields every line exactly once, in order "
   f"({len(got)} lines)")

try:
    get("/api/live/nope?ofs=0")
    ok(False, "unknown job -> 404")
except urllib.error.HTTPError as e:
    ok(e.code == 404, "unknown job -> 404")

srv.shutdown()
import shutil
shutil.rmtree(TMP, ignore_errors=True)
print("FAILURES:", fails)
sys.exit(1 if fails else 0)
