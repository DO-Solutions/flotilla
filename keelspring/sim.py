"""The run-loop scaffolding — game-agnostic (split Stage 3).

SimBase is the half of a match engine that is the same for EVERY game:
decision windows (lockstep pacing lives in the game's tick; the pipelined
window machinery lives here), the catch-up framing, decision forensics,
the API-outage detector, the live-stream flush, the intent/event recorders,
and the outer run() loop with its pause protocol.

A game extends SimBase with its world. The base reads these from self —
the de-facto World protocol, validated in practice by the game's own suite:

  cfg (resolved knob dict) · t (tick) · max_ticks · seed
  fleets: {id: fleet} — fleet: .id .name .bot .alive .team .score()
      .died_t .warnings .recent_hits .combat .contacts .inbox .parley_log
  tick() — advance the world one step (calls the window machinery)
  summary_for(fleet) — the fog-honest snapshot an admiral decides on
  _apply_actions(fleet, actions) — per-field-isolated order application
  _frame() / frames / events / decisions — the replay stream
  live / live_header() — the live sink + its game-shaped header

Determinism discipline: every random draw comes from seeded Random
instances derived from (seed, fleet, window) — wall-clock exists ONLY in
window pacing, which decides when orders land, never how the world evolves.
"""
import random
import threading
import time


def one_line(s, cap):
    """Collapse model-authored text to a single line, then cap it.

    Parley messages are rendered into a rival admiral's prompt as plain text
    ("w3 from Rival: <text>"), so the one-line property IS the whole structural
    defense of that block: a message able to emit a line break can forge engine
    framing (`truce\r=== CURRENT STATE — window 9 ===\r...`) that renders
    flush-left and reads as trusted context. Replacing only "\n" left \r, \v,
    U+0085 and U+2028/9 as live carriers, so every C0/C1 control and unicode
    line separator is folded to a space here.
    """
    out = []
    for ch in str(s):
        o = ord(ch)
        out.append(" " if o < 32 or o == 127 or 0x80 <= o <= 0x9F
                   or o in (0x2028, 0x2029) else ch)
    return "".join(out).strip()[:cap]


class SimBase:
    """Extend with your game (see the module docstring for the surface)."""

    #: the methods a game MUST define — checked the moment the subclass is
    #: created, so an incomplete game fails at import with a list of what is
    #: missing, never as an AttributeError mid-match
    WORLD_PROTOCOL = ("tick", "summary_for", "_apply_actions",
                      "_frame", "live_header")

    def __init_subclass__(cls, **kw):
        super().__init_subclass__(**kw)
        missing = [m for m in SimBase.WORLD_PROTOCOL
                   if not callable(getattr(cls, m, None))]
        if missing:
            raise TypeError(
                f"{cls.__name__} does not satisfy the SimBase World protocol "
                f"— missing: {', '.join(missing)} (see keelspring/sim.py)")

    def _ev(self, kind, **kw):
        kw["t"] = self.t
        kw["k"] = kind
        self.events.append(kw)


    @staticmethod
    def _as_list(v):
        return v if isinstance(v, list) else []

    @staticmethod
    def _as_dict(v):
        return v if isinstance(v, dict) else {}

    def _pipe_window(self, live, t):
        """One half of THE REMONTOIRE (docs/KEELSPRING.md): like the clock
        mechanism it is named for, the window machinery delivers constant,
        metered force to every admiral — same snapshot shape, same budget,
        same cadence — regardless of upstream conditions. The provider
        ladder is the other half.

        Catch-up pipelining: each admiral has at most ONE reply in flight.
        A window stays open window_wait_s wall-clock; admirals answering in
        time decide every window, slow ones miss windows until their reply
        lands — then they get ONE fresh snapshot (state.CATCH_UP says how far
        the world moved) and rejoin live. No parallel calls per admiral means
        no self-stale orders and no token multiplication. The sim blocks only
        for an admiral pipeline_depth whole windows behind."""
        depth = self.cfg["pipeline_depth"]
        wait_s = float(self.cfg["window_wait_s"])
        # 1. pace the window that just elapsed: give this window's dispatches
        #    up to window_wait_s wall-clock; when someone is behind and
        #    hold_full_window is on, burn the whole allowance either way
        if self._win_opened is not None:
            deadline = self._win_opened + wait_s
            behind = any(r["missed"] > 0 for r in self._pipe.values())
            hold = behind and self.cfg["hold_full_window"]
            while True:
                fresh = [r for r in self._pipe.values()
                         if r["missed"] == 0 and r["thread"].is_alive()]
                left = deadline - time.time()
                if left <= 0 or (not fresh and not hold):
                    break
                if fresh:
                    fresh[0]["thread"].join(timeout=min(0.25, left))
                else:
                    time.sleep(min(0.25, left))
        # 2. cap: an admiral depth whole windows behind blocks the fleet
        for f in live:
            rec = self._pipe.get(f.id)
            if rec and rec["missed"] >= depth:
                rec["thread"].join()
        # 3. harvest every landed reply, oldest dispatch first (deterministic)
        done = sorted([r for r in self._pipe.values()
                       if not r["thread"].is_alive()],
                      key=lambda r: (r["t0"], r["fid"]))
        for rec in done:
            del self._pipe[rec["fid"]]
            f = self.fleets[rec["fid"]]
            actions = rec.get("result") or dict(
                thoughts="(admiral produced no reply; standing orders continue)")
            if rec.get("err"):
                # a lost window is VISIBLE (playtest feedback: "losing to
                # infrastructure, not play, is the worst outcome in a skill
                # test" — the silent version ran standing orders with no flag)
                f.windows_lost += 1
                f.warnings.append(
                    f"⚠ your decision window opened at t={rec['t0']} was LOST "
                    f"to an error ({rec['err']}) — standing orders ran; this "
                    "snapshot is current")
            if not f.alive:
                self.decisions.append(dict(
                    t=self.t, fleet=f.id, ot=rec["t0"],
                    thoughts="(orders arrived after this fleet was eliminated)"))
                continue
            self._pipe_ot = rec["t0"]
            try:
                self._apply_actions(f, actions)
            except Exception as e:          # NOTHING an admiral emits kills the sim
                # per-field isolation lives INSIDE _apply_actions — reaching
                # this is an engine fault, not a bad field
                f.warnings.append(
                    f"⚠ an internal error interrupted your orders this window "
                    f"({type(e).__name__}: {str(e)[:200]})")
            finally:
                self._pipe_ot = None
        # 4. still-outstanding calls just missed this boundary
        for rec in self._pipe.values():
            rec["missed"] += 1
        # 5. dispatch ONE call to every live fleet without one in flight; a
        #    fleet whose gap exceeds one window gets the catch-up framing.
        #    (The snapshot itself already batches everything since the last
        #    dispatch — combat, contacts, parley all accumulate per fleet.)
        for f in live:
            if f.id in self._pipe:
                continue                    # its reply is still in flight
            summary = self.summary_for(f)
            gap = (t - self._last_snap.get(f.id, t - self.cfg["window"])) \
                // self.cfg["window"]
            if gap > 1:
                summary["CATCH_UP"] = {
                    "windows_missed": gap - 1,
                    "note": f"your previous reply outran its window — the world "
                            f"sailed on {gap} windows while you thought. THIS "
                            "snapshot is current and includes everything that "
                            "happened meanwhile (combat, contacts, messages). "
                            "You are live again: answer within the window to "
                            "decide every window.",
                }
            self._last_snap[f.id] = t
            f.recent_hits = 0               # consumed by the snapshot just taken
            f.combat = {}
            bot_rng = random.Random((self.seed << 8) ^ (f.id << 4)
                                    ^ (t // self.cfg['window']))
            rec = {"fid": f.id, "t0": t, "missed": 0, "result": None}

            def _call(rec=rec, f=f, summary=summary, bot_rng=bot_rng):
                try:
                    rec["result"] = f.bot.decide(summary, bot_rng)
                except Exception as e:      # a broken admiral never crashes the sim
                    rec["result"] = dict(thoughts=f"(admiral error: {e})")
                    rec["err"] = f"{type(e).__name__}: {str(e)[:120]}"

            th = threading.Thread(target=_call, daemon=True)
            rec["thread"] = th
            self._pipe[f.id] = rec
            th.start()
        self._win_opened = time.time()

    def _pipe_drain(self):
        """Game over: wait out in-flight calls so a series' next game can't
        interleave with this one's stragglers (bots carry per-game state).
        The join is UNBOUNDED on purpose: every decide() is internally bounded
        (per-attempt HTTP timeouts, finite retries), and a bounded join here
        once let a straggler outlive it and write game-N thoughts, scratchpad
        and feedback flags into game N+1's admiral mid-debrief."""
        for rec in self._pipe.values():
            rec["thread"].join()
        self._pipe = {}

    def _record_decision(self, fleet, actions):
        rec = dict(t=self.t, fleet=fleet.id,
                   thoughts=str(actions.get("thoughts", ""))[:400])
        if self._pipe_ot is not None and self._pipe_ot != self.t:
            rec["ot"] = self._pipe_ot      # pipelined: ordered-at vs applied-at
        if actions.get("scratchpad") is not None:    # replay review sees pad rewrites
            rec["pad"] = str(actions["scratchpad"])[:2000]
        if isinstance(actions.get("_usage"), dict):
            rec["u"] = actions["_usage"]
            self._u_fleets.add(fleet.id)
        # ground truth for order forensics: which action fields the reply
        # carried, and every warning this window generated — "did my
        # build_yard actually go in?" is now answerable from the replay
        acts = [k for k in self.ACTION_FIELDS if k in actions]
        if acts:
            rec["acts"] = acts
        wm = getattr(self, "_warn_mark", None)
        if wm is not None and len(fleet.warnings) > wm:
            rec["warns"] = [str(w)[:160] for w in fleet.warnings[wm:wm + 8]]
        self.decisions.append(rec)

    # ---------- per-ship behavior ----------

    def _intent(self, ship, s):
        if s != ship.intent:
            s = self._istr.setdefault(s, s)   # OPT-M1: one str object per distinct
            ship.intent = s                   # phrasing (identical JSON, less RAM)
            self._ev("intent", ship=ship.id, fleet=ship.fleet, s=s)

    def _outage_check(self):
        """True when every LLM admiral has failed with a transport/API error
        (u.err set: connection/5xx/timeout) for outage_pause_windows straight
        windows. Scripted bots carry no usage record and never count; a window
        with no LLM decisions yet (pipelined call in flight) is no signal."""
        k = self.cfg.get("outage_pause_windows", 0)
        if k <= 0:
            return False
        if self.t <= self._outage_eval_t:  # each boundary counts ONCE — a
            return False                   # thaw re-enters at its pause tick
        self._outage_eval_t = self.t
        # decisions are stamped at the boundary tick that OPENED their window:
        # at boundary t the just-finished window's decisions carry t - window
        lo = self.t - self.cfg["window"]
        # only API/transport faults count (u.ek "api"); a TruncatedReply or
        # unparseable answer means the API is UP — pausing on those turned a
        # model-formatting problem into an un-completable auto-resume loop
        recs = [d["u"] for d in self.decisions
                if "u" in d and lo <= d["t"] < self.t]
        errs = [bool(u.get("err")) and u.get("ek", "api") == "api"
                for u in recs]
        if not errs:
            return False
        if not all(errs):
            self._outage_streak = 0
            return False
        # every landed reply errored — but only a FULL window (every live
        # LLM fleet reported) advances the streak: under pipelining a slow
        # but healthy admiral still in flight must not let the errored half
        # of the field pause the whole run
        live_llm = sum(1 for f in self.fleets.values()
                       if f.alive and f.id in self._u_fleets)
        if len(errs) < live_llm:
            return False
        self._outage_streak += 1
        return self._outage_streak >= k

    def _live_flush(self, final=False):
        payload = dict(t=self.t, final=final,
                       frames=self.frames[self._lf:],
                       events=self.events[self._le:],
                       decisions=self.decisions[self._ld:],
                       scores={f.id: f.score() for f in self.fleets.values()})
        self._lf, self._le, self._ld = (len(self.frames), len(self.events),
                                        len(self.decisions))
        try:
            self.live(payload)
        except Exception:
            self.live = None               # a broken sink must never hurt the sim

    def run(self, pause_check=None):
        """pause_check (optional): polled at every window boundary; return True
        to freeze the match. The engine drains in-flight calls, drops its live
        sink, and run() returns None — freeze() then captures the engine as a
        plain-JSON checkpoint, and after thaw() a later run() call continues
        the SAME game tick-for-tick (checkpoint/resume is hash-identical to
        an uninterrupted run; test_pause proves it)."""
        while self.t < self.max_ticks \
                and len({f.team for f in self.fleets.values() if f.alive}) > 1:
            if self.t > 0 and self.t % self.cfg["window"] == 0:
                if pause_check is not None and pause_check():
                    if self._pipe:
                        self._pipe_drain()
                    self.live = None
                    return None
                if self._outage_check():
                    # API outage/slowdown circuit breaker: every LLM admiral
                    # erroring for K straight windows means the game is just
                    # burning ticks on stale orders — freeze instead. The
                    # streak survives freeze/thaw, so a resume that is STILL
                    # in outage re-pauses after one more bad window.
                    self.pause_reason = (
                        f"api-outage: every LLM admiral errored for "
                        f"{self._outage_streak} consecutive windows")
                    if self._pipe:
                        self._pipe_drain()
                    self.live = None
                    return None
            self.tick()
        if self._pipe:
            self._pipe_drain()
        self._frame()
        if self.live is not None:
            self._live_flush(final=True)
        return self._final_result()

    def tiebreak_rungs(self):
        """OPTIONAL game hook: the ordered tie chain applied to fleets tied on
        the primary key (alive, score). Each rung is (label, {fleet_id:
        value}), highest value wins; surviving candidates carry to the next
        rung. One left = the winner (the result records decided_by + the
        trail); chain exhausted = an honest DRAW (winner None). Default: no
        rungs — the engine keeps its legacy lowest-id rule, so a game without
        a tie policy behaves exactly as before."""
        return []

    def _apply_tiebreak(self, cands, value_of, out):
        """Run the game's tie chain over `cands` (ids tied on the primary
        key). Returns the single winner, or None — a DRAW if rungs existed
        (out['tiebreak'] says so), a legacy tie otherwise (no 'tiebreak' key;
        the caller applies the lowest-id rule)."""
        rungs = self.tiebreak_rungs()
        if not rungs:
            return None
        trail = []
        for label, vals in rungs:
            vv = {c: value_of(vals, c) for c in cands}
            top = max(vv.values())
            trail.append({"rung": label, "values": vv})
            cands = [c for c in cands if vv[c] == top]
            if len(cands) == 1:
                out["tiebreak"] = {"decided_by": label, "trail": trail}
                return cands[0]
        out["tiebreak"] = {"draw": True, "trail": trail}
        return None

    def _final_result(self):
        scores = {f.id: f.score() for f in self.fleets.values()}
        alive = [f.id for f in self.fleets.values() if f.alive]
        out = dict(ticks=self.t, scores=scores, alive=alive,
                   names={f.id: f.name for f in self.fleets.values()})
        teams = {f.id: f.team for f in self.fleets.values()}
        if len(set(teams.values())) < len(teams):    # team match: scores SUM
            tscores = {}
            talive = {}
            for f in self.fleets.values():
                tscores[f.team] = tscores.get(f.team, 0) + scores[f.id]
                talive[f.team] = talive.get(f.team, False) or f.alive
            out["teams"] = teams
            out["team_scores"] = tscores
            tprim = lambda tm: (talive[tm], tscores[tm])      # noqa: E731
            tbest = tprim(max(tscores, key=tprim))
            tcands = sorted(tm for tm in tscores if tprim(tm) == tbest)
            tw = tcands[0]
            if len(tcands) > 1:
                # team ties: each rung's per-fleet values SUM across the team
                tw = self._apply_tiebreak(
                    tcands,
                    lambda vals, tm: sum(v for fid, v in vals.items()
                                         if teams[fid] == tm), out)
                if tw is None and "tiebreak" not in out:
                    tw = tcands[0]                    # legacy lowest team id
            if tw is None:
                out["winner"] = None                  # drawn team match
            else:
                # winner (a fleet id, for every existing pipeline) = the
                # winning team's best member
                out["winner"] = max(
                    (f.id for f in self.fleets.values() if f.team == tw),
                    key=lambda k: (self.fleets[k].alive, scores[k], -k))
        else:
            prim = lambda k: (self.fleets[k].alive, scores[k])  # noqa: E731
            pbest = prim(max(scores, key=prim))
            cands = sorted(k for k in scores if prim(k) == pbest)
            w = cands[0]
            if len(cands) > 1:
                w = self._apply_tiebreak(cands,
                                         lambda vals, c: vals.get(c, 0), out)
                if w is None and "tiebreak" not in out:
                    w = cands[0]                      # legacy lowest fleet id
            out["winner"] = w
        # the DISPLAYED order now matches the winner rule (playtest feedback:
        # an eliminated fleet read as rank 1 on banked score while the sole
        # survivor placed third): survivors first, then later-fallen, then
        # score, then the game's tie rungs — the id fallback stays LAST so
        # rank is always a total order (a drawn game still lists everyone;
        # winner=None is what marks the draw)
        rungs = self.tiebreak_rungs()
        out["rank"] = sorted(
            scores, key=lambda k: (self.fleets[k].alive,
                                   self.fleets[k].died_t
                                   if self.fleets[k].died_t is not None else -1,
                                   scores[k],
                                   tuple(vals.get(k, 0) for _l, vals in rungs),
                                   -k), reverse=True)
        return out
