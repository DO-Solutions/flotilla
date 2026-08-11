#!/usr/bin/env python3
"""The one entrypoint: run any Flotilla game, series, or tournament from a config
JSON. Agent-first: read config-schema.json, write a config, run this. The dashboard's
Configure tab produces files for exactly this runner.

Config shape (all sections optional except mode + bots/participants):
{
  "mode": "match" | "series" | "tournament",
  "seed": 42,
  "outdir": "runs/my-run",
  "bots":         ["llm:<model-id>[:Label]" | "<scripted-bot>", ...],   # match/series
  "participants": ["llm:<model-id>[:Label]", ...],                      # tournament
  "scenario": { ...any keys from config-schema.json sections world/economy/combat/
                pacing/scenario... },
  "admirals": { "temperature": ..., "max_tokens": ..., "timeout_s": ...,
                "think": ... },   # omit any key to take the section default
  "series":   { "games": 3, "memos": true, "memo_history": true,
                "vary_seeds": false, "debrief_timeout_s": 300 },
  "tournament": { "format": "round_robin" | "random_pairs" | "single_elim",
                  "players_per_match": 2 | 4, "games_per_match": 1, "rounds": 1,
                  "memo_policy": "none" | "per_series" | "persistent" }
}

Defaults come from config-schema.json (the admirals section, not the values
shown above) — read it rather than assuming. `memo_history` (default true)
carries every past game's memo forward, game-numbered, into later prompts.

Run a paused job to completion with:  python3 run_config.py --resume <outdir>
(the outdir holds the checkpoint; a pipelined run keeps its longer fallback
timeout streak on resume).
"""
import argparse
import itertools
import json
import os
import random
import sys
import threading
import time

from .llm import LLMAdmiral                # noqa: E402
from . import contract                     # noqa: E402

# the GAME's pieces, bound at registration (engine/contract.py): the rules
# engine, the scripted bots, the knob schema, the fog digest. Bound as module
# globals so the orchestration below reads exactly as it always has.
Engine = BOTS = digest_for = config_schema = SCHEMA = None


@contract.on_set
def _bind(game):
    global Engine, BOTS, digest_for, config_schema, SCHEMA
    Engine = game.engine
    BOTS = game.bots
    digest_for = game.digest_for
    config_schema = game.schema
    SCHEMA = game.schema.SCHEMA


def section_defaults(name):
    return {k: s["d"] for k, s in SCHEMA[name].items()}


def make_bot(spec, adm):
    """spec: "llm:<model>[:Label]" | "<scripted>" | a dict for per-player config:
    {"model": <id or scripted name>, "label"?, "prompt"?, and any admirals-section
    override (temperature/max_tokens/timeout_s/think/history_chars/memo_chars)}."""
    if isinstance(spec, dict):
        model = str(spec.get("model", ""))
        if model.startswith("llm:"):
            model = model.split(":", 2)[1]
        if model in BOTS:
            return BOTS[model]
        a = {**adm, **{k: spec[k] for k in
                       ("temperature", "max_tokens", "timeout_s", "think",
                        "history_chars", "memo_chars", "scratchpad",
                        "scratchpad_chars", "warmup_timeout_s", "base_prompt")
                       if k in spec}}
        return LLMAdmiral(model, label=spec.get("label") or None,
                          temperature=a["temperature"], max_tokens=a["max_tokens"],
                          timeout=a["timeout_s"], think=a["think"],
                          history_chars=a["history_chars"], memo_chars=a["memo_chars"],
                          prompt=str(spec.get("prompt", ""))[:a["memo_chars"]],
                          scratchpad=a["scratchpad"],
                          scratchpad_chars=a["scratchpad_chars"],
                          warmup_timeout_s=a["warmup_timeout_s"],
                          base_prompt=a.get("base_prompt", ""))
    if spec.startswith("llm:"):
        parts = spec.split(":", 2)
        return LLMAdmiral(parts[1], label=parts[2] if len(parts) > 2 else None,
                          temperature=adm["temperature"], max_tokens=adm["max_tokens"],
                          timeout=adm["timeout_s"], think=adm["think"],
                          history_chars=adm["history_chars"],
                          memo_chars=adm["memo_chars"],
                          scratchpad=adm["scratchpad"],
                          scratchpad_chars=adm["scratchpad_chars"],
                          warmup_timeout_s=adm["warmup_timeout_s"],
                          base_prompt=adm.get("base_prompt", ""))
    return BOTS[spec]


def spec_name(spec):
    if isinstance(spec, dict):
        label = spec.get("label")
        if label:
            return str(label)
        m = str(spec.get("model", "bot"))
        return m.split(":", 2)[1] if m.startswith("llm:") else m
    if spec.startswith("llm:"):
        return spec.split(":")[2] if spec.count(":") == 2 else spec.split(":")[1]
    return spec


def dedupe(names):
    from collections import Counter
    dupes = {n for n, c in Counter(names).items() if c > 1}
    seen = {}
    out = []
    for n in names:
        if n in dupes:
            seen[n] = seen.get(n, -1) + 1
            n = f"{n}-{chr(65 + seen[n])}"
        out.append(n)
    return out


def bot_provenance(name, b):
    """The EXACT resolved settings this player ran with — a stranger's install
    (different local defaults) can rebuild the identical player from this."""
    if isinstance(b, LLMAdmiral):
        d = dict(label=name, model=b.model_id, temperature=b.temperature,
                 max_tokens=b.max_tokens, timeout_s=b.timeout, think=b.think,
                 history_chars=b.history_chars, memo_chars=b.memo_chars,
                 scratchpad=b.scratchpad_on, scratchpad_chars=b.scratchpad_chars,
                 warmup_timeout_s=b.warmup_timeout)
        if b.custom_prompt:
            d["prompt"] = b.custom_prompt
        if getattr(b, "base_prompt_text", ""):
            d["base_prompt"] = b.base_prompt_text
        return d
    return dict(label=name, model=getattr(b, "name", str(b)), scripted=True)


def merged_scenario(cfg):
    """ONE flat resolve for the replay stamp: schema knobs are a flat unique
    namespace, so folding admirals/series/tournament overrides into the
    engine's scenario makes meta.config carry the ACTUAL values for EVERY
    schema knob — not defaults that lie whenever a section was overridden."""
    return {**(cfg.get("scenario") or {}), **cfg.get("admirals", {}),
            **cfg.get("series", {}), **cfg.get("tournament", {})}


# protocol lines (the "winner"/"memos_saved"/… JSON the server and aux agent
# parse off stdout) as ONE atomic write — parallel tournament matchups print
# from worker threads, and print()'s separate payload+newline writes can
# interleave mid-line under load
_EMIT_LOCK = threading.Lock()


def _emit(obj):
    with _EMIT_LOCK:
        sys.stdout.write(json.dumps(obj) + "\n")
        sys.stdout.flush()


def play_game(named_bots, seed, scenario, outpath, memos_after=None, prov=None,
              pause_check=None, resume_engine=None, game_no=None,
              games_total=None, live=True):
    if resume_engine is None:
        for _, b in named_bots:
            if isinstance(b, LLMAdmiral):
                b._last_thoughts = []
                b.pad = ""                 # per-game working memory; memos carry over
                b.plan_text = ""
                b._trunc_note = False      # game-N feedback must not open game
                b._miss_note = None        # N+1 with a stale accusation
        eng = Engine(named_bots, seed=seed, scenario=scenario)
        if prov is not None:
            eng.provenance = dict(prov, game_seed=seed,
                                  players=[bot_provenance(n, b)
                                           for n, b in named_bots])
    else:
        eng = resume_engine                # a thawed checkpoint mid-game
    # parallel tournament matchups pass live=False: they'd otherwise all
    # truncate + interleave the ONE live.jsonl and corrupt the stream
    live_path = os.environ.get("FLOTILLA_LIVE") if live else None
    lfh = None
    if live_path:
        # live stream: header + one JSON line per window ("w" truncates = new game;
        # readers detect the reset by their offset exceeding the file size).
        # A resumed game RE-EMITS its header: on an aux thaw the file is fresh
        # on a NEW box and the flagship stream still holds the old game —
        # without a header the remainder glues onto the wrong game's timeline.
        # Viewers reset on the header and watch the remainder live; the full
        # replay lands complete at game end regardless.
        lfh = open(live_path, "a" if resume_engine is not None else "w")
        hdr = eng.live_header()
        if game_no:                        # viewers title 'game X of Y' off the
            hdr["game"] = game_no          # header itself — the poller's guess
            hdr["total"] = games_total     # mislabeled the between-games gap
        lfh.write(json.dumps(hdr, separators=(",", ":")) + "\n")
        lfh.flush()
        if resume_engine is not None:
            # CATCH-UP: re-ship the whole game so far — a thawed stream
            # otherwise starts mid-game with a blank timeline. CHUNKED into
            # bounded lines: a single mega-flush line proved undeliverable
            # (post limits + the /api/live complete-line reader both choke).
            CH = 400
            ei = di = 0
            for fi in range(0, len(eng.frames), CH):
                frames = eng.frames[fi:fi + CH]
                t_hi = frames[-1]["t"]
                ej = next((k for k in range(ei, len(eng.events))
                           if eng.events[k]["t"] > t_hi), len(eng.events))
                dj = next((k for k in range(di, len(eng.decisions))
                           if eng.decisions[k]["t"] > t_hi), len(eng.decisions))
                lfh.write(json.dumps(
                    dict(t=t_hi, final=False, frames=frames,
                         events=eng.events[ei:ej],
                         decisions=eng.decisions[di:dj],
                         scores={f.id: f.score()
                                 for f in eng.fleets.values()}),
                    separators=(",", ":")) + "\n")
                ei, di = ej, dj
            if ei < len(eng.events) or di < len(eng.decisions):
                # events recorded ABOVE the last frame's t (the ticks right
                # before the pause) otherwise never reach live viewers — the
                # next flush starts past them
                lfh.write(json.dumps(
                    dict(t=eng.t, final=False, frames=[],
                         events=eng.events[ei:], decisions=eng.decisions[di:],
                         scores={f.id: f.score()
                                 for f in eng.fleets.values()}),
                    separators=(",", ":")) + "\n")
            lfh.flush()

        def _sink(payload):
            lfh.write(json.dumps(payload, separators=(",", ":")) + "\n")
            lfh.flush()
        eng.live = _sink
    result = eng.run(pause_check=pause_check)
    if lfh:
        lfh.close()
    if result is None:                     # pause requested: hand back the engine
        return "PAUSED", eng, None
    replay = eng.replay(result)
    if memos_after:
        replay["memos"] = memos_after
    with open(outpath, "w") as fh:
        fh.write(json.dumps(replay, separators=(",", ":")))
    row = {"seed": seed, "file": outpath,
           "scores": {result["names"][k]: v for k, v in result["scores"].items()},
           "winner": result["names"][result["winner"]]}
    _emit(row)
    return replay, result, row


def make_pause_check(outdir):
    flag = os.path.join(outdir, "pause.flag")
    return lambda: os.path.exists(flag)


def write_checkpoint(outdir, payload):
    """Freeze a run mid-game as PLAIN JSON: the engine's mutable state
    (Engine.freeze), the bots (constructor spec from provenance + their
    mutable state), and the series loop state. JSON — not pickle — so a
    resume on ANY code version rehydrates tolerantly (missing fields keep
    current defaults) and no side ever deserializes executable state.
    Atomic; the pause flag is consumed so a later resume doesn't instantly
    re-pause."""
    eng = payload["engine"]
    named = [(f.name, f.bot) for f in eng.fleets.values()]
    data = dict(payload, ckpt_version=Engine.CKPT_VERSION,
                engine=eng.freeze(),
                bots=[dict(spec=bot_provenance(n, b),
                           state=b.freeze_state()
                           if isinstance(b, LLMAdmiral) else None)
                      for n, b in named])
    if getattr(eng, "pause_reason", None):
        # engine-initiated pause (api-outage circuit breaker): the server's
        # auto-resume loop keys off this field to probe the run back to life
        data["auto_pause"] = dict(reason=eng.pause_reason, at=time.time())
    else:
        # payloads built from an old checkpoint (dict(ck, engine=…)) can carry
        # a STALE auto_pause — left in place it makes the server auto-resume
        # a run the operator deliberately paused
        data.pop("auto_pause", None)
    tmp = os.path.join(outdir, "checkpoint.json.tmp")
    with open(tmp, "w") as fh:
        json.dump(data, fh, separators=(",", ":"))
    os.replace(tmp, os.path.join(outdir, "checkpoint.json"))
    try:
        os.remove(os.path.join(outdir, "pause.flag"))
    except OSError:
        pass


def debrief_all(named_bots, replay, game_no, total, timeout_s, full_info=False):
    memos = {}
    for fid, (name, b) in enumerate(named_bots):
        if not isinstance(b, LLMAdmiral):
            continue
        keep = b.timeout
        try:
            b.timeout = timeout_s          # debrief() floors this at 300
            dg = digest_for(replay, fid, game_no, total, full_info=full_info)
            out = b.debrief(dg)
        finally:
            b.timeout = keep
        memos[name] = {"memo": out["memo"], "err": out["err"]}
        _emit({"debrief": name, "after_game": game_no, "err": out["err"]})
    return memos


def run_series(named_bots, seed, scenario, ser, outdir, label="series", prov=None,
               pause_check=None, resume=None, live=True):
    os.makedirs(outdir, exist_ok=True)
    games = list(resume["rows"]) if resume else []
    final_memos = {}
    start_g = resume["game"] if resume else 1
    for g in range(start_g, ser["games"] + 1):
        gseed = seed + (g - 1 if ser["vary_seeds"] else 0)
        gpath = os.path.join(outdir, f"g{g}.json")
        replay, result, row = play_game(
            named_bots, gseed, scenario, gpath, prov=prov,
            pause_check=pause_check,
            resume_engine=resume.get("engine") if (resume and g == start_g) else None,
            game_no=g, games_total=ser["games"], live=live)
        if replay == "PAUSED":
            write_checkpoint(outdir, dict(
                kind="series", engine=result, game=g, rows=games, ser=ser,
                scenario=scenario, prov=prov, seed=seed))
            print(json.dumps({"paused": True, "game": g, "t": result.t,
                              "reason": getattr(result, "pause_reason",
                                                None)}), flush=True)
            sys.exit(75)
        row["game"] = g
        # every game gets a debrief, INCLUDING the last: the end-of-series review
        # is the memo a future self (rematch, next bracket) picks up
        if ser["memos"]:
            pre = {n: b.notes for n, b in named_bots
                   if isinstance(b, LLMAdmiral)}
            memos = debrief_all(named_bots, replay, g, ser["games"],
                                ser["debrief_timeout_s"],
                                full_info=ser.get("debrief_full_info", False))
            replay["memos"] = memos
            if ser.get("memo_history", True):
                # the memo an admiral carries is the WHOLE series log, not just
                # the last entry — a betrayal in game 1 stays on the record in
                # game 4's plans and debriefs (Kimi kept rediscovering Opus5's
                # lies from scratch every game, 2026-08-09)
                for n, b in named_bots:
                    if not isinstance(b, LLMAdmiral):
                        continue
                    if (memos.get(n) or {}).get("err"):
                        continue    # failed debrief: no memo — never append
                    m = (memos.get(n) or {}).get("memo") or ""
                    if m:
                        hdr = (f"— your memo after game {g} of "
                               f"{ser['games']} —")
                        b.notes = ((pre[n] + "\n\n") if pre.get(n) else "") \
                            + hdr + "\n" + m
            with open(gpath, "w") as fh:
                fh.write(json.dumps(replay, separators=(",", ":")))
            # the marker MUST print after the file gains its memos — the aux
            # agent resends on this line. (The old trigger was the "debrief"
            # lines, which all print BEFORE the write: memos reached the
            # flagship a full game late, and any worker churn in that window
            # lost them for good — that's where g1's and g3's memos went.)
            _emit({"memos_saved": g, "file": gpath})
            if g == ser["games"]:
                final_memos = memos
        games.append(row)
        # early clinch (tournaments, unless full_series): stop once the
        # matchup can no longer be won OR TIED — the leader has more wins than
        # anyone else could reach even winning every remaining game. Standalone
        # series never set `clinch`, so they always play out (memo sample).
        if ser.get("clinch"):
            wins = {}
            for r in games:
                wins[r["winner"]] = wins.get(r["winner"], 0) + 1
            remaining = ser["games"] - g
            ranked = sorted(wins.values(), reverse=True) or [0]
            lead = ranked[0]
            second = ranked[1] if len(ranked) > 1 else 0
            if lead > second + remaining:
                _emit({"clinched": True, "after_game": g,
                       "games": ser["games"]})
                break
    # end of series: ask the admirals, as playtesters, how to improve the game
    sim_feedback = {}
    if ser.get("sim_feedback") and replay != "PAUSED":
        for fid, (name, b) in enumerate(named_bots):
            if not isinstance(b, LLMAdmiral):
                continue
            keep = b.timeout
            try:
                b.timeout = ser["debrief_timeout_s"]
                dg = digest_for(replay, fid, ser["games"], ser["games"],
                                full_info=ser.get("debrief_full_info", False))
                fb = b.feedback(dg)
            finally:
                b.timeout = keep
            sim_feedback[name] = {"feedback": fb.get("feedback", ""),
                                  "err": fb.get("err")}
            _emit({"sim_feedback": name, "err": fb.get("err")})
    with open(os.path.join(outdir, "series.json"), "w") as fh:
        json.dump({"games": [dict(game=r["game"], seed=r["seed"],
                                  file=os.path.basename(r["file"]),
                                  winner=r["winner"]) for r in games],
                   "memos": final_memos,
                   "sim_feedback": sim_feedback}, fh, indent=1)
    return games


def matchup_winner(rows, names):
    wins = {n: sum(1 for r in rows if r["winner"] == n) for n in names}
    totals = {n: sum(r["scores"].get(n, 0) for r in rows) for n in names}
    return max(names, key=lambda n: (wins[n], totals[n]))


def run_tournament(cfg, adm, scenario, outdir, prov=None):
    t = config_schema.section_resolve("tournament", cfg.get("tournament"))
    ser_defaults = {**section_defaults("series"),
                    "games": t["games_per_match"],
                    **{k: v for k, v in cfg.get("series", {}).items()
                       if k in ("vary_seeds", "debrief_timeout_s",
                                "memo_history", "debrief_full_info")}}
    ser_defaults["memos"] = t["memo_policy"] != "none"
    # default: a matchup stops once it's mathematically decided; "full_series"
    # plays every game out regardless
    ser_defaults["clinch"] = not t.get("full_series", False)
    specs = cfg["participants"]
    names = dedupe([spec_name(s) for s in specs])
    bots = {n: make_bot(s, adm) for n, s in zip(names, specs)}
    rng = random.Random(cfg.get("seed", 42))
    ppm = 2 if t["format"] == "single_elim" else int(t["players_per_match"])

    if t["format"] == "round_robin":
        schedule = [list(c) for c in itertools.combinations(names, ppm)]
        rounds = [schedule]
    elif t["format"] == "random_pairs":
        rounds = []
        for _ in range(int(t["rounds"])):
            order = names[:]
            rng.shuffle(order)
            rounds.append([order[i:i + ppm] for i in range(0, len(order) - ppm + 1, ppm)])
    else:                                              # single_elim
        n = len(names)
        if n & (n - 1):
            raise SystemExit("single_elim needs a power-of-2 participant count")
        order = names[:]
        rng.shuffle(order)
        rounds = "ELIM"

    os.makedirs(outdir, exist_ok=True)
    matchups = []
    standings = {n: {"wins": 0, "games": 0, "score": 0} for n in names}
    seed0 = int(cfg.get("seed", 42))
    midx = 0

    # parallel matchups (tournament.parallel > 1): up to N matchups run in
    # worker threads. Each parallel matchup gets FRESH admiral instances (the
    # shared-bot mind would race), so persistent memos force sequential.
    par = max(1, int(t.get("parallel", 1)))
    stag = max(0, int(t.get("stagger_s", 480)))
    if par > 1 and t["memo_policy"] == "persistent":
        _emit({"warning": "memo_policy=persistent carries one mind across "
                          "matchups — parallel forced back to sequential"})
        par = 1
    spec_of = dict(zip(names, specs))
    MU_LOCK = threading.Lock()             # matchups/standings/tournament.json
    _gate = {"next": 0.0}
    _gate_lock = threading.Lock()

    def _stagger():
        # successive parallel STARTS at least stagger_s apart, so concurrent
        # games don't slam the model APIs in the same instant
        with _gate_lock:
            now = time.monotonic()
            wait = max(0.0, _gate["next"] - now)
            _gate["next"] = max(now, _gate["next"]) + stag
        if wait:
            time.sleep(wait)

    def play_matchup(group, rnd, midx, fresh=False):
        # group entries are operator-supplied labels — strip anything that
        # could escape outdir (a label of ../../tmp/x otherwise wrote there)
        import re as _re
        tag = _re.sub(r"[^A-Za-z0-9_-]", "", "_v_".join(group))[:60]
        mdir = os.path.join(outdir, f"m{midx:02d}_" + tag)
        if fresh:
            mbots = {n: make_bot(spec_of[n], adm) for n in group}
        else:
            mbots = bots
            if t["memo_policy"] == "per_series":
                for n in group:
                    if isinstance(bots[n], LLMAdmiral):
                        bots[n].notes = ""
        named = [(n, mbots[n]) for n in group]
        ser = dict(ser_defaults)
        # run_series now debriefs after EVERY game incl. the last, so persistent
        # memo carry-over needs no extra pass — notes simply survive on the bot
        rows = run_series(named, seed0 + midx * 1000, scenario, ser, mdir,
                          prov=dict(prov or {}, matchup=os.path.basename(mdir)),
                          live=not fresh)
        w = matchup_winner(rows, group)
        with MU_LOCK:
            for n in group:
                standings[n]["games"] += len(rows)
                standings[n]["wins"] += sum(1 for r in rows if r["winner"] == n)
                standings[n]["score"] += sum(r["scores"].get(n, 0) for r in rows)
            matchups.append({"round": rnd, "players": group,
                             "dir": os.path.basename(mdir),
                             "games": [dict(game=r["game"], seed=r["seed"],
                                            file=os.path.relpath(r["file"], outdir),
                                            scores=r["scores"], winner=r["winner"])
                                       for r in rows],
                             "winner": w})
            # incremental bracket: spectators (and the aux callback stream)
            # follow the tournament as it runs, not just after the last matchup
            with open(os.path.join(outdir, "tournament.json"), "w") as fh:
                json.dump({"config": cfg, "matchups": matchups,
                           "standings": standings, "partial": True}, fh, indent=1)
        return w

    def play_round(groups, rnd):
        """One round's matchups — the sequential classic path, or a thread
        pool of `parallel` workers with staggered starts. Returns winners in
        group order (single_elim pairs the next round off this)."""
        nonlocal midx
        if par <= 1 or len(groups) <= 1:
            out = []
            for g in groups:
                midx += 1
                out.append(play_matchup(g, rnd, midx))
            return out
        import concurrent.futures as _cf

        def _task(g, mi):
            _stagger()
            return play_matchup(g, rnd, mi, fresh=True)

        with _cf.ThreadPoolExecutor(max_workers=par) as ex:
            futs = []
            for g in groups:
                midx += 1
                futs.append(ex.submit(_task, g, midx))
            return [f.result() for f in futs]

    if rounds == "ELIM":
        alive = order
        rnd = 0
        while len(alive) > 1:
            rnd += 1
            pairs = [[alive[i], alive[i + 1]] for i in range(0, len(alive), 2)]
            alive = play_round(pairs, rnd)   # round barrier: winners pair next
        champion = alive[0]
    else:
        if not any(groups for groups in rounds):
            # e.g. players_per_match > participant count: zero matchups would
            # "complete" and crown an arbitrary champion nobody played for
            raise SystemExit(
                f"tournament shape yields ZERO matchups ({len(names)} "
                f"participants, {ppm} per match) — fix players_per_match")
        for rnd, groups in enumerate(rounds, 1):
            play_round(groups, rnd)
        played = [n for n in names if standings[n]["games"] > 0]
        if not played:
            raise SystemExit("no games were played — refusing to name a champion")
        if len(played) < len(names):
            print(json.dumps({"warning": "byes: these participants were never "
                              "scheduled", "benched":
                              sorted(set(names) - set(played))}), flush=True)
        champion = max(played, key=lambda n: (standings[n]["wins"],
                                              standings[n]["score"]))

    tj = {"config": cfg, "matchups": matchups, "standings": standings,
          "champion": champion,
          "memos_final": {n: bots[n].notes for n in names
                          if isinstance(bots[n], LLMAdmiral) and bots[n].notes}}
    with open(os.path.join(outdir, "tournament.json"), "w") as fh:
        json.dump(tj, fh, indent=1)
    print(json.dumps({"tournament_done": True, "champion": champion,
                      "standings": standings}), flush=True)


def _pipelined_env(scenario):
    """The ONE place the pipelined flag is derived from a scenario — main()
    and resume_run both call it, so a resumed run can never lose it."""
    if int((scenario or {}).get("pipeline_depth", 0) or 0) > 0:
        os.environ["FLOTILLA_PIPELINED"] = "1"


def resume_run(outdir):
    """Continue a paused run from its checkpoint.json (spot-instance workflow:
    pause on interruption, thaw when capacity returns). Bots are REBUILT from
    the checkpoint's provenance specs on current code, their mutable state
    overlaid, then Engine.thaw resumes the interrupted game mid-tick-loop; the
    series loop continues after it. IMPORTANT: a failed resume must leave
    checkpoint.json on disk — it is only removed after the run COMPLETES."""
    with open(os.path.join(outdir, "checkpoint.json")) as fh:
        ck = json.load(fh)
    # mirror main()'s pipelined flag — a resumed pipelined run must keep the
    # longer timeout streak (it used to silently drop to the 3-strike default,
    # exactly in the runs where in-flight timeouts are most common)
    _pipelined_env(ck.get("scenario"))
    adm = section_defaults("admirals")
    named = []
    for b in ck["bots"]:
        spec = dict(b["spec"])
        name = spec.pop("label")
        spec.pop("scripted", None)
        bot = make_bot(spec, adm)
        if b.get("state") is not None and isinstance(bot, LLMAdmiral):
            bot.load_state(b["state"])
        named.append((name, bot))
    eng = Engine.thaw(ck["engine"], named)
    pc = make_pause_check(outdir)
    if ck["kind"] == "match":
        r = play_game(named, ck["seed"], ck["scenario"],
                      os.path.join(outdir, "match.json"), prov=ck["prov"],
                      pause_check=pc, resume_engine=eng,
                      game_no=1, games_total=1)
        if r[0] == "PAUSED":
            write_checkpoint(outdir, dict(ck, engine=r[1]))
            print(json.dumps({"paused": True, "t": r[1].t,
                              "reason": getattr(r[1], "pause_reason",
                                                None)}), flush=True)
            sys.exit(75)
    else:
        run_series(named, ck["seed"], ck["scenario"], ck["ser"], outdir,
                   prov=ck["prov"], pause_check=pc,
                   resume={**ck, "engine": eng})
    try:
        os.remove(os.path.join(outdir, "checkpoint.json"))
    except OSError:
        pass


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("config", help="path to a run-config JSON (see module "
                    "docstring), or with --resume: the outdir of a paused run")
    ap.add_argument("--resume", action="store_true",
                    help="continue a paused run from its checkpoint.json")
    args = ap.parse_args()
    if args.resume:
        resume_run(args.config)
        return
    cfg = json.load(open(args.config))
    # the provider ladder uses a LONGER timeout streak when windows pipeline
    # (in-flight calls make timeouts cheaper) — flag it for sim/providers.py
    _pipelined_env(merged_scenario(cfg))
    run(cfg)


def run(cfg):
    """Run a config dict — the ONE series/match/tournament runner (series.py's
    CLI delegates here so there is exactly one set of semantics)."""
    adm = config_schema.section_resolve("admirals", cfg.get("admirals"))
    scenario = merged_scenario(cfg)
    outdir = cfg.get("outdir", "run-out")
    mode = cfg.get("mode", "match")
    seed = int(cfg.get("seed", 42))
    prov = dict(mode=mode, base_seed=seed)

    if mode == "tournament":
        run_tournament(cfg, adm, scenario, outdir, prov=prov)
        return
    specs = cfg["bots"]
    names = dedupe([spec_name(s) for s in specs])
    named = [(n, make_bot(s, adm)) for n, s in zip(names, specs)]
    os.makedirs(outdir, exist_ok=True)
    pc = make_pause_check(outdir)
    if mode == "match":
        r = play_game(named, seed, scenario, os.path.join(outdir, "match.json"),
                      prov=prov, pause_check=pc, game_no=1, games_total=1)
        if r[0] == "PAUSED":
            write_checkpoint(outdir, dict(kind="match", engine=r[1], seed=seed,
                                          scenario=scenario, prov=prov))
            print(json.dumps({"paused": True, "t": r[1].t}), flush=True)
            sys.exit(75)
    elif mode == "series":
        ser = config_schema.section_resolve("series", cfg.get("series"))
        cont = cfg.get("continue")
        resume = None
        if cont:
            # game-boundary continuation: no checkpoint needed. `rows` are the
            # already-played games' result rows (kept verbatim in the final
            # series.json); `memos` restore each admiral's series memory.
            # CONTRACT: with memo_history on, pass the admiral's FULL notes
            # log (headers included), not just the last game's memo — this
            # value lands in b.notes verbatim.
            for n, b in named:
                memo = (cont.get("memos") or {}).get(n)
                if memo and isinstance(b, LLMAdmiral):
                    b.notes = memo
            resume = dict(game=int(cont["game"]),
                          rows=list(cont.get("rows") or []))
        run_series(named, seed, scenario, ser, outdir, prov=prov,
                   pause_check=pc, resume=resume)
    else:
        raise SystemExit(f"unknown mode {mode!r}")


if __name__ == "__main__":
    main()
