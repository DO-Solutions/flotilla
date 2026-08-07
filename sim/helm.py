"""helm — the ship-programming language. Each ship is a machine with sensors and
a helm; admirals write PROGRAMS (per squadron) that ride it. This is the real
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
  default: <action>             same as `when 1:`

Actions:  helm.goto(x, y) · helm.gather() · helm.attack() · helm.flee()
          · helm.home() · helm.hold()
Exprs:    numbers · sensors · mem.<name> · + - * / % · == != < <= > >= ·
          and or not · ( ) · min(a,b) max(a,b) abs(a) dist(x1,y1,x2,y2)

The SENSORS/ACTIONS tables below are the single source of truth: the interpreter,
the API reference injected into admiral prompts, and the docs all derive from them.
"""
import re

SENSORS = {
    "self.x": "your ship's x", "self.y": "your ship's y",
    "self.hull_pct": "hull % (0-100)",
    "self.cargo": "cargo aboard", "self.hold_cap": "cargo capacity",
    "self.power": "your combat power",
    "self.docked": "1 inside your harbor circle else 0",
    "self.tick": "current game tick",
    "harbor.x": "your harbor x", "harbor.y": "your harbor y",
    "harbor.dist": "distance to your harbor",
    "enemy.found": "1 if an enemy ship is in YOUR ship's sight",
    "enemy.x": "nearest visible enemy x", "enemy.y": "nearest visible enemy y",
    "enemy.dist": "distance to that enemy",
    "enemy.laden": "1 if it is carrying cargo",
    "enemy.power": "its combat power",
    "enemy.stronger": "1 if it outguns you",
    "ally.found": "1 if a fleet-mate is in sight",
    "ally.dist": "distance to the nearest fleet-mate",
    "node.found": "1 if your charts show a stocked island",
    "node.x": "nearest believed-stocked island x", "node.y": "…y",
    "node.dist": "distance to it", "node.stock": "believed stock there",
    "rival.found": "1 if orders.target_fleet names a living rival",
    "rival.x": "that rival's harbor x", "rival.y": "that rival's harbor y",
    "orders.rally_x": "rally x from your squadron's standing orders",
    "orders.rally_y": "rally y from standing orders",
    "orders.aggression": "aggression from standing orders (0-3)",
    "orders.retreat": "retreat_hull_pct from standing orders",
}

ACTIONS = {
    "goto": (2, "sail toward (x, y)"),
    "gather": (0, "gather if ON a stocked island (else holds)"),
    "attack": (0, "close on the nearest visible enemy (guns fire automatically in range)"),
    "flee": (0, "run directly away from the nearest visible enemy"),
    "home": (0, "make for your harbor (deposits/repairs/new orders in the circle)"),
    "hold": (0, "hold position"),
}

FUNCS = {"min": 2, "max": 2, "abs": 1, "dist": 4}

MAX_LINES = 64
BUDGET = 1500                  # expression-node evaluations per ship per tick


class HelmError(Exception):
    def __init__(self, msg, line=None):
        super().__init__(f"line {line}: {msg}" if line else msg)
        self.line = line


def api_reference():
    """The API card admirals receive — generated from the same tables the
    interpreter runs on, so documentation can never drift from behavior."""
    s = "\n".join(f"  {k:<18} {v}" for k, v in SENSORS.items())
    a = "\n".join(f"  helm.{k}({', '.join('xy'[i] for i in range(n)) if n else ''})"
                  f"  — {d}" for k, (n, d) in ACTIONS.items())
    return f"""
SHIP PROGRAMMING (the helm language) — each ship is a machine; you write the
control program. Send "programs": {{"A": "<script>"}} alongside orders — same
delivery physics as orders (harbor circle / signal). One program per squadron;
every ship runs its own instance with its own mem. An empty string removes the
program (ship reverts to its standing-orders role).

Statements (one per line, run TOP-DOWN every tick; first true `when` acts):
  mem <name> = <number>      persistent per-ship variable (initialized once)
  set <name> = <expr>        assignment, executes when reached
  when <expr>: <action>      condition -> action; first hit ends the tick
  default: <action>          fallback (put it last)
  # comment

Expressions: + - * / %  · == != < <= > >=  · and or not · ( ) ·
  min(a,b) max(a,b) abs(a) dist(x1,y1,x2,y2)

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
  when self.cargo >= self.hold_cap: helm.home()
  when enemy.found and enemy.stronger and enemy.dist < 8: helm.flee()
  when node.found and node.dist == 0: helm.gather()
  when node.found: helm.goto(node.x, node.y)
  default: helm.goto(orders.rally_x, orders.rally_y)
"""


# ---------------- tokenizer / parser ----------------

TOKEN = re.compile(r"\s*(>=|<=|==|!=|[-+*/%()<>:,=]|\d+\.?\d*|[A-Za-z_][A-Za-z_0-9]*(?:\.[A-Za-z_][A-Za-z_0-9]*)?)")


def _tokens(s, line):
    out, i = [], 0
    while i < len(s):
        m = TOKEN.match(s, i)
        if not m:
            raise HelmError(f"cannot read {s[i:i+10]!r}", line)
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
            raise HelmError(f"expected {want or 'more'}, got {tok!r}", self.line)
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
            return ("num", float(tok))
        if tok in FUNCS:
            self.take("(")
            args = [self.expr()]
            while self.peek() == ",":
                self.take()
                args.append(self.expr())
            self.take(")")
            if len(args) != FUNCS[tok]:
                raise HelmError(f"{tok}() takes {FUNCS[tok]} args", self.line)
            return ("fn", tok, args)
        if tok in SENSORS:
            return ("sensor", tok)
        if tok.startswith("mem."):
            name = tok[4:]
            if name not in self.decls:
                raise HelmError(f"mem.{name} not declared (add: mem {name} = 0)",
                                self.line)
            return ("mem", name)
        raise HelmError(f"unknown name {tok!r} — see the sensor list", self.line)


def _parse_action(p):
    tok = p.take()
    if tok != "helm" and not tok.startswith("helm."):
        raise HelmError(f"action must be helm.<verb>(...), got {tok!r}", p.line)
    verb = tok[5:] if tok.startswith("helm.") else p.take()
    if verb not in ACTIONS:
        raise HelmError(f"unknown action helm.{verb} — one of {list(ACTIONS)}", p.line)
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
        raise HelmError(f"helm.{verb} takes {nargs} args", p.line)
    if p.peek() is not None:
        raise HelmError(f"unexpected {p.peek()!r} after action", p.line)
    return (verb, args)


def compile_program(text):
    """text -> Program. Raises HelmError (with line number) on any problem."""
    decls = {}
    stmts = []                             # ("set", name, expr) | ("when", expr, action)
    lines = text.splitlines()
    if len(lines) > MAX_LINES:
        raise HelmError(f"program exceeds {MAX_LINES} lines")
    for ln, raw in enumerate(lines, 1):
        s = raw.split("#", 1)[0].strip()
        if not s:
            continue
        if s.startswith("mem "):
            m = re.fullmatch(r"mem\s+([A-Za-z_][A-Za-z_0-9]*)\s*=\s*(-?\d+\.?\d*)", s)
            if not m:
                raise HelmError("mem syntax: mem <name> = <number>", ln)
            decls[m.group(1)] = float(m.group(2))
            continue
        if s.startswith("set "):
            m = re.match(r"set\s+([A-Za-z_][A-Za-z_0-9]*)\s*=\s*(.+)", s)
            if not m:
                raise HelmError("set syntax: set <name> = <expr>", ln)
            if m.group(1) not in decls:
                raise HelmError(f"set of undeclared {m.group(1)} "
                                f"(add: mem {m.group(1)} = 0)", ln)
            p = _P(_tokens(m.group(2), ln), ln, decls)
            e = p.expr()
            if p.peek() is not None:
                raise HelmError(f"unexpected {p.peek()!r}", ln)
            stmts.append(("set", m.group(1), e, ln))
            continue
        if s.startswith("when ") or s.startswith("default"):
            if s.startswith("default"):
                rest = s[len("default"):].lstrip()
                if not rest.startswith(":"):
                    raise HelmError("default syntax: default: <action>", ln)
                cond = ("num", 1.0)
                body = rest[1:].strip()
            else:
                if ":" not in s:
                    raise HelmError("when syntax: when <expr>: <action>", ln)
                cexpr, body = s[5:].split(":", 1)
                p = _P(_tokens(cexpr.strip(), ln), ln, decls)
                cond = p.expr()
                if p.peek() is not None:
                    raise HelmError(f"unexpected {p.peek()!r} in condition", ln)
                body = body.strip()
            pa = _P(_tokens(body, ln), ln, decls)
            action = _parse_action(pa)
            stmts.append(("when", cond, action, ln))
            continue
        raise HelmError(f"cannot parse {s[:30]!r} — statements are "
                        "mem/set/when/default", ln)
    if not any(st[0] == "when" for st in stmts):
        raise HelmError("program has no when/default statement — it can never act")
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
        Raises HelmError on budget exhaustion."""
        budget = [BUDGET]

        def ev(node):
            budget[0] -= 1
            if budget[0] <= 0:
                raise HelmError(f"instruction budget ({BUDGET}) exhausted")
            k = node[0]
            if k == "num":
                return node[1]
            if k == "sensor":
                return float(sensors.get(node[1], 0.0))
            if k == "mem":
                return float(mem.get(node[1], 0.0))
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
                return float(max(abs(a[0] - a[2]), abs(a[1] - a[3])))  # dist = cheb
            a, b = ev(node[1]), ev(node[2])
            if k == "+":
                return a + b
            if k == "-":
                return a - b
            if k == "*":
                return a * b
            if k == "/":
                return a / b if b else 0.0
            if k == "%":
                return a % b if b else 0.0
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

        for st in self.stmts:
            if st[0] == "set":
                mem[st[1]] = ev(st[2])
            else:
                if ev(st[1]):
                    verb, argnodes = st[2]
                    return (verb, [ev(x) for x in argnodes], st[3])
        return None
