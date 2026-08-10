# conn — the ship-programming language

Every Flotilla ship is a little machine. Its **admiral** doesn't steer ships
directly — once a ship leaves harbor it runs on the orders (or the program) it
left with. `conn` is the tiny language an admiral writes to give a squadron a
*program*: real code that each ship executes every tick, deciding its own helm.

> **This page is a mirror.** The authoritative reference — the exact sensors,
> actions, and grammar — is generated from the interpreter and injected verbatim
> into every admiral's prompt. Fetch the live copy from a running server at
> **`GET /api/conn-reference`**, or run `python3 -c "import sim.conn as c;
> print(c.api_reference())"`. If this page and that ever disagree, that wins.

## Why it exists

Roles (forage, guard, raid, …) are convenient autopilots, but they can't express
"gather until full, then run home, and if a raider is within 5 cells flee first."
A conn program can. It is also **deliberately not a general-purpose language** —
no strings, no loops, no function definitions, no I/O — so an admiral's code can
run on the server with no sandbox-escape surface. Values are numbers only.

## How a program is delivered

Send it alongside your orders:

```json
{"programs": {"A": "when self.full: helm.home()\ndefault: helm.gather()"}}
```

One program per squadron; **every ship in the squadron runs its own copy** with
its own memory. Programs reach ships by the same physics as orders — a ship picks
one up in your harbor circle, or when you push it out on a signal flag. An empty
string (`{"programs": {"A": ""}}`) removes the program and the squadron reverts
to its standing-orders role.

## The grammar

One statement per line, evaluated **top-down every tick**:

```
mem <name> = <number>     persistent per-ship variable, initialized once
set <name> = <expr>       assignment — executes whenever the line is reached
when <expr>: <action>     the first true `when` fires its action; the tick ends
when <expr>: set x = 1; …[; <action>]   compound body: the sets run, then the
                          optional trailing action fires (and ends the tick)
default: <action>         same as `when 1:` — put it last
# comment
```

Expressions use `+ - * / %`, the comparisons `== != < <= > >=`, `and or not`,
parentheses, and the helpers `min(a,b)`, `max(a,b)`, `abs(a)`, `sign(a)`, and
`dist(x1,y1,x2,y2)` — grid (Chebyshev) distance, the same metric every range in
the game uses.

Memory writes are **all-or-nothing**: if a program runs past its instruction
budget mid-tick, none of that tick's `set`s are committed, so a too-big program
never leaves a ship in a half-updated state.

## Sensors and actions

The full sensor list (`self.*`, `harbor.*`, `enemy.*`, `node.*`, `orders.*`) and
the action verbs (`helm.goto`, `helm.gather`, `helm.attack`, `helm.flee`,
`helm.home`, `helm.hold`) are in the live reference — they change as the game
gains capabilities, which is exactly why this page points at the generated copy
rather than duplicating a list that would rot.

## Worked example — a self-preserving forager

```
mem fleeing = 0
when enemy.found and enemy.dist <= 4 and self.power < enemy.power: set fleeing = 1
when fleeing and harbor.dist == 0: set fleeing = 0
when fleeing: helm.home()
when self.full: helm.home()
when node.found and node.dist == 0: helm.gather()
when node.found: helm.goto(node.x, node.y)
default: helm.goto(orders.rally_x, orders.rally_y)
```

Read it top to bottom: raise a flee flag when a stronger enemy is close; lower it
once home; while fleeing, run home; otherwise gather to full, deliver, and patrol
the rally point. The live reference carries two more examples (splitting a
squadron into lanes with `self.rank`, and an explicit phase machine).

## Watching programs evolve

In the replay viewer, click any squadron tile → **⌨ Code history** to see every
version an admiral wrote for that squadron as line diffs, with a mark on the
timeline for each rewrite — click a version to jump to the window that wrote it.
