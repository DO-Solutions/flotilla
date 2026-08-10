"""conn — the ship-programming language. Naval fact: the officer directing a
ship's movements "has the conn"; the helm executes. Exactly so here — admirals
write CONN PROGRAMS (per squadron) that have the conn and drive each ship's helm. This is the real
coding challenge: not toggling preset behaviors, but writing the control loop.

Design constraints (why not Python/Lua):
- DETERMINISTIC: pure expression evaluation, hard instruction budget per tick —
  replays stay byte-reproducible.
- SAFE: a purpose-built interpreter, no host access of any kind — LLM-authored
  code runs on any self-hosted server without a sandbox escape surface.
- LEGIBLE: line-numbered errors, and every action a ship takes records
  "program L<n>: <action>" as its intent — debuggable from the replay.

Grammar (one statement per line, evaluated TOP-DOWN every tick per ship):
  # comment
  mem <name> = <number>         persistent per-ship variable, initialized once
  set <name> = <expr>           assignment — executes whenever the line is reached
  when <expr>: <action>         first true `when` fires its action; run ends
  when <expr>: set <n> = <e>; …[; <action>]   compound body: sets run, then the
                                optional action fires (ends the run if present)
  default: <action>             same as `when 1:`

Actions:  helm.goto(x, y) · helm.gather() · helm.attack() · helm.flee()
          · helm.home() · helm.hold()
Exprs:    numbers · sensors · mem.<name> · + - * / % · == != < <= > >= ·
          and or not · ( ) · min(a,b) max(a,b) abs(a) sign(a) ·
          dist(x1,y1,x2,y2)

The SENSORS/ACTIONS tables below are the single source of truth: the interpreter,
the API reference injected into admiral prompts, and the docs all derive from them.
"""
import difflib
import math
import re

SENSORS = {
    "self.x": "your ship's x", "self.y": "your ship's y",
    "self.hull_pct": "hull % (0-100)",
    "self.cargo": "cargo aboard", "self.hold_cap": "cargo capacity",
    "self.power": "your combat power",
    "self.speed": "your speed stat",
    "self.docked": "1 inside your harbor circle else 0",
    "self.tick": "current game tick",
    "self.full": "1 if your hold is full (cargo >= capacity)",
    "self.rank": "your index among your squadron's LIVING ships (0,1,2… by age) — split lanes with it",
    "self.count": "how many living ships your squadron has",
    "self.stuck": "ticks wedged AT SEA (unable to move); 0 while docked",
    "self.idle": "ticks AT SEA without gaining cargo; 0 while docked",
    "harbor.x": "your harbor x", "harbor.y": "your harbor y",
    "harbor.dist": "distance to your harbor",
    "terr.owner": "territory games: fleet index holding the territory your ship stands in; -1 unclaimed, -2 when the match has no territories. Every cell belongs to its NEAREST territory seat (Chebyshev distance, ties to the lower id)",
    "terr.mine": "1 if the territory you stand in is held by you or a teammate — 'hold this point until it is ours' is `when terr.mine: helm.hold()`",
    "terr.capture": "capture progress % (0-99) of the claim underway in the territory you stand in; 0 when no capture is running",
    "enemy.found": "1 if an enemy ship is in YOUR ship's sight",
    "enemy.x": "nearest visible enemy x", "enemy.y": "nearest visible enemy y",
    "enemy.dist": "distance to that enemy",
    "enemy.laden": "1 if it is carrying cargo",
    "enemy.power": "its combat power",
    "enemy.stronger": "1 if it outguns you",
    "enemy.count": "how many enemy ships are in your sight right now",
    "ally.found": "1 if a fleet-mate is in sight",
    "ally.dist": "distance to the nearest fleet-mate",
    "node.found": "1 if your charts show a stocked island",
    "node.x": "nearest believed-stocked island x", "node.y": "…y",
    "node.dist": "distance to it", "node.stock": "believed stock there",
    "node.kind": "0 = fish shoal (regenerates), 1 = wreck (finite)",
    "rival.found": "1 if orders.target_fleet names a living rival",
    "rival.x": "that rival's harbor x", "rival.y": "that rival's harbor y",
    "rival.flag_x": "nearest hostile FLAGSHIP x (harbors are on the map)",
    "rival.flag_y": "nearest hostile flagship y",
    "rival.flag_dist": "distance to the nearest hostile flagship (destroy it to eliminate that fleet)",
    "rival.flag_hull": "that flagship's hull — revealed ONLY when THIS ship is close enough to scout it, else -1 (each ship reads its own sightings; a scout cannot share it — get in close and risk its guns)",
    "rival.yard_busy": "that fleet's shipyard works in progress (builds+refits+repairs) — revealed at the same close scouting range as flag_hull, and likewise only to THIS ship, else -1 (read their production tempo up close)",
    "orders.rally_x": "rally x from your squadron's standing orders",
    "orders.rally_y": "rally y from standing orders",
    "orders.aggression": "aggression from standing orders (0-3)",
    "orders.retreat": "retreat_hull_pct from standing orders",
}

ACTIONS = {
    "goto": (2, "sail toward (x, y)"),
    "gather": (0, "gather if ON a stocked island (else holds)"),
    "attack": (0, "close on the nearest visible enemy SHIP (guns fire automatically in range)"),
    "assault": (0, "close on the nearest hostile FLAGSHIP and batter it — the only way to eliminate a fleet. Flag damage needs adjacency (cheb<=1); the flag's own guns reach 2 cells, so expect to trade hulls. Send mass."),
    "flee": (0, "run directly away from the nearest visible enemy"),
    "home": (0, "make for your harbor (deposits/repairs/new orders in the circle)"),
    "hold": (0, "hold position"),
}

FUNCS = {"min": 2, "max": 2, "abs": 1, "sign": 1, "dist": 4}
# dist() is CHEBYSHEV (grid) distance — the same metric the whole game uses

MAX_LINES = 64
BUDGET = 3000                  # expression-node evaluations per ship per tick
                               # (raised with program_chars for richer programs;
                               # the perf pass gave the headroom)


class ConnError(Exception):
    def __init__(self, msg, line=None):
        super().__init__(f"line {line}: {msg}" if line else msg)
        self.line = line


def _fin(v, op):
    """Arithmetic must stay finite. conn evaluates in IEEE floats, so repeated
    multiplication saturates to inf (and inf-inf to nan). A non-finite value
    used to sail on to int() at the helm, raising OverflowError/ValueError —
    neither a ConnError, so nothing up the stack caught it and a THREE-LINE
    program killed the whole match (and in a series, the run process). Faulting
    here routes it to the ordinary program-fault path: standing orders take
    over, the admiral gets a warning, and the replay stays reproducible. It also
    keeps inf out of pmem, which json.dumps would have written to a checkpoint
    as bare `Infinity`.
    """
    if not math.isfinite(v):
        raise ConnError(f"'{op}' overflowed to a non-finite value")
    return v


def api_reference(examples=5):
    """The API card admirals receive — generated from the same tables the
    interpreter runs on, so documentation can never drift from behavior.
    examples (2-5) = how many worked examples to include: the first two are
    always shown; the PHASE MACHINE / FLAGSHIP HUNT / PACK HUNTER walkthroughs
    are the difficulty ladder (rank presets trim them — an Admiral gets the
    bare tables, a Captain gets the full tutorial)."""
    s = "\n".join(f"  {k:<18} {v}" for k, v in SENSORS.items())
    a = "\n".join(f"  helm.{k}({', '.join('xy'[i] for i in range(n)) if n else ''})"
                  f"  — {d}" for k, (n, d) in ACTIONS.items())
    ladder = ""
    if examples >= 3:
        ladder += """
Example (PHASE MACHINE — explicit state beats clever conditions; declare a
mem, branch on it, advance it):
  mem phase = 0
  when mem.phase == 0 and self.full: set phase = 1
  when mem.phase == 0 and node.found and node.dist == 0: helm.gather()
  when mem.phase == 0 and node.found: helm.goto(node.x, node.y)
  when mem.phase == 1 and self.docked: set phase = 0
  when mem.phase == 1: helm.home()
  when self.idle > 300: helm.home()
  default: helm.goto(orders.rally_x, orders.rally_y)
"""
    if examples >= 4:
        ladder += """
Example (FLAGSHIP HUNT — domination is won at the enemy flag; mass first,
press the flag, break off before you sink):
  when self.hull_pct < 30: helm.home()
  when self.count < 3: helm.goto(orders.rally_x, orders.rally_y)
  when enemy.found and enemy.dist <= 1 and rival.flag_dist > 3: helm.attack()
  default: helm.assault()
"""
    if examples >= 5:
        ladder += """
Example (PACK HUNTER — fight only with local superiority; alone, fall back
to the rally and wait for the pack):
  mem hot = 0
  when self.hull_pct < orders.retreat: set hot = 0; helm.home()
  when enemy.found and not enemy.stronger and ally.dist <= 4: set hot = 1; helm.attack()
  when enemy.found and enemy.stronger and enemy.dist < 6: helm.flee()
  when mem.hot == 1 and enemy.found: helm.attack()
  default: helm.goto(orders.rally_x, orders.rally_y)
"""
    return f"""
SHIP PROGRAMMING (the conn language) — each ship is a machine; your program HAS THE
CONN and drives its helm. Send "programs": {{"A": "<script>"}} alongside orders — same
delivery physics as orders (harbor circle / signal). One program per squadron;
every ship runs its own instance with its own mem. An empty string removes the
program (ship reverts to its standing-orders role).

Statements (one per line, run TOP-DOWN every tick):
  mem <name> = <number>      persistent per-ship variable (initialized once)
  set <name> = <expr>        assignment, executes when reached
  when <expr>: <body>        body = semicolon-separated `set`s with an OPTIONAL
                             final action, e.g.  when c: set a = 1; helm.home()
                             sets run and evaluation CONTINUES; the first ACTION
                             that fires ends the tick
  default: <body>            fallback (put it last)
  # comment

Expressions: + - * / %  · == != < <= > >=  · and or not · ( ) ·
  min(a,b) max(a,b) abs(a) sign(a) dist(x1,y1,x2,y2) — grid (Chebyshev) distance,
  the same metric every range in the game uses

SENSORS (read-only):
{s}
  mem.<name>          your own variables

ACTIONS (exactly one fires per tick):
{a}

Notes: deposits, repairs, and order pickup happen automatically in your harbor
circle; guns fire automatically at enemies in range. orders.* lets you retune a
running program's parameters each window WITHOUT rewriting code. A program that
fails to parse is REJECTED (you'll see program_rejected in events); a runtime
error makes that ship fall back to its standing orders for the tick. Budget:
{MAX_LINES} lines, {BUDGET} expression-steps per tick.

Example (a self-preserving forager):
  mem lowfuel = 0
  when self.hull_pct < orders.retreat: helm.home()
  when self.full: helm.home()
  when enemy.found and enemy.stronger and enemy.dist < 8: helm.flee()
  when node.found and node.dist == 0: helm.gather()

Example (LANE SPLIT — stop the whole squadron stacking on one shoal;
self.rank spreads ships WITHOUT signals):
  when self.full: helm.home()
  when self.stuck > 150: helm.home()
  when node.found and self.rank % 2 == 0 and node.dist == 0: helm.gather()
  when node.found and self.rank % 2 == 0: helm.goto(node.x, node.y)
  default: helm.goto(orders.rally_x, orders.rally_y)
{ladder}"""


# ---------------- tokenizer / parser ----------------

TOKEN = re.compile(r"\s*(>=|<=|==|!=|[-+*/%()<>:,=]|\d+\.?\d*|[A-Za-z_][A-Za-z_0-9]*(?:\.[A-Za-z_][A-Za-z_0-9]*)?)")


def _tokens(s, line):
    out, i = [], 0
    while i < len(s):
        m = TOKEN.match(s, i)
        if not m:
            raise ConnError(f"cannot read {s[i:i+10]!r}", line)
        out.append(m.group(1))
        i = m.end()
    return out


class _P:
    def __init__(self, toks, line, decls):
        self.t, self.i, self.line, self.decls = toks, 0, line, decls

    def peek(self):
        return self.t[self.i] if self.i < len(self.t) else None

    def take(self, want=None):
        tok = self.peek()
        if tok is None or (want and tok != want):
            raise ConnError(f"expected {want or 'more'}, got {tok!r}", self.line)
        self.i += 1
        return tok

    # precedence: or < and < not < cmp < add < mul < unary
    def expr(self):
        node = self.and_()
        while self.peek() == "or":
            self.take()
            node = ("or", node, self.and_())
        return node

    def and_(self):
        node = self.not_()
        while self.peek() == "and":
            self.take()
            node = ("and", node, self.not_())
        return node

    def not_(self):
        if self.peek() == "not":
            self.take()
            return ("not", self.not_())
        return self.cmp()

    def cmp(self):
        node = self.add()
        while self.peek() in ("==", "!=", "<", "<=", ">", ">="):
            op = self.take()
            node = (op, node, self.add())
        return node

    def add(self):
        node = self.mul()
        while self.peek() in ("+", "-"):
            op = self.take()
            node = (op, node, self.mul())
        return node

    def mul(self):
        node = self.unary()
        while self.peek() in ("*", "/", "%"):
            op = self.take()
            node = (op, node, self.unary())
        return node

    def unary(self):
        if self.peek() == "-":
            self.take()
            return ("neg", self.unary())
        return self.atom()

    def atom(self):
        tok = self.take()
        if tok == "(":
            node = self.expr()
            self.take(")")
            return node
        if re.fullmatch(r"\d+\.?\d*", tok):
            v = float(tok)
            if not math.isfinite(v):        # 400 digits parses straight to inf
                raise ConnError(f"number {tok[:12]}… is too large", self.line)
            return ("num", v)
        if tok in FUNCS:
            self.take("(")
            args = [self.expr()]
            while self.peek() == ",":
                self.take()
                args.append(self.expr())
            self.take(")")
            if len(args) != FUNCS[tok]:
                raise ConnError(f"{tok}() takes {FUNCS[tok]} args", self.line)
            return ("fn", tok, args)
        if tok in SENSORS:
            return ("sensor", tok)
        if tok.startswith("mem."):
            name = tok[4:]
            if name not in self.decls:
                raise ConnError(f"mem.{name} not declared (add: mem {name} = 0)",
                                self.line)
            return ("mem", name)
        cands = list(SENSORS) + [f"mem.{d}" for d in self.decls] + list(FUNCS)
        sug = difflib.get_close_matches(tok, cands, n=2, cutoff=0.5)
        hint = (f" — did you mean {' or '.join(sug)}?" if sug else
                f" — declare your own variable first (mem {tok} = 0, then "
                f"mem.{tok}) or use a sensor from the list")
        raise ConnError(f"unknown name {tok!r}{hint}", self.line)


def _parse_action(p):
    tok = p.take()
    if tok != "helm" and not tok.startswith("helm."):
        raise ConnError(f"action must be helm.<verb>(...), got {tok!r}", p.line)
    verb = tok[5:] if tok.startswith("helm.") else p.take()
    if verb not in ACTIONS:
        raise ConnError(f"unknown action helm.{verb} — one of {list(ACTIONS)}", p.line)
    nargs = ACTIONS[verb][0]
    p.take("(")
    args = []
    if p.peek() != ")":
        args.append(p.expr())
        while p.peek() == ",":
            p.take()
            args.append(p.expr())
    p.take(")")
    if len(args) != nargs:
        raise ConnError(f"helm.{verb} takes {nargs} args", p.line)
    return (verb, args)


def _parse_body(body, ln, decls):
    """A when/default body: semicolon-separated `set`s + at most one final action.
    Returns (sets, action_or_None); sets = [(name, expr), ...]."""
    sets, action = [], None
    parts = [x.strip() for x in body.split(";") if x.strip()]
    if not parts:
        raise ConnError("empty body — give it set(s) and/or an action", ln)
    for i, part in enumerate(parts):
        if part.startswith("set "):
            m = re.match(r"set\s+([A-Za-z_][A-Za-z_0-9]*)\s*=\s*(.+)", part)
            if not m:
                raise ConnError("set syntax: set <name> = <expr>", ln)
            if m.group(1) not in decls:
                raise ConnError(f"set of undeclared {m.group(1)} "
                                f"(add: mem {m.group(1)} = 0)", ln)
            p = _P(_tokens(m.group(2), ln), ln, decls)
            e = p.expr()
            if p.peek() is not None:
                raise ConnError(f"unexpected {p.peek()!r}", ln)
            sets.append((m.group(1), e))
        else:
            if i != len(parts) - 1:
                raise ConnError("the action must come LAST in a body", ln)
            pa = _P(_tokens(part, ln), ln, decls)
            action = _parse_action(pa)
            if pa.peek() is not None:
                raise ConnError(f"unexpected {pa.peek()!r} after action", ln)
    return sets, action


def compile_program(text):
    """text -> Program. Raises ConnError (with line number) on any problem."""
    decls = {}
    stmts = []                             # ("set", name, expr) | ("when", expr, action)
    lines = text.splitlines()
    meaningful = [l for l in lines if l.split("#", 1)[0].strip()]
    if len(meaningful) > MAX_LINES:        # comments/blanks are free
        raise ConnError(f"program exceeds {MAX_LINES} statements "
                        f"({len(meaningful)}; comments don't count)")
    # pass 1: HOIST every mem declaration — models paste programs with their
    # mems at the bottom, and declaration order should never be an error
    for ln, raw in enumerate(lines, 1):
        s = raw.split("#", 1)[0].strip()
        if s.startswith("mem "):
            m = re.fullmatch(r"mem\s+([A-Za-z_][A-Za-z_0-9]*)\s*=\s*(-?\d+\.?\d*)", s)
            if not m:
                raise ConnError("mem syntax: mem <name> = <number>", ln)
            v = float(m.group(2))
            if not math.isfinite(v):        # 400 digits parses straight to inf
                raise ConnError(f"mem {m.group(1)} initial value is too large", ln)
            decls[m.group(1)] = v
    for ln, raw in enumerate(lines, 1):
        s = raw.split("#", 1)[0].strip()
        if not s:
            continue
        if s.startswith("mem "):
            continue                       # hoisted in pass 1
        if s.startswith("set "):
            m = re.match(r"set\s+([A-Za-z_][A-Za-z_0-9]*)\s*=\s*(.+)", s)
            if not m:
                raise ConnError("set syntax: set <name> = <expr>", ln)
            if m.group(1) not in decls:
                raise ConnError(f"set of undeclared {m.group(1)} "
                                f"(add: mem {m.group(1)} = 0)", ln)
            p = _P(_tokens(m.group(2), ln), ln, decls)
            e = p.expr()
            if p.peek() is not None:
                raise ConnError(f"unexpected {p.peek()!r}", ln)
            stmts.append(("set", m.group(1), e, ln))
            continue
        if s.startswith("when ") or s.startswith("default"):
            if s.startswith("default"):
                rest = s[len("default"):].lstrip()
                if not rest.startswith(":"):
                    raise ConnError("default syntax: default: <action>", ln)
                cond = ("num", 1.0)
                body = rest[1:].strip()
            else:
                if ":" not in s:
                    raise ConnError("when syntax: when <expr>: <action>", ln)
                cexpr, body = s[5:].split(":", 1)
                p = _P(_tokens(cexpr.strip(), ln), ln, decls)
                cond = p.expr()
                if p.peek() is not None:
                    raise ConnError(f"unexpected {p.peek()!r} in condition", ln)
                body = body.strip()
            sets, action = _parse_body(body, ln, decls)
            stmts.append(("when", cond, sets, action, ln))
            continue
        raise ConnError(f"cannot parse {s[:30]!r} — statements are "
                        "mem/set/when/default (an action like helm.home() "
                        "must sit inside a when/default body)", ln)
    if not any(st[0] == "when" and st[3] is not None for st in stmts):
        raise ConnError("program has no when/default with an ACTION — it can never act")
    return Program(decls, stmts, text)


class Program:
    __slots__ = ("decls", "stmts", "text")

    def __init__(self, decls, stmts, text):
        self.decls = decls
        self.stmts = stmts
        self.text = text

    def init_mem(self):
        return dict(self.decls)

    def run(self, sensors, mem):
        """One tick: returns (verb, [arg values], line) or None (no when fired).
        Raises ConnError on budget exhaustion."""
        budget = [BUDGET]
        staged = dict(mem)                 # ev() reads THIS: later statements
                                           # see earlier writes, as always

        def ev(node):
            # check-then-decrement: the advertised budget is the real budget
            # (decrement-first admitted BUDGET-1 evaluations)
            if budget[0] <= 0:
                raise ConnError(f"instruction budget ({BUDGET}) exhausted")
            budget[0] -= 1
            k = node[0]
            if k == "num":
                return node[1]
            if k == "sensor":
                return float(sensors.get(node[1], 0.0))
            if k == "mem":
                return float(staged.get(node[1], 0.0))
            if k == "neg":
                return -ev(node[1])
            if k == "not":
                return 0.0 if ev(node[1]) else 1.0
            if k == "and":
                return ev(node[2]) if ev(node[1]) else 0.0
            if k == "or":
                a = ev(node[1])
                return a if a else ev(node[2])
            if k == "fn":
                a = [ev(x) for x in node[2]]
                if node[1] == "min":
                    return min(a)
                if node[1] == "max":
                    return max(a)
                if node[1] == "abs":
                    return abs(a[0])
                if node[1] == "sign":
                    return float((a[0] > 0) - (a[0] < 0))
                return _fin(max(abs(a[0] - a[2]),
                                abs(a[1] - a[3])), "dist")  # dist = cheb
            a, b = ev(node[1]), ev(node[2])
            if k == "+":
                return _fin(a + b, k)
            if k == "-":
                return _fin(a - b, k)
            if k == "*":
                return _fin(a * b, k)
            if k == "/":
                return _fin(a / b, k) if b else 0.0
            if k == "%":
                return _fin(a % b, k) if b else 0.0
            if k == "==":
                return 1.0 if a == b else 0.0
            if k == "!=":
                return 1.0 if a != b else 0.0
            if k == "<":
                return 1.0 if a < b else 0.0
            if k == "<=":
                return 1.0 if a <= b else 0.0
            if k == ">":
                return 1.0 if a > b else 0.0
            return 1.0 if a >= b else 0.0

        # writes stage into the scratch overlay and COMMIT only on clean exit —
        # a budget fault mid-walk used to keep the earlier assignments, so a
        # too-big program "ran standing orders" while its phase counters
        # silently advanced every tick
        try:
            for st in self.stmts:
                if st[0] == "set":
                    staged[st[1]] = ev(st[2])
                else:                      # ("when", cond, sets, action, line)
                    if ev(st[1]):
                        for name, e in st[2]:   # body sets run; evaluation continues…
                            staged[name] = ev(e)
                        if st[3] is not None:   # …unless an action fires
                            verb, argnodes = st[3]
                            out = (verb, [ev(x) for x in argnodes], st[4])
                            mem.clear()
                            mem.update(staged)
                            return out
            mem.clear()
            mem.update(staged)
            return None
        except ConnError:
            raise                          # mem untouched: all-or-nothing
