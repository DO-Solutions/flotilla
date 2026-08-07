"""The Flotilla configuration schema — the single source of truth for every knob.

Everything derives from SCHEMA: engine defaults, the dashboard's Configure form,
the generated CONFIG.md, the published config-schema.json, and the resolved
config stamped into every replay's meta. Agent-first: an agent reads
config-schema.json, writes a config JSON, and runs `python3 sim/run_config.py
config.json` — same power as the GUI, no extra ceremony.
"""

SCHEMA = {
    "world": {
        "width":       dict(d=96,   t="int", lo=48, hi=192, doc="Map width in cells."),
        "height":      dict(d=54,   t="int", lo=27, hi=108, doc="Map height in cells."),
        "fish_sites":  dict(d=4,    t="int", lo=1,  hi=10,  doc="Fish shoals per map quadrant (mirrored 4-way, so total = 4x this)."),
        "fish_cap":    dict(d=40,   t="int", lo=5,  hi=200, doc="Max cargo a fish shoal holds."),
        "fish_regen_period": dict(d=50, t="int", lo=5, hi=1000, doc="Ticks per +1 cargo regen on fish shoals (10 ticks = 1s)."),
        "wreck_cap":   dict(d=60,   t="int", lo=0,  hi=300, doc="Cargo in each of the two contested center wrecks."),
        "start_cargo": dict(d=30,   t="int", lo=0,  hi=500, doc="Each fleet's starting treasury."),
        "flag_hull":   dict(d=200,  t="int", lo=50, hi=1000, doc="Flagship hull points. Destroying a flagship eliminates its fleet."),
        "harbor_r":    dict(d=3,    t="int", lo=1,  hi=12,  doc="Harbor command-circle radius: any ship inside it deposits cargo, repairs, and picks up the admiral's latest standing orders."),
        "contact_ttl": dict(d=1200, t="int", lo=0,  hi=60000, doc="Ticks an enemy CONTACT stays on the admiral's plot after last sighting (state.enemies entries carry age_s; stale contacts keep last-known position/type/laden). 0 = only currently-visible enemies are reported."),
    },
    "economy": {
        "ship_cost":   dict(d=15,   t="int", lo=1,  hi=100, doc="Cargo cost to build any ship."),
        "build_ticks": dict(d=100,  t="int", lo=10, hi=1000, doc="Ticks to build one ship (queue max 3)."),
        "gather_period": dict(d=4,  t="int", lo=1,  hi=50,  doc="Ticks per 1 cargo gathered while sitting on a node."),
        "signal_cost": dict(d=5,    t="int", lo=0,  hi=100, doc="Cargo cost to hoist a signal flag."),
        "signal_cd":   dict(d=3,    t="int", lo=0,  hi=20,  doc="Cooldown between signal hoists, in decision windows."),
        "signal_mode": dict(d="return_only", t="enum", opts=["return_only", "preset", "custom"],
                            doc="What signal flags can say. return_only: the ONE built-in flag — RETURN TO PORT (per-squad or all); recalled ships sail home, collect your latest orders in the harbor circle, and head back out. preset: return + user-defined named flags (signal_presets) whose baked-in orders apply instantly at sea. custom: a hoist pushes your current standing orders to every ship at sea (the classic instant push, payload capped by signal_max_chars)."),
        "signal_max_chars": dict(d=400, t="int", lo=40, hi=4000, doc="custom mode: max JSON characters of standing orders one hoist may push; an oversized hoist is refused."),
        "signal_presets": dict(d="", t="str", doc='preset mode: JSON object of named flags, e.g. {"Strike": {"E": {"role": "assault", "target_fleet": 1}}, "Regroup": {"D": {"role": "guard", "rally": [20, 20]}}}. Hoisting a flag applies its orders to those squads instantly at sea AND sets them as the squads\' standing orders. Invalid JSON fails loudly at match start.'),
        "repair_period": dict(d=5,  t="int", lo=1,  hi=50,  doc="Ticks per +2 hull repaired while docked."),
        "refit_cost":  dict(d=8,    t="int", lo=0,  hi=100, doc="Cargo cost to REFIT a docked ship to another class. Refit directives are per-squadron standing orders (\"refit\": {\"A\": \"frigate\"}); ships convert as they dock until the directive is cleared (null)."),
        "refit_ticks": dict(d=0,    t="int", lo=0,  hi=2000, doc="Drydock time per refit: the ship is held in port this many ticks before the conversion completes (paid up front). 0 = immediate."),
    },
    "combat": {
        "volley":      dict(d=5,    t="int", lo=1,  hi=50,  doc="Ticks between shots for every gun."),
        "kill_score":  dict(d=8,    t="int", lo=0,  hi=100, doc="Score per enemy ship sunk (timed_score mode)."),
        "flag_kill_score": dict(d=150, t="int", lo=0, hi=1000, doc="Score for destroying a flagship (timed_score mode)."),
        "leash":       dict(d=12,   t="int", lo=2,  hi=60,  doc="ROLE-driven ships only: max cells a ship pursues an enemy from its station. Conn-programmed ships make their own pursuit decisions and ignore this."),
    },
    "pacing": {
        "window":      dict(d=100,  t="int", lo=20, hi=1000, doc="Ticks between admiral decision windows (100 = every 10s of game time)."),
        "max_ticks":   dict(d=6000, t="int", lo=500, hi=60000, doc="Match length in ticks (6000 = 10 minutes)."),
        "territory_tick": dict(d=50, t="int", lo=10, hi=500, doc="Territory mode: ticks between control updates/scoring (+1 per held region)."),
    },
    "scenario": {
        "parley":      dict(d=True, t="bool", doc="Admiral-to-admiral messaging (diplomacy). Off = no communication between fleets: pure play skill, no negotiation."),
        "voyage_reports": dict(d=True, t="bool", doc="Ships file a one-line voyage report when they return to the harbor circle (time out, cargo delivered, gathered, hits taken, range) — delivered in the admiral's next state.reports and recorded for post-game review."),
        "allow_designs": dict(d=True, t="bool", doc="Admirals may DESIGN custom ship classes mid-game (\"designs\": {\"name\": {speed,hold,guns,armor,hull,lookout}}): every stat >=1, total == design_points, max 4 classes/fleet. Built and refitted by name like built-ins; the viewer derives each class's silhouette from its stats."),
        "design_points": dict(d=12, t="int", lo=6, hi=30, doc="Stat-point budget every ship class must total — built-ins use 12."),
        "ship_designs": dict(d="", t="str", doc='Operator-defined EXTRA classes available to ALL fleets from the start (symmetric): JSON like {"corvette": {"speed":4,"hold":1,"guns":2,"armor":2,"hull":2,"lookout":1}}. Same validation as live designs; invalid JSON fails loudly at match start.'),
        "programs":    dict(d=True, t="bool", doc="Ship programming: admirals may write conn-language control programs per squadron (the real coding challenge — the API reference is injected into their prompts). Off = standing-order roles only."),
        "role_fallback": dict(d=False, t="bool", doc="Built-in role autopilot for unprogrammed ships. OFF (default): ships execute ONLY conn programs — an unprogrammed ship sits idle in port (still deposits/repairs); standing orders serve purely as orders.* parameters. ON: unprogrammed ships run their standing-order role (the classic behavior)."),
        "roles_allowed": dict(d="", t="str", doc='role_fallback=on only: comma-separated subset of roles admirals may assign (e.g. "forage,scout,guard"). Empty = all of forage/scout/escort/raid/guard/blockade/assault.'),
        "scuttle":     dict(d=True, t="bool", doc="Admirals may SCUTTLE any of their ships anywhere ('scuttle': [ship ids]) for scuttle_value treasury each — the mid-game budget-lock escape hatch. A laden ship's cargo sinks as a wreck where it goes down."),
        "scuttle_value": dict(d=5,  t="int", lo=0, hi=50, doc="Treasury recovered per scuttled ship."),
        "income_amount": dict(d=0,  t="int", lo=0, hi=100, doc="Passive income: every fleet receives this much treasury every income_period ticks. 0 = off. Another budget-lock relief valve."),
        "income_period": dict(d=100, t="int", lo=10, hi=5000, doc="Ticks between passive income payments."),
        "program_chars": dict(d=1500, t="int", lo=200, hi=8000, doc="Max characters per squadron program; oversized programs are rejected at delivery."),
        "win":         dict(d="timed_score", t="enum", opts=["timed_score", "territory"],
                            doc="Victory condition. timed_score: cargo hauled + kills. territory: control points from holding named regions."),
        "regions":     dict(d=16,   t="int", lo=4,  hi=48,  doc="Territory mode: number of named control regions."),
        "description": dict(d="",   t="str", doc="Shown to admirals in state.scenario. Leave empty to auto-generate from the win condition."),
    },
    "admirals": {
        "temperature": dict(d=0.2,  t="float", lo=0.0, hi=1.5, doc="LLM sampling temperature for decisions."),
        "max_tokens":  dict(d=700,  t="int", lo=100, hi=4000, doc="Max tokens per decision response."),
        "timeout_s":   dict(d=90,   t="int", lo=5,  hi=600, doc="Seconds an admiral gets to answer a decision window; slower = orders stand (missed window)."),
        "think":       dict(d=False, t="bool", doc="Allow reasoning models their hidden thinking (slower, possibly smarter). Off = fair fast default."),
        "history_chars": dict(d=8000, t="int", lo=0, hi=60000, doc="Character budget for the admiral's full-game memory in every prompt: its campaign journal (every past thought) + the complete parley transcript. Oldest entries drop first when over budget. 0 disables in-game history."),
        "memo_chars":  dict(d=1200, t="int", lo=200, hi=8000, doc="Hard character cap for post-game strategy memos, opening plans, AND per-admiral custom prompts. Models are told this budget explicitly; text is cut at the limit."),
        "scratchpad":  dict(d=True, t="bool", doc="Admirals get a persistent scratchpad: a freeform note they can fully rewrite any window (response field \"scratchpad\"), always in their context and handed to the post-game review."),
        "scratchpad_chars": dict(d=2000, t="int", lo=0, hi=10000, doc="Character cap for the scratchpad."),
        "warmup":      dict(d=True, t="bool", doc="Pre-game planning phase: before window 0 each admiral studies the rules + its opening view and writes an OPENING PLAN (kept in context all game, shown in the replay)."),
        "warmup_timeout_s": dict(d=120, t="int", lo=10, hi=600, doc="Seconds for the warmup planning call — deliberately longer than the in-game window."),
    },
    "series": {
        "games":       dict(d=3,    t="int", lo=1,  hi=20,  doc="Games in the series."),
        "memos":       dict(d=True, t="bool", doc="Between games, each admiral studies its own record and writes a strategy memo carried into the next game."),
        "vary_seeds":  dict(d=False, t="bool", doc="New map each game. Off = same map, sharper learning signal."),
        "debrief_timeout_s": dict(d=300, t="int", lo=30, hi=900, doc="Seconds for the post-game memo call. Care beats speed here."),
    },
    "tournament": {
        "format":      dict(d="round_robin", t="enum",
                            opts=["round_robin", "random_pairs", "single_elim"],
                            doc="Bracket structure. round_robin: every combination plays. random_pairs: seeded random pairings per round. single_elim: knockout bracket (2-player matchups, participants must be a power of 2)."),
        "players_per_match": dict(d=2, t="enum", opts=[2, 4], doc="Fleets per matchup (single_elim is always 2)."),
        "games_per_match": dict(d=1, t="int", lo=1, hi=9, doc="Games per matchup; odd numbers avoid ties (winner by wins, then total score)."),
        "rounds":      dict(d=1,    t="int", lo=1,  hi=10,  doc="random_pairs only: how many rounds of fresh pairings."),
        "memo_policy": dict(d="per_series", t="enum", opts=["none", "per_series", "persistent"],
                            doc="none: fresh mind every game. per_series: memos carry within a matchup, reset between. persistent: an admiral keeps its notes across the WHOLE tournament — win a series, carry the lessons into the next bracket."),
    },
}


def defaults():
    return {k: spec["d"] for sec in SCHEMA.values() for k, spec in sec.items()}


def resolve(overrides=None):
    """Merge overrides onto defaults; unknown keys rejected loudly (agents get a
    clear error, not silent misconfiguration). Values are clamped to bounds."""
    cfg = defaults()
    known = set(cfg)
    for k, v in (overrides or {}).items():
        if k in ("rules",):
            continue
        if k not in known:
            raise KeyError(f"unknown config key '{k}' — see config-schema.json")
        spec = next(s[k] for s in SCHEMA.values() if k in s)
        if spec["t"] == "int":
            v = max(spec["lo"], min(spec["hi"], int(v)))
        elif spec["t"] == "float":
            v = max(spec["lo"], min(spec["hi"], float(v)))
        elif spec["t"] == "enum" and v not in spec["opts"]:
            raise ValueError(f"config key '{k}' must be one of {spec['opts']}, got {v!r}")
        elif spec["t"] == "bool":
            v = bool(v)
        cfg[k] = v
    return cfg


def schema_json():
    import json
    return json.dumps({sec: {k: {kk: vv for kk, vv in spec.items()}
                             for k, spec in knobs.items()}
                       for sec, knobs in SCHEMA.items()}, indent=1)


def config_md():
    out = ["# Flotilla configuration reference",
           "",
           "Every knob, its default, bounds, and effect. Agent-first: read",
           "`config-schema.json` (same content, machine-readable), write a config",
           "JSON with any subset of these keys, run `python3 sim/run_config.py",
           "your-config.json`. Unknown keys fail loudly; values clamp to bounds.",
           ""]
    for sec, knobs in SCHEMA.items():
        out.append(f"## {sec}")
        out.append("")
        out.append("| key | default | range | effect |")
        out.append("|---|---|---|---|")
        for k, s in knobs.items():
            rng = f"{s['lo']}–{s['hi']}" if "lo" in s else \
                  " / ".join(map(str, s["opts"])) if "opts" in s else "text"
            out.append(f"| `{k}` | `{s['d']}` | {rng} | {s['doc']} |")
        out.append("")
    return "\n".join(out)


if __name__ == "__main__":
    import sys
    if "--json" in sys.argv:
        print(schema_json())
    else:
        print(config_md())
