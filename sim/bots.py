"""Scripted admirals for P0 balance work.

Contract (identical to future LLM admirals): decide(summary, rng) -> actions dict:
  {orders: {squad: {role, rally, aggression, retreat_hull_pct, target_fleet}},
   build: [{preset, squad}], signal: bool, thoughts: str}
Bots must be deterministic given (summary, rng).
"""


def _counts(summary):
    c = {}
    for s in summary["you"]["ships"]:
        c[s["squad"]] = c.get(s["squad"], 0) + 1
    return c


def _richest_enemy(summary):
    scores = summary["scores"]
    alive = [f for f in summary["alive"] if f != summary["you"]["fleet"]]
    if not alive:
        return None
    return max(alive, key=lambda f: (scores.get(f, 0), -f))


def _weakest_enemy(summary):
    alive = [f for f in summary["alive"] if f != summary["you"]["fleet"]]
    if not alive:
        return None
    return min(alive, key=lambda f: (summary["scores"].get(f, 0), f))


def _mid(a, b):
    return ((a[0] + b[0]) // 2, (a[1] + b[1]) // 2)


class Merchant:
    """Economy-max: many traders, a scout, guards only after being hit."""
    name = "merchant"

    def decide(self, summary, rng):
        you = summary["you"]
        c = _counts(summary)
        build = []
        budget = you["cargo"]
        want_guards = 2 if you["recent_hits"] > 0 else 0
        while budget >= 15 and len(build) + you["builds"] < 3:
            if c.get("C", 0) + sum(1 for b in build if b["squad"] == "C") < want_guards:
                build.append(dict(preset="frigate", squad="C"))
            elif c.get("A", 0) + sum(1 for b in build if b["squad"] == "A") < 8:
                build.append(dict(preset="trader", squad="A"))
            elif c.get("B", 0) < 1:
                build.append(dict(preset="scout", squad="B"))
            else:
                break
            budget -= 15
        # forage toward the richest believed node cluster
        nodes = [n for n in summary["nodes"] if n["believed"] > 0]
        rally = you["harbor"]
        if nodes:
            hx, hy = you["harbor"]
            best = max(nodes, key=lambda n: (n["believed"] - abs(n["x"] - hx)
                                             - abs(n["y"] - hy), -n["id"]))
            rally = (best["x"], best["y"])
        orders = {
            "A": dict(role="forage", rally=rally, aggression=0, retreat_hull_pct=50),
            "B": dict(role="scout", rally=(48, 27), aggression=0, retreat_hull_pct=60),
            "C": dict(role="guard", rally=rally if you["recent_hits"] else you["harbor"],
                      aggression=2, retreat_hull_pct=25),
        }
        th = f"Trade winds favor us — {c.get('A', 0)} traders on the {rally} grounds."
        if you["recent_hits"]:
            th = "We were hit! Commissioning frigates to guard the harbor."
        return dict(orders=orders, build=build, signal=False, thoughts=th)


class Corsair:
    """Aggro: lean economy, early raiders on the richest enemy, assault when ahead."""
    name = "corsair"

    def decide(self, summary, rng):
        you = summary["you"]
        c = _counts(summary)
        target = _richest_enemy(summary)
        build = []
        budget = you["cargo"]
        raiders = c.get("D", 0) + c.get("E", 0)
        while budget >= 15 and len(build) + you["builds"] < 3:
            have = lambda sq: c.get(sq, 0) + sum(1 for b in build if b["squad"] == sq)
            if have("A") < 3:
                build.append(dict(preset="trader", squad="A"))
            elif have("D") < 5:
                build.append(dict(preset="raider", squad="D"))
            elif have("E") < 6:
                build.append(dict(preset="raider", squad="E"))
            elif have("A") < 6:
                build.append(dict(preset="trader", squad="A"))
            else:
                break
            budget -= 15
        orders = {
            "A": dict(role="forage", rally=you["harbor"], aggression=1, retreat_hull_pct=40),
            "B": dict(role="scout", rally=(48, 27), aggression=0, retreat_hull_pct=60),
            "C": dict(role="guard", rally=you["harbor"], aggression=2, retreat_hull_pct=25),
        }
        th = "Building the raiding pack."
        signal = False
        if target is not None:
            hb = summary["harbors"].get(target)
            if hb:
                orders["D"] = dict(role="raid", rally=_mid(you["harbor"], hb), aggression=2,
                                   retreat_hull_pct=30, target_fleet=target)
                th = f"Hunting fleet {target}'s cargo lanes."
                staging = _mid(_mid(you["harbor"], hb), you["harbor"])   # quarter-way out
                staged = sum(1 for s in you["ships"]
                             if s["squad"] == "E" and s["role"] != "assault")
                if staged >= 5:
                    # the wave is massed at the staging point — hoist signals so the flip
                    # to assault reaches them AT SEA (they never visit port). Next window
                    # the standing order reverts to staging, so new builds mass again.
                    orders["E"] = dict(role="assault", rally=hb, aggression=3,
                                       retreat_hull_pct=15, target_fleet=target)
                    signal = True
                    th = f"Signals up: strike wave of {staged} — burn fleet {target}'s flagship!"
                else:
                    orders["E"] = dict(role="guard", rally=staging, aggression=1,
                                       retreat_hull_pct=40)
        return dict(orders=orders, build=build, signal=signal, thoughts=th)


class Admiralty:
    """Balanced: solid economy, escorts, punish the leader opportunistically."""
    name = "admiralty"

    def decide(self, summary, rng):
        you = summary["you"]
        c = _counts(summary)
        build = []
        budget = you["cargo"]
        while budget >= 15 and len(build) + you["builds"] < 3:
            have = lambda sq: c.get(sq, 0) + sum(1 for b in build if b["squad"] == sq)
            if have("A") < 4:
                build.append(dict(preset="trader", squad="A"))
            elif have("B") < 1:
                build.append(dict(preset="scout", squad="B"))
            elif have("C") < 2:
                build.append(dict(preset="frigate", squad="C"))
            elif have("E") < 2:
                build.append(dict(preset="frigate", squad="E"))
            elif have("D") < 3:
                build.append(dict(preset="raider", squad="D"))
            elif have("A") < 7:
                build.append(dict(preset="trader", squad="A"))
            else:
                break
            budget -= 15
        hx, hy = you["harbor"]
        near = [n for n in summary["nodes"] if n["believed"] > 0]
        grounds = you["harbor"]
        if near:
            g = min(near, key=lambda n: (abs(n["x"] - hx) + abs(n["y"] - hy), n["id"]))
            grounds = (g["x"], g["y"])
        orders = {
            "A": dict(role="forage", rally=you["harbor"], aggression=1, retreat_hull_pct=45),
            "B": dict(role="scout", rally=(48, 27), aggression=0, retreat_hull_pct=60),
            "C": dict(role="guard", rally=you["harbor"], aggression=2, retreat_hull_pct=25),
            "E": dict(role="guard", rally=grounds, aggression=2, retreat_hull_pct=30),
        }
        th = "Steady as she goes: convoy pickets on the grounds, a balanced book."
        leader = _richest_enemy(summary)
        my = summary["scores"][you["fleet"]]
        if leader is not None and summary["scores"][leader] > my + 40 and c.get("D", 0) >= 2:
            hb = summary["harbors"].get(leader)
            if hb:
                orders["D"] = dict(role="raid", rally=_mid(you["harbor"], hb), aggression=2,
                                   retreat_hull_pct=35, target_fleet=leader)
                th = f"Fleet {leader} runs ahead — dispatching privateers to tax them."
        return dict(orders=orders, build=build, signal=False, thoughts=th)


class Turtle:
    """Defensive hoarder: local nodes only, thick harbor guard."""
    name = "turtle"

    def decide(self, summary, rng):
        you = summary["you"]
        c = _counts(summary)
        build = []
        budget = you["cargo"]
        while budget >= 15 and len(build) + you["builds"] < 3:
            have = lambda sq: c.get(sq, 0) + sum(1 for b in build if b["squad"] == sq)
            if have("C") < 2:
                build.append(dict(preset="frigate", squad="C"))
            elif have("A") < 6:
                build.append(dict(preset="trader", squad="A"))
            elif have("E") < 2:
                build.append(dict(preset="frigate", squad="E"))
            else:
                break
            budget -= 15
        hx, hy = you["harbor"]
        near = [n for n in summary["nodes"]
                if n["believed"] > 0 and abs(n["x"] - hx) + abs(n["y"] - hy) <= 30]
        rally = (near[0]["x"], near[0]["y"]) if near else you["harbor"]
        orders = {
            "A": dict(role="forage", rally=rally, aggression=0, retreat_hull_pct=60),
            "B": dict(role="scout", rally=you["harbor"], aggression=0, retreat_hull_pct=60),
            "C": dict(role="guard", rally=you["harbor"], aggression=2, retreat_hull_pct=20),
            "E": dict(role="guard", rally=rally, aggression=2, retreat_hull_pct=25),
        }
        return dict(orders=orders, build=build, signal=False,
                    thoughts="Hold fast. The harbor is our fortress; the shallows feed us.")


BOTS = {b.name: b for b in (Merchant(), Corsair(), Admiralty(), Turtle())}
