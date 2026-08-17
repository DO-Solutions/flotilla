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

# the GAME's pieces, bound at registration (keelspring/contract.py): the rules
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


def mdir_name(group, midx):
    """m<NN>_<A>_v_<B> — group entries are operator-supplied labels, so strip
    anything that could escape outdir (a label of ../../tmp/x once wrote
    there)."""
    import re as _re
    tag = _re.sub(r"[^A-Za-z0-9_-]", "", "_v_".join(group))[:60]
    return f"m{midx:02d}_" + tag


def swiss_standings(names, matchups, before_round):
    """The table as it stood BEFORE `before_round`, built only from matchups
    recorded in EARLIER rounds.

    The "before" is the whole point. Pairing must never see a later round's
    results: a resumed tournament replays the pairing logic for rounds already
    played, and if it read the final table it would pair round 2 differently
    from the round 2 that actually happened — inventing matchups that contradict
    the record it just restored."""
    st = {n: {"series_wins": 0, "wins": 0, "score": 0} for n in names}
    for m in matchups:
        if int(m.get("round") or 0) >= before_round:
            continue
        w = m.get("winner")
        if w in st:
            st[w]["series_wins"] += 1
        for g in (m.get("games") or []):
            if g.get("winner") in st:
                st[g["winner"]]["wins"] += 1
            for n, sc in (g.get("scores") or {}).items():
                if n in st:
                    st[n]["score"] += sc
    return st


def swiss_pairs(names, matchups, rnd):
    """Pairings for Swiss round `rnd`: sort by record, pair down the table,
    avoid rematches, and hand any bye to the player who has had fewest.

    Deterministic by construction — the sort falls back to the name and there
    is no rng anywhere, so the same record always yields the same pairing. That
    is what makes a resumed run re-derive the rounds it already played instead
    of forking a different bracket.

    Swiss keeps everyone playing: nobody is knocked out, and after a couple of
    rounds the leaders are meeting each other rather than farming the tail."""
    st = swiss_standings(names, matchups, rnd)
    prior = [m for m in matchups if int(m.get("round") or 0) < rnd]
    met, appearances = set(), {n: 0 for n in names}
    for m in prior:
        ps = [p for p in (m.get("players") or []) if p in appearances]
        for i, a in enumerate(ps):
            appearances[a] += 1
            for b in ps[i + 1:]:
                met.add(frozenset((a, b)))
    # strongest first; name breaks every tie so the order is total
    order = sorted(names, key=lambda n: (-st[n]["series_wins"], -st[n]["wins"],
                                         -st[n]["score"], n))
    pool = list(order)
    if len(pool) % 2:
        # odd field: someone sits out. Give it to whoever has sat out least,
        # then to the weakest of those — the same instinct as the chess rule,
        # without inventing a free win for a series that was never sailed.
        byes = {n: (rnd - 1) - appearances[n] for n in names}
        rank = {n: i for i, n in enumerate(order)}
        sit = min(pool, key=lambda n: (byes[n], -rank[n], n))
        pool.remove(sit)
    pairs, used = [], set()
    for i, a in enumerate(pool):
        if a in used:
            continue
        opp = next((b for b in pool[i + 1:]
                    if b not in used and frozenset((a, b)) not in met), None)
        if opp is None:                    # everyone left has already been met:
            opp = next((b for b in pool[i + 1:] if b not in used), None)
        if opp is None:                    # a rematch beats not playing
            break
        used.add(a); used.add(opp)
        pairs.append([a, opp])
    return pairs

# Every top-level key a run config may carry. Knobs INSIDE the four schema
# sections are already validated by config_schema.resolve() — an unknown one
# raises "unknown config key 'x'". A whole unknown SECTION had no such check:
# merged_scenario() reads exactly scenario/admirals/series/tournament and
# everything else is silently ignored, so a config that says one thing runs as
# another with no error anywhere.
#
# That is not hypothetical. A cup was launched with pipeline_depth in a
# top-level "pacing" section (its real SCHEMA section, but not an envelope
# section); it was dropped, the run was never pipelined, and two days of a
# supposed A/B measured two identical configurations before anyone noticed.
CONFIG_TOP_KEYS = {
    "mode", "seed", "name", "outdir",           # identity + placement
    "bots", "participants",                     # the players
    "scenario", "admirals", "series", "tournament",   # the four schema bags
    "continue", "ack_cost",                     # run behaviour
    "executor", "aux_size", "public",           # server-side submit options
}


def validate_config(cfg):
    """Unknown top-level keys -> a list of human-readable complaints ([] = ok).

    Deliberately a hard error at the boundary rather than a warning in a log:
    the whole failure mode here is a config that LOOKS applied and is not, and
    a warning nobody reads is indistinguishable from the silence we already
    had."""
    if not isinstance(cfg, dict):
        return ["config must be a JSON object"]
    out = []
    # historic moments (moments.py): both of these fail LOUDLY at submit
    # rather than silently skipping at the end of an hours-long run
    ser = cfg.get("series") if isinstance(cfg.get("series"), dict) else {}
    tt = cfg.get("tournament") if isinstance(cfg.get("tournament"), dict) else {}
    if ser.get("historic_moments") and \
            not str(ser.get("historic_moments_model") or "").strip():
        out.append("series.historic_moments is on but historic_moments_model "
                   "is empty — set it (the dashboard injects the Server tab's "
                   "default narrator; a bare config must name one)")
    if tt.get("historic_moments") and not ser.get("historic_moments"):
        out.append("tournament.historic_moments synthesizes from the "
                   "per-series stories, so it requires "
                   "series.historic_moments=true as well")
    unknown = sorted(set(cfg) - CONFIG_TOP_KEYS)
    if not unknown:
        return out
    schema_sections = set(SCHEMA or {})
    for k in unknown:
        # the trap that actually bit: a real SCHEMA section name (pacing, world,
        # economy, combat) used as an envelope section. Say exactly where it goes.
        if k in schema_sections:
            out.append(
                f"'{k}' is a schema SECTION, not a config section — its knobs "
                f"go inside \"scenario\" (which carries every world/economy/"
                f"combat/pacing/scenario knob). As written it would be ignored "
                f"entirely.")
        else:
            out.append(f"unknown config key '{k}' — expected one of: "
                       + ", ".join(sorted(CONFIG_TOP_KEYS)))
    return out


def schedule_preview(cfg):
    """Every matchup a tournament WILL play — [{"dir", "players"}], computed
    exactly the way run_tournament builds its schedule (deterministic from
    cfg, same rng consumption order). Lets the bracket page show scheduled
    lanes before a single game lands. single_elim and swiss pair later rounds
    off results, so their schedules are unknowable up front: []."""
    if config_schema is None:
        contract.game()                    # bind the registered game's schema
    t = config_schema.section_resolve("tournament", cfg.get("tournament"))
    names = dedupe([spec_name(s) for s in cfg["participants"]])
    ppm = 2 if t["format"] in ("single_elim", "swiss") else int(t["players_per_match"])
    rng = random.Random(cfg.get("seed", 42))
    if t["format"] == "round_robin":
        rounds = [[list(c) for c in itertools.combinations(names, ppm)]]
    elif t["format"] == "random_pairs":
        rounds = []
        for _ in range(int(t["rounds"])):
            order = names[:]
            rng.shuffle(order)
            rounds.append([order[i:i + ppm]
                           for i in range(0, len(order) - ppm + 1, ppm)])
    else:
        return []
    out, mi = [], 0
    for rgroups in rounds:
        for g in rgroups:
            mi += 1
            out.append({"dir": mdir_name(g, mi), "players": list(g)})
    return out


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
    # live is a bool (the base stream) or a LANE NAME string: a parallel
    # tournament matchup gets its own live-<lane>.jsonl beside the base
    # file — the old behavior was live=False for parallel lanes, because
    # sharing the ONE live.jsonl truncated + interleaved the stream
    base_live = os.environ.get("FLOTILLA_LIVE")
    if isinstance(live, str) and base_live:
        live_path = os.path.join(os.path.dirname(base_live),
                                 f"live-{live}.jsonl")
    else:
        live_path = base_live if live else None
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
        if isinstance(live, str):
            hdr["lane"] = live             # which matchup this stream watches
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
           # winner None = a DRAW (the game's tie chain ran dry) — kept null
           # so nobody is credited a game win they didn't earn
           "winner": (result["names"][result["winner"]]
                      if result.get("winner") is not None else None)}
    if result.get("tiebreak"):
        row["tiebreak"] = (result["tiebreak"].get("decided_by")
                           or ("draw" if result["tiebreak"].get("draw")
                               else None))
    _emit(row)
    return replay, result, row


def make_pause_check(outdir):
    flag = os.path.join(outdir, "pause.flag")
    return lambda: os.path.exists(flag)


class SeriesPaused(Exception):
    """A tournament lane froze itself (operator pause or the api-outage
    breaker). By the time this is raised the lane's checkpoint is already
    durable in its matchup dir — the tournament catches it, freezes its
    sibling lanes, and embeds every lane checkpoint in its own."""

    def __init__(self, mdir, game, rows=None):
        super().__init__(f"lane paused at game {game}")
        self.mdir, self.game = mdir, game
        self.rows = rows or []      # completed-game rows: the fallback resume
                                    # path if the checkpoint itself is lost


def _clinched(rows, total):
    """True once the leader has more wins than anyone else could reach even
    winning every remaining game (a tie included — same rule as the in-loop
    check, extracted so a resumed lane can re-derive it from its game rows
    instead of trusting a counter)."""
    wins = {}
    for r in rows:
        if r["winner"] is not None:      # a drawn game credits nobody
            wins[r["winner"]] = wins.get(r["winner"], 0) + 1
    ranked = sorted(wins.values(), reverse=True) or [0]
    lead = ranked[0]
    second = ranked[1] if len(ranked) > 1 else 0
    return lead > second + (total - len(rows))


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
               pause_check=None, resume=None, live=True, pause_mode="exit"):
    os.makedirs(outdir, exist_ok=True)
    games = list(resume["rows"]) if resume else []
    final_memos = {}
    start_g = resume["game"] if resume else 1
    replay = None
    thaw_eng = resume.get("engine") if resume else None
    g = start_g
    # a lane rebuilt from its game rows (worker died with no checkpoint) may
    # already be decided — recompute clinch from the rows, never a counter,
    # or a thawed matchup replays a decided series
    if resume and ser.get("clinch") and games and _clinched(games, ser["games"]):
        _emit({"clinched": True, "after_game": len(games),
               "games": ser["games"], "on_resume": True})
        g = ser["games"] + 1
    while g <= ser["games"]:
        gseed = seed + (g - 1 if ser["vary_seeds"] else 0)
        gpath = os.path.join(outdir, f"g{g}.json")
        replay, result, row = play_game(
            named_bots, gseed, scenario, gpath, prov=prov,
            pause_check=pause_check,
            resume_engine=thaw_eng,
            game_no=g, games_total=ser["games"], live=live)
        thaw_eng = None
        if replay == "PAUSED":
            try:
                write_checkpoint(outdir, dict(
                    kind="series", engine=result, game=g, rows=games, ser=ser,
                    scenario=scenario, prov=prov, seed=seed))
            except OSError as e:
                # fail-safe, not fail-dead: losing a pause (a rotation, one
                # breaker trip) is cheap; exiting 75 with nothing on disk
                # loses the whole run. Refuse the pause and keep playing the
                # SAME game in-process.
                _emit({"pause_refused": True, "game": g,
                       "err": f"{type(e).__name__}: {e}"})
                try:
                    os.remove(os.path.join(outdir, "pause.flag"))
                except OSError:
                    pass
                thaw_eng = result
                continue
            if pause_mode == "raise":               # tournament lane: the
                raise SeriesPaused(outdir, g, games)  # caller freezes, never
                                                      # this process
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
        if ser.get("clinch") and _clinched(games, ser["games"]):
            _emit({"clinched": True, "after_game": g,
                   "games": ser["games"]})
            break
        g += 1
    # end of series: ask the admirals, as playtesters, how to improve the game
    sim_feedback = {}
    if ser.get("sim_feedback") and replay is not None:
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
    doc = {"games": [dict(game=r["game"], seed=r["seed"],
                          file=os.path.basename(r["file"]),
                          winner=r["winner"]) for r in games],
           "memos": final_memos,
           "sim_feedback": sim_feedback}
    if ser.get("historic_moments"):
        doc["historic_moments"] = _series_moments(named_bots, games, ser)
    with open(os.path.join(outdir, "series.json"), "w") as fh:
        json.dump(doc, fh, indent=1)
    return games


def _series_moments(named_bots, games, ser):
    """Narrate a finished series (keelspring/moments.py). Reads the games
    back off disk — narration is post-hoc by design, and it must NEVER take
    down a finished run: any failure is recorded in the output, not raised."""
    from . import moments
    try:
        replays = []
        for r in games:
            with open(r["file"]) as fh:
                replays.append(json.load(fh))
        players = [(n, fid) for fid, (n, b) in enumerate(named_bots)
                   if isinstance(b, LLMAdmiral)]
        return moments.narrate_series(
            replays, players,
            str(ser.get("historic_moments_model") or "").strip(),
            ser.get("historic_moments_timeout_s", 300),
            int(ser.get("historic_moments_chars", 2500)), emit=_emit)
    except Exception as e:
        return {"_meta": {"err": f"{type(e).__name__}: {e}"}}


def _tournament_moments(outdir, matchups, standings, champion, cfg):
    """The bracket-wide pass: synthesize each participant's tournament arc
    from the per-series stories already on disk (validate_config guarantees
    series.historic_moments was on). Same never-raise posture."""
    from . import moments
    ser = {**section_defaults("series"), **(cfg.get("series") or {})}
    sm = []
    for m in matchups:
        try:
            with open(os.path.join(outdir, m["dir"], "series.json")) as fh:
                sj = json.load(fh)
        except (OSError, ValueError):
            continue
        if sj.get("historic_moments"):
            sm.append({"dir": m["dir"], "players": m["players"],
                       "winner": m.get("winner"),
                       "moments": sj["historic_moments"]})
    try:
        return moments.narrate_tournament(
            sm, standings, champion,
            str(ser.get("historic_moments_model") or "").strip(),
            ser.get("historic_moments_timeout_s", 300),
            int(ser.get("historic_moments_chars", 2500)), emit=_emit)
    except Exception as e:
        return {"_meta": {"err": f"{type(e).__name__}: {e}"}}


def matchup_winner(rows, names):
    wins = {n: sum(1 for r in rows if r["winner"] == n) for n in names}
    totals = {n: sum(r["scores"].get(n, 0) for r in rows) for n in names}
    return max(names, key=lambda n: (wins[n], totals[n]))


def run_tournament(cfg, adm, scenario, outdir, prov=None, resume_ck=None):
    t = config_schema.section_resolve("tournament", cfg.get("tournament"))
    ser_defaults = {**section_defaults("series"),
                    "games": t["games_per_match"],
                    **{k: v for k, v in cfg.get("series", {}).items()
                       if k in ("vary_seeds", "debrief_timeout_s",
                                "memo_history", "debrief_full_info",
                                "historic_moments", "historic_moments_model",
                                "historic_moments_timeout_s",
                                "historic_moments_chars")}}
    ser_defaults["memos"] = t["memo_policy"] != "none"
    # default: a matchup stops once it's mathematically decided; "full_series"
    # plays every game out regardless
    ser_defaults["clinch"] = not t.get("full_series", False)
    specs = cfg["participants"]
    names = dedupe([spec_name(s) for s in specs])
    bots = {n: make_bot(s, adm) for n, s in zip(names, specs)}
    rng = random.Random(cfg.get("seed", 42))
    ppm = 2 if t["format"] in ("single_elim", "swiss") else int(t["players_per_match"])

    if t["format"] == "round_robin":
        schedule = [list(c) for c in itertools.combinations(names, ppm)]
        rounds = [schedule]
    elif t["format"] == "random_pairs":
        rounds = []
        for _ in range(int(t["rounds"])):
            order = names[:]
            rng.shuffle(order)
            rounds.append([order[i:i + ppm] for i in range(0, len(order) - ppm + 1, ppm)])
    elif t["format"] == "swiss":
        # pairings come from the standings before each round, so like ELIM the
        # bracket is discovered as it plays rather than laid out up front
        rounds = "SWISS"
    else:                                              # single_elim
        n = len(names)
        if n & (n - 1):
            raise SystemExit("single_elim needs a power-of-2 participant count")
        order = names[:]
        rng.shuffle(order)
        rounds = "ELIM"

    os.makedirs(outdir, exist_ok=True)
    # RESUME: the schedule above is rebuilt deterministically from cfg (the
    # rng is seeded from cfg.seed), so the checkpoint only needs the RESULTS —
    # completed matchup records, standings, frozen lanes, carried memos
    ck = resume_ck or {}
    matchups = list(ck.get("completed") or [])
    standings = ck.get("standings") or \
        {n: {"series_wins": 0, "wins": 0, "games": 0, "score": 0}
         for n in names}
    for st in standings.values():
        st.setdefault("series_wins", 0)
    for n, notes in (ck.get("bot_notes") or {}).items():
        if n in bots and isinstance(bots[n], LLMAdmiral):
            bots[n].notes = notes
    seed0 = int(cfg.get("seed", 42))
    midx = 0

    def _matchup_seed(mi):
        # map_set (default on): every matchup starts from the SAME base seed,
        # so vary_seeds walks identical maps — game N is the same water in
        # every lane and no pairing draws luckier islands than its rivals.
        # Off: each bracket slot derives its own seeds (the old behavior).
        return seed0 if t["map_set"] else seed0 + mi * 1000

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

    # pause plumbing: pc watches the operator flag (the aux command channel
    # writes outdir/pause.flag); pause_evt is the internal fan-in signal — the
    # moment ONE lane freezes, every sibling freezes at its next window
    pc = make_pause_check(outdir)
    pause_evt = threading.Event()

    def lane_pc():
        return pause_evt.is_set() or pc()

    # frozen-lane records for the checkpoint. On a resume this SEEDS with the
    # prior life's lanes — a re-freeze before they are thawed must carry them
    # forward, never write a checkpoint that silently drops a frozen lane
    PAUSED = [dict(L) for L in (ck.get("paused_lanes") or [])]
    PL_LOCK = threading.Lock()
    done_dirs = {m["dir"] for m in matchups}

    def _stagger():
        # successive parallel STARTS at least stagger_s apart, so concurrent
        # games don't slam the model APIs in the same instant
        with _gate_lock:
            now = time.monotonic()
            wait = max(0.0, _gate["next"] - now)
            _gate["next"] = max(now, _gate["next"]) + stag
        if wait:
            time.sleep(wait)

    _mdir_name = mdir_name                 # module helper (shared with the
                                           # server's submit-time preview)
    # the full matchup list, known up front for every format but single_elim —
    # rides every tournament.json write so the bracket page can show
    # scheduled lanes before their first game lands
    schedule_meta = []
    if rounds not in ("ELIM", "SWISS"):
        _mi = 0
        for _rg in rounds:
            for _g in _rg:
                _mi += 1
                schedule_meta.append({"dir": mdir_name(_g, _mi),
                                      "players": list(_g)})
    if not matchups:                       # fresh start: bracket-to-be
        with open(os.path.join(outdir, "tournament.json"), "w") as fh:
            json.dump({"config": cfg, "matchups": [], "standings": standings,
                       "schedule": schedule_meta, "partial": True},
                      fh, indent=1)

    def _record_matchup(group, rnd, midx, mdir, rows):
        w = matchup_winner(rows, group)
        with MU_LOCK:
            standings[w]["series_wins"] += 1
            for n in group:
                standings[n]["games"] += len(rows)
                standings[n]["wins"] += sum(1 for r in rows if r["winner"] == n)
                standings[n]["score"] += sum(r["scores"].get(n, 0) for r in rows)
            matchups.append({"round": rnd, "players": list(group),
                             "dir": os.path.basename(mdir),
                             "games": [dict(game=r["game"], seed=r["seed"],
                                            file=os.path.relpath(r["file"], outdir),
                                            scores=r["scores"], winner=r["winner"])
                                       for r in rows],
                             "winner": w})
            done_dirs.add(os.path.basename(mdir))
            # incremental bracket: spectators (and the aux callback stream)
            # follow the tournament as it runs, not just after the last matchup
            with open(os.path.join(outdir, "tournament.json"), "w") as fh:
                json.dump({"config": cfg, "matchups": matchups,
                           "standings": standings,
                           "schedule": schedule_meta, "partial": True},
                          fh, indent=1)
        return w

    def play_matchup(group, rnd, midx, fresh=False):
        mdir = os.path.join(outdir, _mdir_name(group, midx))
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
        try:
            rows = run_series(named, _matchup_seed(midx), scenario, ser, mdir,
                              prov=dict(prov or {},
                                        matchup=os.path.basename(mdir)),
                              # parallel lanes stream to their OWN
                              # live-<matchup>.jsonl; sequential keeps the
                              # base stream (the classic Live view)
                              live=os.path.basename(mdir) if fresh else True,
                              pause_check=lane_pc, pause_mode="raise")
        except SeriesPaused as sp:
            pause_evt.set()                # siblings freeze at their next window
            with PL_LOCK:
                PAUSED.append({"midx": midx, "round": rnd,
                               "group": list(group),
                               "dir": os.path.basename(mdir),
                               "game": sp.game, "fresh": fresh,
                               "rows": sp.rows})
            return None
        return _record_matchup(group, rnd, midx, mdir, rows)

    def resume_matchup(lane):
        """Thaw one frozen lane: from its embedded checkpoint (mid-game, no
        loss) or — if the checkpoint didn't survive — from its completed-game
        rows, dropping the one in-flight game. That bounded loss is the worst
        case per lane, never the run."""
        mdir = os.path.join(outdir, lane["dir"])
        os.makedirs(mdir, exist_ok=True)
        lck = lane.get("checkpoint")
        if lck is None:
            try:
                with open(os.path.join(mdir, "checkpoint.json")) as fh:
                    lck = json.load(fh)
            except (OSError, ValueError):
                lck = None
        if lck is not None:
            named = _thaw_bots(lck, adm)
            eng = Engine.thaw(lck["engine"], named)
            ser = lck.get("ser") or dict(ser_defaults)
            seed_l, scen_l = lck["seed"], lck["scenario"]
            prov_l = lck.get("prov")
            resume = {**lck, "engine": eng}
        else:
            _emit({"lane_checkpoint_lost": lane["dir"],
                   "resuming_after_game": len(lane.get("rows") or [])})
            named = [(n, make_bot(spec_of[n], adm)) for n in lane["group"]]
            ser = dict(ser_defaults)
            seed_l, scen_l = _matchup_seed(lane["midx"]), scenario
            prov_l = dict(prov or {}, matchup=lane["dir"])
            resume = {"rows": list(lane.get("rows") or []),
                      "game": len(lane.get("rows") or []) + 1, "engine": None}
        try:
            rows = run_series(named, seed_l, scen_l, ser, mdir, prov=prov_l,
                              live=lane["dir"] if lane.get("fresh") else True,
                              pause_check=lane_pc, pause_mode="raise",
                              resume=resume)
        except SeriesPaused as sp:
            pause_evt.set()
            with PL_LOCK:                  # REPLACE the seeded entry — a
                if lane in PAUSED:         # re-paused lane must not duplicate
                    PAUSED.remove(lane)
                PAUSED.append(dict(lane, game=sp.game, rows=sp.rows,
                                   checkpoint=None))
            return None
        with PL_LOCK:
            if lane in PAUSED:
                PAUSED.remove(lane)
        try:
            os.remove(os.path.join(mdir, "checkpoint.json"))
        except OSError:
            pass
        if t["memo_policy"] == "persistent":
            # the thawed bots carried the shared mind — hand it back
            for n, b in named:
                if n in bots and isinstance(bots[n], LLMAdmiral) \
                        and isinstance(b, LLMAdmiral):
                    bots[n].notes = b.notes
        return _record_matchup(lane["group"], lane["round"], lane["midx"],
                               mdir, rows)

    def _freeze():
        """Write the tournament checkpoint — every frozen lane's own
        checkpoint embedded, so ONE file ships to the flagship — and exit 75.
        Ordering rule: this runs only after every live lane has landed its
        own durable state, so the tournament checkpoint can never point at a
        lane that isn't on disk."""
        with PL_LOCK:
            plist = [dict(L) for L in PAUSED]
        auto = None
        for L in plist:
            if L.get("checkpoint") is None:
                try:
                    with open(os.path.join(outdir, L["dir"],
                                           "checkpoint.json")) as fh:
                        L["checkpoint"] = json.load(fh)
                except (OSError, ValueError):
                    L["checkpoint"] = None   # rows fallback at resume
            auto = auto or (L.get("checkpoint") or {}).get("auto_pause")
        data = {"kind": "tournament", "cfg": cfg, "completed": matchups,
                "standings": standings, "paused_lanes": plist}
        if t["memo_policy"] == "persistent":
            data["bot_notes"] = {n: b.notes for n, b in bots.items()
                                 if isinstance(b, LLMAdmiral) and b.notes}
        if auto:
            # a lane's outage-breaker pause promotes to the tournament: the
            # server's auto-resume prober keys off this field
            data["auto_pause"] = auto
        err = None
        for _ in range(3):
            try:
                tmp = os.path.join(outdir, "checkpoint.json.tmp")
                with open(tmp, "w") as fh:
                    json.dump(data, fh, separators=(",", ":"))
                os.replace(tmp, os.path.join(outdir, "checkpoint.json"))
                err = None
                break
            except OSError as e:
                err = e
                time.sleep(2)
        try:
            os.remove(os.path.join(outdir, "pause.flag"))
        except OSError:
            pass
        if err is not None:
            # last resort: the lane checkpoints are still durable in their
            # matchup dirs and every finished game already streamed home —
            # the library reconcile rebuilds the bracket from there
            _emit({"error": "tournament checkpoint write failed after "
                            f"retries ({type(err).__name__}: {err}); lane "
                            "checkpoints remain in their matchup dirs"})
        print(json.dumps({"paused": True, "tournament": True,
                          "matchups_done": len(matchups),
                          "lanes_frozen": len(plist)}), flush=True)
        sys.exit(75)

    def _maybe_freeze_between():
        """Between matchups there is nothing in flight: freeze cheaply — but
        only if a checkpoint is actually writable. Fail-safe, not fail-dead:
        a pause we cannot honor is refused (flag consumed, run continues),
        never answered with rc 75 and an empty disk."""
        if not (pc() or pause_evt.is_set()):
            return
        probe = os.path.join(outdir, ".ckpt-probe")
        try:
            with open(probe, "w") as fh:
                fh.write("1")
            os.remove(probe)
        except OSError as e:
            _emit({"pause_refused": True, "between_matchups": True,
                   "err": f"{type(e).__name__}: {e}"})
            pause_evt.clear()
            try:
                os.remove(os.path.join(outdir, "pause.flag"))
            except OSError:
                pass
            return
        _freeze()

    def play_round(groups, rnd):
        """One round's matchups — the sequential classic path, or a thread
        pool of `parallel` workers with staggered starts. Returns winners in
        group order (single_elim pairs the next round off this). Matchups
        already in the record (a resumed run) are skipped, their recorded
        winner returned."""
        nonlocal midx
        if par <= 1 or len(groups) <= 1:
            out = []
            for g in groups:
                midx += 1
                mname = _mdir_name(g, midx)
                if mname in done_dirs:
                    out.append(next(m["winner"] for m in matchups
                                    if m["dir"] == mname))
                    continue
                _maybe_freeze_between()
                out.append(play_matchup(g, rnd, midx))
                if pause_evt.is_set() or pc():
                    _freeze()              # the matchup above just froze
            return out
        import concurrent.futures as _cf

        def _task(g, mi):
            if lane_pc():
                return None                # pausing: start no new lanes
            _stagger()
            if lane_pc():
                return None
            return play_matchup(g, rnd, mi, fresh=True)

        with _cf.ThreadPoolExecutor(max_workers=par) as ex:
            futs, skipped = [], {}
            for i, g in enumerate(groups):
                midx += 1
                mname = _mdir_name(g, midx)
                if mname in done_dirs:
                    skipped[i] = next(m["winner"] for m in matchups
                                      if m["dir"] == mname)
                    continue
                futs.append(ex.submit(_task, g, midx))
            res = [f.result() for f in futs]
        out, ri = [], 0
        for i in range(len(groups)):
            if i in skipped:
                out.append(skipped[i])
            else:
                out.append(res[ri])
                ri += 1
        if pause_evt.is_set() or pc():
            _freeze()                      # fan-in: every lane has landed
        return out

    # a resumed run first finishes its frozen lanes (their results feed the
    # schedule walk below, which then skips them as recorded matchups)
    for lane in list(PAUSED):
        _maybe_freeze_between()
        resume_matchup(lane)
        if pause_evt.is_set() or pc():
            _freeze()

    if rounds == "ELIM":
        alive = order
        rnd = 0
        while len(alive) > 1:
            rnd += 1
            pairs = [[alive[i], alive[i + 1]] for i in range(0, len(alive), 2)]
            alive = play_round(pairs, rnd)   # round barrier: winners pair next
        champion = alive[0]
    elif rounds == "SWISS":
        # Every round is paired off the table as it stood BEFORE that round, so
        # a resumed run re-derives the rounds it already played (play_round then
        # skips them by name) instead of forking a different bracket. Nobody is
        # eliminated: the field stays whole and the leaders find each other.
        nrounds = max(1, int(t.get("rounds", 1) or 1))
        for rnd in range(1, nrounds + 1):
            pairs = swiss_pairs(names, matchups, rnd)
            if not pairs:                    # fewer than two players: nothing to play
                break
            play_round(pairs, rnd)
        played = [n for n in names if standings[n]["games"] > 0]
        if not played:
            raise SystemExit("no games were played — refusing to name a champion")
        # same rule as round_robin: series won first, game wins and score only
        # break ties (12 game wins across 4 lost series must not outrank 4-0)
        champion = max(played, key=lambda n: (standings[n]["series_wins"],
                                              standings[n]["wins"],
                                              standings[n]["score"]))
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
        # a round-robin is decided by SERIES won; game wins and score only break
        # ties (12 game wins across 4 lost series must not outrank 4-0)
        champion = max(played, key=lambda n: (standings[n]["series_wins"],
                                              standings[n]["wins"],
                                              standings[n]["score"]))

    tj = {"config": cfg, "matchups": matchups, "standings": standings,
          "champion": champion, "schedule": schedule_meta,
          "memos_final": {n: bots[n].notes for n in names
                          if isinstance(bots[n], LLMAdmiral) and bots[n].notes}}
    if t.get("historic_moments"):
        tj["historic_moments"] = _tournament_moments(
            outdir, matchups, standings, champion, cfg)
    with open(os.path.join(outdir, "tournament.json"), "w") as fh:
        json.dump(tj, fh, indent=1)
    print(json.dumps({"tournament_done": True, "champion": champion,
                      "standings": standings}), flush=True)


def _pipelined_env(scenario):
    """The ONE place the pipelined flag is derived from a scenario — main()
    and resume_run both call it, so a resumed run can never lose it."""
    if int((scenario or {}).get("pipeline_depth", 0) or 0) > 0:
        os.environ["FLOTILLA_PIPELINED"] = "1"


def _thaw_bots(ck, adm):
    """Rebuild a checkpoint's bots on CURRENT code: constructor spec from
    provenance, mutable state overlaid."""
    named = []
    for b in ck["bots"]:
        spec = dict(b["spec"])
        name = spec.pop("label")
        spec.pop("scripted", None)
        bot = make_bot(spec, adm)
        if b.get("state") is not None and isinstance(bot, LLMAdmiral):
            bot.load_state(b["state"])
        named.append((name, bot))
    return named


def resume_run(outdir):
    """Continue a paused run from its checkpoint.json (spot-instance workflow:
    pause on interruption, thaw when capacity returns). Bots are REBUILT from
    the checkpoint's provenance specs on current code, their mutable state
    overlaid, then Engine.thaw resumes the interrupted game mid-tick-loop; the
    series loop continues after it. IMPORTANT: a failed resume must leave
    checkpoint.json on disk — it is only removed after the run COMPLETES."""
    with open(os.path.join(outdir, "checkpoint.json")) as fh:
        ck = json.load(fh)
    if ck.get("kind") == "tournament":
        # the schedule is rebuilt deterministically from the embedded cfg;
        # completed matchups + frozen lanes come from the checkpoint
        cfg = ck["cfg"]
        _pipelined_env(merged_scenario(cfg))
        adm = config_schema.section_resolve("admirals", cfg.get("admirals"))
        prov = dict(mode="tournament", base_seed=int(cfg.get("seed", 42)))
        run_tournament(cfg, adm, merged_scenario(cfg), outdir, prov=prov,
                       resume_ck=ck)
        try:
            os.remove(os.path.join(outdir, "checkpoint.json"))
        except OSError:
            pass
        return
    # mirror main()'s pipelined flag — a resumed pipelined run must keep the
    # longer timeout streak (it used to silently drop to the 3-strike default,
    # exactly in the runs where in-flight timeouts are most common)
    _pipelined_env(ck.get("scenario"))
    adm = section_defaults("admirals")
    named = _thaw_bots(ck, adm)
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
    # every entry point lands here, so this is the one place a misspelled or
    # misplaced section can be caught for the CLI, the server, and aux workers
    problems = validate_config(cfg)
    if problems:
        raise SystemExit("config rejected:\n  - " + "\n  - ".join(problems))
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
