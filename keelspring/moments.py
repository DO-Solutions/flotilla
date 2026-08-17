#!/usr/bin/env python3
"""Historic Moments: post-run narration of each admiral's campaign.

One NARRATOR model — independent of who played — writes each LLM admiral's
story through a series (and, separately, across a whole tournament): key
decisions, turning points, and where the admiral's own words show it
surprised, frustrated, triumphant or resigned. The emotional read is grounded
in the admiral's OWN journal, memos and parley — never inferred from a
scoreboard alone.

THE ANCHOR RULE (anti-hallucination, and also the product): every beat must
cite a {game, tick} pair copied from the anchor list the narrator was shown.
A beat that cannot be anchored to a real recorded moment is dropped
mechanically before it ever renders — and an anchored beat renders as a link
into the replay at that tick, which is exactly the jump-to-the-moment tool a
video editor wants.

This is a sibling of debrief()/feedback() (see runner.run_series), not a new
subsystem: same transport (the provider ladder via LLMAdmiral._chat), same
usage accounting, same "an error is a recorded fact, not a crash" posture —
narration failing must never take down a finished run.
"""
import json

from .llm import LLMAdmiral, TruncatedReply

MAX_BEATS_SERIES = 10
MAX_BEATS_TOURNAMENT = 8
MAX_ANCHOR_LINES = 150                 # the citation vocabulary shown per player
MATERIAL_CHARS = 30000                 # cap on the whole user prompt
_TITLE_CAP, _NOTE_CAP, _EMOTION_CAP = 80, 280, 24


def _names_map(replay):
    n = replay["result"]["names"]
    return {int(k): v for k, v in n.items()}


def _ev_line(e, names):
    """One anchor line's description — a python cousin of the viewer's
    describeEvent, kept to the kinds worth citing."""
    nm = lambda fid: names.get(fid, f"fleet {fid}")   # noqa: E731
    k = e.get("k")
    if k == "flag_sunk":
        by = nm(e["by"]) + " destroyed " if e.get("by") is not None else ""
        return f"{by}{nm(e['fleet'])}'s flagship" + \
            ("" if by else " went down") + " — ELIMINATED"
    if k == "sink":
        cls = e.get("preset", "ship")
        if e.get("cause") == "scuttle":
            return f"{nm(e['fleet'])} scuttled a {cls}"
        if e.get("by") is not None:
            return f"{nm(e['by'])} sank {nm(e['fleet'])}'s {cls}"
        return f"{nm(e['fleet'])} lost a {cls}"
    if k == "region":
        if e.get("prev") is None:
            return f"{nm(e['fleet'])} claimed {e.get('name')}"
        return f"{nm(e['fleet'])} took {e.get('name')} from {nm(e['prev'])}"
    if k == "signal":
        return f"{nm(e['fleet'])} signalled return to port"
    if k == "parley":
        to = e.get("to")
        to = "all" if not isinstance(to, int) else names.get(to, f"fleet {to}")
        return f"{nm(e['fleet'])} → {to}: {str(e.get('text', ''))[:80]}"
    if k == "design":
        return f"{nm(e['fleet'])} designed the {e.get('name')}"
    if k == "yard_built":
        return f"{nm(e['fleet'])} opened a yard slot"
    return None


_ANCHOR_KINDS = ("flag_sunk", "sink", "region", "signal", "parley", "design",
                 "yard_built")


def anchors_for(replays):
    """{(game, tick)} + the prompt lines, across a series' replays. The set is
    the validator; the lines are the narrator's citation vocabulary — one and
    the same by construction, so 'cited but unlisted' cannot happen honestly."""
    keys, lines = set(), []
    for g, rp in enumerate(replays, 1):
        names = _names_map(rp)
        evs = [e for e in rp.get("events", []) if e.get("k") in _ANCHOR_KINDS]
        # keep every big beat; thin the chatter kinds evenly if over budget
        budget = max(10, MAX_ANCHOR_LINES // max(1, len(replays)))
        if len(evs) > budget:
            big = [e for e in evs if e["k"] in ("flag_sunk", "region")]
            rest = [e for e in evs if e["k"] not in ("flag_sunk", "region")]
            step = max(1, len(rest) // max(1, budget - len(big)))
            evs = sorted(big + rest[::step], key=lambda e: e["t"])
        for e in evs:
            d = _ev_line(e, names)
            if not d:
                continue
            keys.add((g, int(e["t"])))
            lines.append(f"[g{g} t{e['t']}] {d}")
    return keys, lines


def player_material(replays, fid, name, budget=MATERIAL_CHARS):
    """Everything the narrator may know about ONE admiral: results, its own
    thoughts (the campaign journal), its memos, and parley it was party to.
    Enemy thoughts never enter — the story is grounded in this admiral's own
    words plus the public record."""
    parts = []
    for g, rp in enumerate(replays, 1):
        names = _names_map(rp)
        res = rp["result"]
        scores = {names[int(k) if isinstance(k, str) else k]: v
                  for k, v in res["scores"].items()}
        winner = names.get(res.get("winner"), res.get("winner"))
        parts.append(f"=== GAME {g} — winner {winner}, scores "
                     f"{json.dumps(scores, sort_keys=True)} ===")
        own = [d for d in rp.get("decisions", [])
               if d.get("fleet") == fid and d.get("thoughts")]
        picks = own[:2] + own[-6:] if len(own) > 8 else own
        for d in picks:
            parts.append(f"[g{g} t{d['t']}] {name} thought: "
                         + str(d["thoughts"])[:400])
        for e in rp.get("events", []):
            if e.get("k") != "parley":
                continue
            if e.get("fleet") != fid and e.get("to") != fid:
                continue
            parts.append(f"[g{g} t{e['t']}] parley "
                         + (_ev_line(e, names) or ""))
        memo = ((rp.get("memos") or {}).get(name) or {}).get("memo")
        if memo:
            parts.append(f"--- {name}'s memo after game {g} ---\n"
                         + str(memo)[:900])
    out = "\n".join(parts)
    return out[:budget] + ("\n(…material trimmed at budget…)"
                           if len(out) > budget else "")


def validate_beats(beats, anchor_keys, max_beats, extra=None):
    """Drop every beat that does not cite a listed anchor — mechanically,
    before anything renders. Returns (kept, dropped_count). `extra` names an
    additional required key (tournament beats also cite their matchup)."""
    kept, dropped = [], 0
    for b in beats if isinstance(beats, list) else []:
        try:
            key = (int(b["game"]), int(b["tick"]))
        except (KeyError, TypeError, ValueError):
            dropped += 1
            continue
        ex = None
        if extra is not None:
            ex = str(b.get(extra, ""))
            key = (ex,) + key
        if key not in anchor_keys:
            dropped += 1
            continue
        beat = {"game": key[-2], "tick": key[-1],
                "title": str(b.get("title", ""))[:_TITLE_CAP],
                "note": str(b.get("note", ""))[:_NOTE_CAP]}
        emo = b.get("emotion")
        if emo:
            beat["emotion"] = str(emo)[:_EMOTION_CAP].lower()
        if extra is not None:
            beat[extra] = ex
        kept.append(beat)
        if len(kept) >= max_beats:
            break                          # over-cap beats are trimmed, not
                                           # counted as hallucinations
    return kept, dropped


_SYSTEM = """You are the fleet historian for FLOTILLA, a naval strategy game \
played by LLM admirals. You write one admiral's TRUE story from the match \
record — a spectator-facing arc with real turning points, not a scoreboard \
recap and not fiction.

HARD RULES:
- Ground every claim in the record you are given. The admiral's quoted \
thoughts, memos and parley are your only window into what it felt — call it \
surprised, frustrated, triumphant, resigned ONLY when its own words show it; \
otherwise omit the emotion.
- Every beat MUST copy its "game" and "tick" EXACTLY from one line of the \
ANCHORS list. A beat citing any other moment will be deleted.
- Write for a spectator who has not watched the match. Name ships, places \
and rivals as the record names them.

Reply with ONLY a JSON object:
{"story": "<the arc, 2-5 sentences>",
 "beats": [{"game": <int>, "tick": <int>, "title": "<≤10 words>",
            "note": "<1-2 sentences>", "emotion": "<word or null>"}]}"""


def _narrate_one(bot, sys_extra, material, chars, max_beats):
    """One narrator call → parsed JSON, with the same retry/truncation posture
    as debrief(). Returns (obj_or_None, usage_dict)."""
    msgs = [{"role": "system", "content": _SYSTEM + sys_extra
             + f"\nStory HARD LIMIT: {chars} characters. "
             f"At most {max_beats} beats — pick the ones that matter."},
            {"role": "user", "content": material}]
    tin = tout = ms = 0
    try:
        try:
            text, tin, tout, ms = bot._chat(msgs)
        except TruncatedReply as e:
            text, tin, tout, ms = e.text, e.tin, e.tout, e.ms
        except Exception:
            text, tin, tout, ms = bot._chat(msgs)      # one retry
        obj = bot._extract_json(text)
        cost = (tin * bot.price[0] + tout * bot.price[1]) / 1e6
        return obj, dict(tin=tin, tout=tout, ms=ms, cost=round(cost, 6),
                         err=None)
    except Exception as e:
        cost = (tin * bot.price[0] + tout * bot.price[1]) / 1e6
        return None, dict(tin=tin, tout=tout, ms=ms, cost=round(cost, 6),
                          err=f"{type(e).__name__}: {e}")


def narrator(model_id, timeout_s, chars):
    """A bare LLMAdmiral used as transport only — the provider ladder, retry
    and pricing come along; the admiral persona does not (we pass our own
    messages straight to _chat)."""
    b = LLMAdmiral(model_id)
    b.timeout = max(30, int(timeout_s))
    b.temperature = 0.7
    b.think = False
    # headroom past the story cap + the beat list; chars/3 ≈ tokens
    b.max_tokens = max(1200, min(6000, chars))
    return b


def narrate_series(replays, players, model_id, timeout_s, chars, emit=None):
    """players: [(name, fid)] for the LLM admirals. Returns the series'
    historic_moments dict: per-player {story, beats, dropped_beats, usage},
    plus _meta. Never raises — an error is recorded per player."""
    out = {"_meta": {"model": model_id, "cost": 0.0}}
    if not model_id:
        out["_meta"]["err"] = "no narrator model configured"
        return out
    keys, lines = anchors_for(replays)
    anchor_txt = "\n=== ANCHORS (cite game+tick from THESE lines only) ===\n" \
        + "\n".join(lines)
    bot = narrator(model_id, timeout_s, chars)
    for name, fid in players:
        material = (f"The admiral whose story you are writing: {name} "
                    f"(fleet {fid}).\n\n"
                    + player_material(replays, fid, name)
                    + anchor_txt)
        obj, usage = _narrate_one(bot, "", material, chars, MAX_BEATS_SERIES)
        rec = {"usage": usage}
        if obj is not None:
            rec["story"] = str(obj.get("story", ""))[:chars]
            beats, dropped = validate_beats(obj.get("beats"), keys,
                                            MAX_BEATS_SERIES)
            rec["beats"] = beats
            rec["dropped_beats"] = dropped
        out[name] = rec
        out["_meta"]["cost"] = round(out["_meta"]["cost"]
                                     + (usage.get("cost") or 0), 6)
        if emit:
            emit({"historic_moments": name, "err": usage.get("err"),
                  "beats": len(rec.get("beats", [])),
                  "dropped": rec.get("dropped_beats", 0)})
    return out


def narrate_tournament(series_moments, standings, champion, model_id,
                       timeout_s, chars, emit=None):
    """The bracket-wide arc per participant, SYNTHESIZED from the per-series
    stories (already anchored and validated — cheap and grounded, instead of
    re-reading every replay). series_moments: [{dir, players, winner,
    moments}] per matchup. Beats cite {matchup, game, tick} triples drawn
    from the series beats."""
    out = {"_meta": {"model": model_id, "cost": 0.0}}
    if not model_id:
        out["_meta"]["err"] = "no narrator model configured"
        return out
    participants = sorted(standings)
    bot = narrator(model_id, timeout_s, chars)
    for name in participants:
        keys, lines, stories = set(), [], []
        for m in series_moments:
            if name not in m.get("players", []):
                continue
            opp = " vs ".join(m["players"])
            w = m.get("winner")
            stories.append(f"=== MATCHUP {m['dir']} ({opp}) — "
                           f"series winner {w} ===")
            rec = (m.get("moments") or {}).get(name) or {}
            if rec.get("story"):
                stories.append(rec["story"])
            for b in rec.get("beats") or []:
                keys.add((m["dir"], int(b["game"]), int(b["tick"])))
                lines.append(f"[matchup {m['dir']} g{b['game']} t{b['tick']}] "
                             f"{b.get('title', '')} — {b.get('note', '')}")
        if not stories:
            continue                       # no narrated series: nothing to say
        st = standings.get(name) or {}
        material = (f"The admiral whose TOURNAMENT arc you are writing: "
                    f"{name}.\nFinal standings entry: "
                    f"{json.dumps(st, sort_keys=True)}. Champion: {champion}."
                    + ("\n\n" + "\n".join(stories))
                    + "\n\n=== ANCHORS (cite matchup+game+tick from THESE "
                    "lines only) ===\n" + "\n".join(lines))
        sys_extra = ("\nThis is the TOURNAMENT-level arc: one story across "
                     "every matchup, built from the per-series accounts "
                     "below. Each beat MUST also carry \"matchup\" copied "
                     "exactly from an ANCHORS line.")
        obj, usage = _narrate_one(bot, sys_extra, material, chars,
                                  MAX_BEATS_TOURNAMENT)
        rec = {"usage": usage}
        if obj is not None:
            rec["story"] = str(obj.get("story", ""))[:chars]
            beats, dropped = validate_beats(obj.get("beats"), keys,
                                            MAX_BEATS_TOURNAMENT,
                                            extra="matchup")
            rec["beats"] = beats
            rec["dropped_beats"] = dropped
        out[name] = rec
        out["_meta"]["cost"] = round(out["_meta"]["cost"]
                                     + (usage.get("cost") or 0), 6)
        if emit:
            emit({"historic_moments": name, "scope": "tournament",
                  "err": usage.get("err"),
                  "beats": len(rec.get("beats", [])),
                  "dropped": rec.get("dropped_beats", 0)})
    return out
