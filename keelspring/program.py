"""The ship-program interpreter core — game-agnostic (split Stage 1e).

A tiny, deterministic, instruction-budgeted language: tokenizer, parser,
compiler, and evaluator, with the error pedagogy (line numbers, did-you-mean)
built in. The VOCABULARY is the game's: SENSORS (readable values) and ACTIONS
(verbs + arity + doc) are installed by the game at registration — sim/conn.py
does it for Flotilla — and validated against at compile time. FUNCS and the
size/step budgets are language machinery and live here.
"""
import difflib
import math
import re

# installed by the game (dicts of {name: doc} / {verb: (arity, doc)});
# compile_program validates against them, so they must be installed before use
SENSORS = {}
ACTIONS = {}

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
