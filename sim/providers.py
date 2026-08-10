"""Inference-provider ladder: ordered providers with automatic fallback and
canary fall-forward.

Config arrives as JSON in the FLOTILLA_PROVIDERS env var — the flagship
injects it from its key store (dash "Server" tab) into local runners and aux
workers alike. Absent/empty = a single DigitalOcean provider from the classic
DO_INFERENCE_KEY env, byte-for-byte the old behavior.

Per-MODEL state (all admirals with the same model share one ladder):
- any 429 from the current provider  -> demote immediately
- N timeouts in a row (separate N when pipelined)  -> demote
- M transport/5xx errors in a row  -> demote
- while demoted, every `canary_minutes` ONE real request probes the BEST
  serving rung (normally the primary); success jumps straight back to it,
  failure stays put and costs nothing extra. Demotion walks DOWN one rung
  at a time; recovery jumps UP to the top.

A provider serves a model when its model_map names it or its models list
contains it (the check button on the Server tab fills that list). The
BUILTIN provider always serves everything — it is where the ids come from.
A model starts on the highest rung that serves it.
"""
import json
import os
import threading
import time


def _load():
    raw = os.environ.get("FLOTILLA_PROVIDERS", "")
    if raw:
        try:
            cfg = json.loads(raw)
            provs = [p for p in cfg.get("providers", [])
                     if p.get("enabled", True) and p.get("key")]
            provs.sort(key=lambda p: p.get("order", 0))
            if provs:
                return provs, cfg.get("fallback", {}) or {}
        except Exception:
            pass
    return [dict(id="digitalocean", label="DigitalOcean", builtin=True,
                 base_url=os.environ.get("DO_INFERENCE_BASE",
                                         "https://inference.do-ai.run/v1"),
                 key=os.environ.get("DO_INFERENCE_KEY", ""))], {}


class Ladder:
    def __init__(self, providers=None, fallback=None):
        if providers is None:
            providers, fb = _load()
            fallback = fallback or fb
        fallback = fallback or {}
        self.providers = providers
        self.timeout_streak = int(fallback.get("timeout_streak", 3))
        self.timeout_streak_pipelined = int(
            fallback.get("timeout_streak_pipelined", 5))
        self.error_streak = int(fallback.get("error_streak", 2))
        self.canary_s = float(fallback.get("canary_minutes", 10)) * 60
        self.pipelined = os.environ.get("FLOTILLA_PIPELINED") == "1"
        self._state = {}                   # model -> dict
        self._lock = threading.Lock()

    # ---- support / mapping ----
    def _serves(self, i, model):
        # the BUILTIN provider is where the model ids come from — it serves
        # everything. Anyone else needs a model_map entry or a discovered
        # models list. (The old "rung 0 always serves" broke the moment the
        # operator disabled the builtin: a third-party rung 0 got raw DO ids
        # it couldn't serve, and _top_serving canaried it forever.)
        p = self.providers[i]
        if p.get("builtin"):
            return True
        return model in (p.get("model_map") or {}) \
            or model in (p.get("models") or [])

    def _first_serving(self, model):
        for i in range(len(self.providers)):
            if self._serves(i, model):
                return i
        return 0                           # nothing claims it: old behavior

    def mapped(self, i, model):
        return (self.providers[i].get("model_map") or {}).get(model, model)

    def _next_down(self, model, frm):
        for i in range(frm + 1, len(self.providers)):
            if self._serves(i, model):
                return i
        return None

    def _top_serving(self, model, below):
        """The HIGHEST-preference rung above `below` that serves the model —
        canaries always probe the best available rung (usually the primary),
        not the next one up: demotion walks down one rung at a time, but
        recovery jumps straight back to the top the moment it answers."""
        for i in range(0, below):
            if self._serves(i, model):
                return i
        return None

    def _st(self, model):
        st = self._state.get(model)
        if st is None:
            st = self._state[model] = {"idx": self._first_serving(model),
                                       "touts": 0, "errs": 0, "canary_at": 0.0}
        return st

    # ---- the two calls _chat makes ----
    def resolve(self, model):
        """-> (idx, provider dict, mapped model id, is_canary)."""
        with self._lock:
            st = self._st(model)
            idx = st["idx"]
            if idx > 0 and time.time() - st["canary_at"] >= self.canary_s:
                st["canary_at"] = time.time()
                up = self._top_serving(model, idx)
                if up is not None:
                    return up, self.providers[up], self.mapped(up, model), True
            return idx, self.providers[idx], self.mapped(idx, model), False

    def report(self, model, idx, is_canary, outcome):
        """outcome: ok | 429 | timeout | error. Returns a log line when the
        rung changed (the caller surfaces it), else None."""
        with self._lock:
            st = self._st(model)
            if outcome == "ok":
                if is_canary and idx < st["idx"]:
                    st["idx"] = idx        # jump straight back up
                    st["touts"] = st["errs"] = 0
                    return (f"provider canary ok — {model} promoted to "
                            f"{self._label(idx)}")
                if idx != st["idx"]:
                    return None            # a late success from an abandoned
                    # rung must not clear the CURRENT rung's streaks
                st["touts"] = st["errs"] = 0
                return None
            if is_canary:                  # a failed probe never demotes
                return None
            if idx != st["idx"]:
                return None                # stale report after a rung change
            demote = False
            if outcome == "429":
                demote = True
            elif outcome == "timeout":
                st["touts"] += 1
                lim = self.timeout_streak_pipelined if self.pipelined \
                    else self.timeout_streak
                demote = st["touts"] >= lim
            else:                          # 5xx / connection error
                st["errs"] += 1
                demote = st["errs"] >= self.error_streak
            if not demote:
                return None
            nxt = self._next_down(model, st["idx"])
            if nxt is None:
                st["touts"] = st["errs"] = 0
                return None                # no fallback serves this model
            st["idx"] = nxt
            st["touts"] = st["errs"] = 0
            st["canary_at"] = time.time()
            return (f"provider fallback — {model}: {outcome} on "
                    f"{self._label(idx)}, demoted to {self._label(nxt)}")

    def _label(self, i):
        p = self.providers[i]
        return p.get("label") or p.get("id") or f"provider {i}"


_LADDER = None
_LADDER_LOCK = threading.Lock()


def ladder():
    # all admirals' first calls fire simultaneously from the tick pool — an
    # unlocked check-then-set would hand one thread an orphaned Ladder whose
    # demotions vanish
    global _LADDER
    with _LADDER_LOCK:
        if _LADDER is None:
            _LADDER = Ladder()
        return _LADDER
