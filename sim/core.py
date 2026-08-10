"""Flotilla sim core — deterministic tick engine.

Determinism contract: integer state only, seeded random.Random, ships iterated in id
order. Same seed + same admiral decisions => identical match (verified by the
harness double-run hash). Wall-clock exists in exactly one place — catch-up
pipelining's window pacing (_pipe_window) — and it only affects WHEN decisions
land, never how the recorded decisions replay.
"""
import json
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import conn
from config_schema import resolve as _resolve_cfg

# --- tunables (balance knobs live here; the harness sweeps matches against them) ---
TICKS_PER_SEC = 10
WINDOW = 100            # ticks between admiral decision windows (10s)
MAX_TICKS = 6000        # 10 min
W, H = 96, 54
HARBOR_R = 3
SHIP_COST = 15
BUILD_TICKS = 100
GATHER_PERIOD = 4       # 1 cargo per N ticks while on a node
VOLLEY = 5              # ticks between shots
REPAIR_PERIOD = 5       # docked: +2 hull per N ticks
MOVE_THRESH = 8         # move_acc >= this -> step one cell
SIGNAL_COST = 5
SIGNAL_CD = 3           # windows
FRAME_EVERY = 2         # record a frame every N ticks
LEASH = 12              # max pursuit distance from role anchor
KILL_SCORE = 8
FLAG_KILL_SCORE = 150
START_CARGO = 30
FISH_CAP = 40
FISH_REGEN_PERIOD = 50  # +1 per N ticks
WRECK_CAP = 60

PRESETS = {  # 12 points each: speed, hold, guns, armor, hull, lookout
    "trawler": dict(speed=3, hold=5, guns=1, armor=1, hull=1, lookout=1),
    "raider":  dict(speed=4, hold=1, guns=3, armor=2, hull=1, lookout=1),
    "frigate": dict(speed=2, hold=1, guns=4, armor=3, hull=1, lookout=1),
    "scout":   dict(speed=5, hold=1, guns=1, armor=1, hull=1, lookout=3),
}
ROLES = ("forage", "scout", "escort", "raid", "guard", "blockade", "assault")
ISLANDS = [  # small tropical islands — every resource node gets one (seed-shuffled)
    "Aitutaki", "Bora Bora", "Rarotonga", "Moorea", "Huahine", "Tahaa", "Maupiti",
    "Manihiki", "Penrhyn", "Tokelau", "Funafuti", "Niue", "Palmerston", "Suwarrow",
    "Mangaia", "Atiu", "Mitiaro", "Mauke", "Rakahanga", "Pukapuka", "Fakaofo",
    "Nukunonu", "Atafu", "Kiritimati", "Tabuaeran", "Teraina", "Malden", "Starbuck",
    "Vostok", "Flint", "Caroline", "Tongareva", "Tematangi", "Hao", "Makemo",
    "Fakarava", "Rangiroa", "Tikehau", "Mataiva", "Anaa", "Kauehi", "Taenga",
    "Nihiru", "Marutea", "Tepoto", "Napuka", "Ahe", "Manihi"]
DEFAULT_ORDER = dict(role="guard", rally=None, aggression=1, retreat_hull_pct=35,
                     target_fleet=None)


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


def cheb(ax, ay, bx, by):
    return max(abs(ax - bx), abs(ay - by))


def sign(v):
    return (v > 0) - (v < 0)


class Ship:
    __slots__ = ("id", "fleet", "squad", "x", "y", "stats", "hull", "hull_max",
                 "cargo", "move_acc", "volley_cd", "attackers", "flag_attackers",
                 "wp_i", "preset", "orders",
                 "intent", "node_id", "haul_home", "recall", "recall_safe",
                 "program", "pmem",
                 "trip_start", "trip_gathered", "trip_hits", "trip_far",
                 "refit_to", "refit_at", "repair_at", "nav_prev", "moved_t",
                 "gained_t", "assault_flag")

    def __init__(self, sid, fleet, squad, x, y, preset, stats=None):
        self.id = sid
        self.fleet = fleet
        self.squad = squad
        self.x, self.y = x, y
        self.preset = preset
        self.stats = dict(stats if stats is not None else PRESETS[preset])
        self.hull_max = 20 + self.stats["hull"] * 10
        self.hull = self.hull_max
        self.cargo = 0
        self.move_acc = 0
        self.volley_cd = 0
        self.attackers = set()            # ship ids that hit her (kill credit)
        self.flag_attackers = set()       # fleet ids whose FLAGSHIP battery hit her
        self.assault_flag = False         # this tick's helm.assault() -> prioritize the enemy flag in combat
        self.wp_i = 0
        self.recall = False                 # RETURN NOW: straight home, no detours
        self.recall_safe = False            # RETURN SAFE: home now, evade danger en route
        self.refit_to = None                # class this ship is converting to
        self.refit_at = 0                   # tick the drydock work completes
        self.repair_at = 0                  # tick yard repairs complete (0 = none)
        self.trip_start = None              # None = in port; else departure tick
        self.trip_gathered = 0
        self.trip_hits = 0
        self.trip_far = 0
        self.program = None                 # compiled conn program (has the conn)
        self.pmem = {}                      # the program's per-ship memory
        self.orders = dict(DEFAULT_ORDER)   # per-ship copy; refreshed in port / by signal
        self.intent = ""                    # human/agent-readable "what am I doing"
        self.node_id = None                 # committed forage target (stops flip-flopping)
        self.haul_home = False              # threatened while laden -> commit to delivery
        self.nav_prev = None                # last cell (islands: no-backtrack steering)
        self.moved_t = 0                    # tick the ship last changed cell
        self.gained_t = 0                   # tick cargo last increased

    @property
    def hold_cap(self):
        return self.stats["hold"] * 3

    @property
    def vision(self):
        return 4 + self.stats["lookout"] * 2

    def power(self):
        return self.stats["guns"] * 2 + self.stats["armor"] + self.hull // 10


class Fleet:
    mode = "timed_score"                   # set by the engine at init

    def __init__(self, fid, name, bot, hx, hy):
        self.id = fid
        self.name = name
        self.bot = bot
        self.hx, self.hy = hx, hy          # harbor / flagship anchor
        self.flag_hull = 200
        self.cargo = START_CARGO
        self.bank = 0                      # deposited cargo (scoring)
        self.kills = 0                     # kill POINTS (kill_score/flag_kill_score)
        self.kill_count = 0                # ships actually sunk (for the prompt)
        self.alive = True
        self.pending = {}                  # squad -> the admiral's standing orders;
                                           # ships copy them in port or on a signal hoist
        self.pending_reassign = {}         # ship id -> squad (applies as the ship docks)
        self.flag_target = None            # flagship relocation destination
        self.flag_acc = 0                  # relocation movement accumulator
        self.flag_prev = None              # last cell (islands: no-backtrack)
        self.team = fid                    # team id (own id = free-for-all)
        self.build_q = []                  # list of (preset, squad) awaiting a slot
        self.builds = []                   # ACTIVE yard builds: [preset, squad, done_t]
        self.build_t = 0                   # legacy serialized-build clock (shipyard_slots=0)
        self.yards = 1                     # CURRENT yard slots (engine seeds from cfg)
        self.yard_done_t = 0               # tick the in-progress expansion completes
        self.signal_cd = 0
        self.node_mem = {}                 # node id -> (last seen remaining, tick seen)
        self.contacts = {}                 # enemy ship id -> last-sighting record
        self.inbox = []                    # parley messages, delivered at next window
        self.parley_log = []               # full transcript, both directions
        self.pending_programs = {}         # squad -> compiled conn Program
        self.pending_refits = {}           # squad -> class name (standing directive)
        self.designs = {}                  # this fleet's custom ship classes
        self.reports_pending = []          # voyage reports awaiting the next window
        self.queued_signal = None          # hoist waiting on funds
        self.warnings = []                 # per-window admiral warnings (funds etc.)
        self.territory = 0                 # territory-mode control points
        self.recent_hits = 0               # hits taken since last window (bot signal)
        self.combat = {}                   # (ship key, rival fleet) -> engagement
                                           # since last window (dealt/taken/where)

    def score(self):
        if self.mode == "territory":
            return self.territory
        if self.mode == "domination":
            return self.kills          # display + cap tiebreak; winning = surviving
        return self.bank + self.kills


class Node:
    __slots__ = ("id", "x", "y", "kind", "remaining", "cap", "regen_acc", "name")

    def __init__(self, nid, x, y, kind, amount, cap, name=""):
        self.id = nid
        self.x, self.y = x, y
        self.kind = kind                   # "fish" (regen) | "wreck" (finite)
        self.remaining = amount
        self.cap = cap
        self.regen_acc = 0
        self.name = name


DEFAULT_SCENARIO = dict(
    win="timed_score",       # timed_score (cargo+kills) | territory (control points)
                             # | domination (last admiral standing, no clock)
    territories=16,          # territory mode: number of control territories
    max_ticks=MAX_TICKS,
    description="Timed match: score = cargo hauled to port + 8/enemy ship sunk + "
                "150/flagship destroyed. Highest score at the bell wins; "
                "losing your flagship eliminates you.")

TERRITORY_TICK = 50          # control update + scoring cadence


class Engine:
    def __init__(self, players, seed, max_ticks=None, scenario=None):
        """players: list of (name, bot) — 2 to 8. Bot: .decide(summary, rng) -> actions."""
        self.rng = random.Random(seed)
        self.seed = seed
        self.cfg = _resolve_cfg(scenario)
        c = self.cfg
        if c["win"] == "domination" and "flag_salvage" not in (scenario or {}):
            # domination default: a 200-cargo prize snowballed the first kill
            # into the game; 50 rewards the strike without deciding the war.
            # An explicit flag_salvage in the config always wins.
            c["flag_salvage"] = 50
        self.W, self.H = c["width"], c["height"]
        self.hr = c["harbor_r"]
        self.max_ticks = max_ticks if max_ticks is not None else c["max_ticks"]
        if c["win"] == "domination" and max_ticks is None:
            self.max_ticks = c["domination_cap"]   # no clock — cap is a safety net
        if not c["description"]:
            if c["win"] == "domination":
                c["description"] = (
                    "CONQUEST match: there is NO clock and NO points-victory. The only "
                    "way to win: be the last admiral afloat — a fleet is eliminated when "
                    "its flagship is destroyed. Cargo funds ships but does not score; "
                    "kills alone win nothing while any rival flag flies. Survive, "
                    "outbuild, and destroy every enemy flagship.")
            elif c["win"] == "territory":
                c["description"] = (
                    "TERRITORIES match: the sea is split into named territories. Hold a "
                    "territory by being the only fleet with ships in it; the holder "
                    "keeps it until ALL its ships leave or sink while a rival's ship "
                    "remains. "
                    + (f"CAPTURING takes {c['territory_capture_ticks'] / 10:g}s of SOLE "
                       f"possession: your clock only runs while no enemy ship (the "
                       f"holder included) stands in the territory — clear the sea first. "
                       f"An enemy entering PAUSES your progress where it is; progress "
                       f"only resets if ALL your ships leave the territory. Ship counts "
                       f"never matter (state.regions shows contested_by + capture_pct). "
                       if c.get("territory_capture_ticks", 0) > 0 else "")
                    + "Score = +1 "
                    f"per held territory per {c['territory_tick'] / 10:g}s. Cargo funds "
                    "ships but does NOT score; kills do NOT score. Highest territory "
                    "score at the bell wins; losing your flagship still eliminates you. "
                    "GEOGRAPHY: every cell belongs to its NEAREST territory seat "
                    "(Chebyshev distance; ties by straight-line distance, then "
                    "the lower id) — state.regions "
                    "lists the seats, so you can work out which territory any "
                    "point is in. Ship programs can read terr.owner / terr.mine / "
                    "terr.capture for the territory under their keel.")
            else:
                c["description"] = (
                    f"SCORE match (timed): score = cargo hauled to port + {c['kill_score']}/enemy "
                    f"ship sunk + {c['flag_kill_score']}/flagship destroyed. Highest "
                    "score at the bell wins; losing your flagship eliminates you.")
        self.roles_allowed = set(ROLES)
        if c["roles_allowed"].strip():
            self.roles_allowed = {r.strip() for r in c["roles_allowed"].split(",")
                                  if r.strip()}
            bad = self.roles_allowed - set(ROLES)
            if bad:
                raise ValueError(f"roles_allowed contains unknown roles: {sorted(bad)}")
        self.shared_designs = {}           # operator classes, symmetric for all fleets
        if c["default_designs"]:
            raw = json.loads(c["default_designs"])       # invalid = fail loud
            if not isinstance(raw, dict):
                raise ValueError("default_designs must be a JSON object of classes")
            for name, st in raw.items():
                clean = self._clean_design(name, st)
                if clean is None:
                    raise ValueError(f"default_designs[{name!r}] invalid: every stat "
                                     f">=1, total == {c['design_points']}")
                self.shared_designs[name] = clean
        self.signal_presets = {}
        if c["signal_presets"]:
            self.signal_presets = json.loads(c["signal_presets"])   # invalid = fail loud
            if not isinstance(self.signal_presets, dict):
                raise ValueError("signal_presets must be a JSON object of named flags")
        sig_rules = (f"SIGNAL FLAGS (cost {c['signal_cost']} cargo, cooldown "
                     f"{c['signal_cd']} windows): ")
        if c["signal_mode"] == "return_only":
            sig_rules += (
                'the ONLY flags are RETURN TO PORT, in two urgencies: '
                '{"signal": {"return": "all"}} (or ["A","C"]) = NOW — ships '
                'drop everything and beeline home, even straight through a '
                'gun line; {"signal": {"return_safe": "all"}} = SAFE — ships '
                "turn back immediately too, but route DEFENSIVELY: any enemy "
                "met en route makes them break off and evade until clear, "
                "then resume homing (slower, but your trawlers do not sail "
                "through an attack force). Returned ships collect your "
                "latest standing orders inside the harbor circle, then "
                "execute them. There is NO instant orders-push: outside the "
                "circle, ships run on the orders they left with")
        elif c["signal_mode"] == "preset":
            sig_rules += (
                'RETURN TO PORT ({"signal": {"return": "all" or [squads]}} NOW, '
                '{"return_safe": ...} = same recall with defensive, evasive '
                'routing) plus your '
                f"pre-defined flags {list(self.signal_presets)} — hoist "
                '{"signal": {"hoist": "<FlagName>"}} to apply that flag\'s baked-in '
                "orders to its squads instantly at sea. No other message can be "
                "signalled")
        else:
            sig_rules += (
                'hoist {"signal": true} to push your CURRENT standing orders to every '
                'ship at sea instantly, or {"signal": {"return": "all" or [squads]}} '
                'to recall ships NOW ({"return_safe": ...} = the same recall '
                "with defensive, evasive routing home)")
        sig_rules += ("; an UNAFFORDABLE hoist is QUEUED and raises itself the moment "
                      "funds allow — you.warnings tells you every window; cancel with "
                      '{"signal": {"cancel": true}}')
        if c["shipyard_slots"] > 0:
            repair_rules = (
                f"SHIPYARD: your harbor opens with {c['shipyard_slots']} yard "
                "slot(s) — every build, refit, and repair holds one until it "
                "finishes; extra work waits its turn (build capacity is a "
                "strategic resource: repairing battle damage competes with "
                'growing the fleet). EXPAND with {"build_yard": true}: '
                f"{c['shipyard_cost']} treasury and "
                f"{c['shipyard_ticks'] / 10:g}s per extra slot, max "
                f"{c['shipyard_max']} total; expansion is harbor construction "
                "(it does not occupy a slot; one at a time). REPAIRS ARE NOT "
                "FREE: a damaged ship docked in your circle starts repair "
                "when a slot is free AND your treasury covers the bill — "
                "charged up front, cost "
                f"{c['repair_cost_pct']}% of her build cost and "
                f"{c['repair_ticks_pct']}% of build time, both scaled by the "
                "hull she is missing (min 1) — ALWAYS cheaper and faster than "
                "building her replacement, so bring damaged ships home rather "
                "than fighting them to the bottom; she is held in the yard "
                "until done. A rival ship scouting close to your flagship "
                "reads your yard activity — and yours theirs "
                "(rival.yard_busy)")
            build_note = ", one per free yard slot"
        else:
            repair_rules = f"docked repair 2 hull/{c['repair_period']} ticks"
            build_note = ""
        c["rules"] = (
            f"map {self.W}x{self.H}; decision window every {c['window'] / 10:g}s; match "
            f"ends t={self.max_ticks}; ships cost {c['ship_cost']} cargo, build in "
            f"{c['build_ticks'] / 10:g}s (queue max 3{build_note}); {sig_rules}; gather 1 "
            f"cargo/{c['gather_period']} ticks; fish shoals regen 1 cargo/"
            f"{c['fish_regen_period'] / 10:g}s; {repair_rules}; flagship hull "
            f"{c['flag_hull']}; your harbor "
            f"circle (radius {c['harbor_r']}) is your COMMAND RADIUS — any of your ships "
            "inside it deposits cargo, repairs, and picks up your latest standing orders"
            "; MONEY: you.treasury = SPENDABLE funds right now (deposits add to it, "
            "builds/refits/signals/scuttles change it); you.hauled = LIFETIME cargo "
            "delivered (score in timed matches, NEVER spendable)"
            + (f"; enemy CONTACTS persist on your plot {c['contact_ttl'] / 10:g}s after "
               "last sighting — state.enemies entries with age_s>0 are stale last-known "
               "positions (the ship may have moved or sunk unseen)"
               if c["contact_ttl"] > 0 else "")
            + ("" if c["parley"] else
               "; PARLEY IS DISABLED this match — no admiral-to-admiral messaging, "
               "any parley you send is discarded")
            + ("; returning ships file voyage reports — read state.reports each window"
               if c["voyage_reports"] else "")
            + ((f"; CATCH-UP WINDOWS (pipelined): the game does NOT pause while "
                f"you think. Answer within ~{c['window_wait_s']}s and you decide "
                "EVERY window; run over and the world sails on without you — "
                "when your reply finally lands you get ONE fresh snapshot "
                "(state.CATCH_UP says how many windows passed; it already "
                "includes all combat/contacts/messages you missed) and you are "
                f"live again. Fall {c['pipeline_depth']} whole windows behind "
                "and the fleet holds for you. Reply speed is an edge: fast "
                "admirals simply decide more often")
               if c["pipeline_depth"] > 0 else "")
            + (f"; REFIT (cost {c['refit_cost']} cargo/ship"
               + (f", {(c['build_ticks'] if c['refit_ticks'] < 0 else c['refit_ticks']) / 10:g}s in drydock"
                  if (c['refit_ticks'] != 0) else "")
               + '): "refit": {"A": "frigate"} converts that squadron\'s ships to the '
               "named class as they dock — a STANDING directive until cleared "
               "with null")
            + ('; REASSIGN: "reassign": {"<ship id>": "B"} transfers that ship to '
               "the named squadron when it next docks (it then flies that "
               "squadron's orders and program)")
            + ((f'; FLAGSHIP RELOCATION enabled: "relocate": [x,y] sails your '
                f"flagship — and your whole command circle with it — to a new "
                f"anchorage at speed {c['flag_speed']} (a trawler is 3); deposits, "
                "repairs and order pickup follow the flagship, and it stays "
                "vulnerable while under way")
               if c["flag_move"] else "")
            + (f"; SHIP DESIGN enabled: \"designs\": {{\"<name>\": {{six stats}}}} "
               f"creates a custom class — speed/hold/guns/armor/hull/lookout, each "
               ">=1, "
               + (f"total 6 to {c['design_points_max']} points and BUILD/REFIT cost "
                  f"scales with the total ({c['ship_cost']} cargo at "
                  f"{c['design_points']} points — small hulls are cheap, big hulls "
                  "are expensive gambles)"
                  if c["flex_design"] else
                  f"total EXACTLY {c['design_points']} points")
               + ", max 4 classes; then build or refit it by name"
               + (f"; operator classes available to all: "
                  + ", ".join(f"{n} {v}" for n, v in sorted(self.shared_designs.items()))
                  if self.shared_designs else "")
               if c["allow_designs"] else "")
            + (f"; CONN PROGRAMS enabled (the conn language, max "
               f"{c['program_chars']} chars/squadron — full API reference in your "
               "system prompt); a programmed ship ignores its role and runs your code"
               if c["programs"] else
               "; conn programs are DISABLED this match — standing-order roles only")
            + (("; ROLE AUTOPILOT ENABLED for unprogrammed ships — roles available: "
                + ",".join(sorted(self.roles_allowed)))
               if c["role_fallback"] else
               "; ROLE AUTOPILOT DISABLED: ships execute ONLY your conn programs — an "
               "unprogrammed ship sits IDLE in port (deposits/repairs still work); "
               "standing orders feed your code as orders.* parameters. To DESTROY an "
               "enemy flagship (the domination win) use helm.assault() in a program — "
               "aim it with the rival.flag_* sensors")
            + (f"; SCUTTLE: \"scuttle\": [ship ids] sinks your own ships for "
               f"{c['scuttle_value']} treasury each — ANY of your ships, ANYWHERE on "
               "the map, at any time (no need to dock). This is the escape hatch "
               "from a broke fleet: scuttle a hull to fund a signal hoist or a "
               "build. A laden ship's cargo sinks as a wreck where she goes down"
               if c["scuttle"] else "")
            + (f"; passive income: every fleet receives +{c['income_amount']} treasury "
               f"per {c['income_period'] / 10:g}s" if c["income_amount"] > 0 else "")
            + ".")
        self.scenario = dict(win=c["win"], description=c["description"],
                             rules=c["rules"], regions=c["territories"],
                             max_ticks=self.max_ticks,
                             signal_mode=c["signal_mode"],
                             signal_flags=sorted(self.signal_presets)
                             if c["signal_mode"] == "preset" else [],
                             programs=c["programs"],
                             conn_examples=c["conn_examples"])
        self.t = 0
        self.live = None                   # optional per-window flush callback
        self._pipe = {}                    # catch-up pipelining: fid -> the ONE in-flight call
        self._pipe_ot = None               # ordered-at tick for pipelined applies
        self._win_opened = None            # wall-clock stamp of the current window's dispatch
        self._last_snap = {}               # fid -> tick of its last snapshot (CATCH_UP gap)
        self.provenance = None             # run_config stamps the EXACT run setup
        self._istr = {}                    # OPT-M1: intent-string intern pool
        self._nrows = {}                   # OPT-M2: shared frame node rows
        self._rank_tick = -1               # per-tick squadron-roster cache
        self._ranks = {}
        self._yard = {}                    # per-tick shipyard occupancy {fid: works}
        self._lf = self._le = self._ld = 0
        self.next_ship_id = 1
        self.next_node_id = 1
        self.ships = {}                    # id -> Ship (insertion ordered)
        self.events = []
        self.frames = []
        self.decisions = []
        self._outage_streak = 0            # consecutive all-LLM-error windows
        self._u_fleets = set()             # fleets that have ever reported usage
                                           # (= the LLM-driven ones)
        self._outage_eval_t = -1           # last boundary evaluated (each once)
        self.pause_reason = None           # set when run() auto-pauses
        W2, H2 = self.W, self.H
        if not (2 <= len(players) <= 8):
            raise ValueError(f"2-8 fleets, got {len(players)}")
        # 4 corners first, then edge midpoints (5-8 players want a BIG map)
        harbors = [(10, 10), (W2 - 11, H2 - 11), (W2 - 11, 10), (10, H2 - 11),
                   (W2 // 2, 10), (W2 // 2, H2 - 11),
                   (10, H2 // 2), (W2 - 11, H2 // 2)][: len(players)]
        self.fleets = {}
        for i, (name, bot) in enumerate(players):
            f = Fleet(i, name, bot, *harbors[i])
            f.mode = c["win"]
            f.cargo = c["start_cargo"]
            f.yards = max(1, c["shipyard_slots"])
            f.flag_hull = c["flag_hull"]
            f.pending = {
                "A": dict(DEFAULT_ORDER, role="forage", rally=(f.hx, f.hy)),
                "B": dict(DEFAULT_ORDER, role="scout", rally=(self.W // 2, self.H // 2), aggression=0),
                "C": dict(DEFAULT_ORDER, role="guard", rally=(f.hx, f.hy), aggression=2),
            }
            self.fleets[i] = f
        if c["teams"].strip():             # team play: "0,1|2,3" -> team ids
            for ti, grp in enumerate(c["teams"].split("|")):
                for tok in grp.split(","):
                    tok = tok.strip()
                    if not tok.isdigit() or int(tok) not in self.fleets:
                        raise ValueError(f"teams: bad player index {tok!r} "
                                         f"(players are 0..{len(players) - 1})")
                    self.fleets[int(tok)].team = ti
            rosters = {}
            for f in self.fleets.values():
                rosters.setdefault(f.team, []).append(f.name)
            team_txt = ("; TEAM MATCH — " +
                        " vs ".join("+".join(ns) for ns in rosters.values()) +
                        ": teammates never damage each other, share lookout vision, "
                        "and their scores SUM — you win or lose together")
            c["rules"] += team_txt
            self.scenario["rules"] = c["rules"]
        # OPT-A: teams are immutable after this point -> precompute enmity
        self._hostile_to = {a: frozenset(b for b in self.fleets
                                         if self.fleets[b].team != self.fleets[a].team)
                            for a in self.fleets}
        self.nodes = {}
        self._island_pool = list(ISLANDS)
        self.rng.shuffle(self._island_pool)
        self._gen_nodes()
        self.blocked = set()               # impassable land cells
        self.island_specs = []             # (cx, cy, r) — charted geography
        if c["island_coverage"] > 0:
            self._gen_islands()
            c["rules"] += ("; ISLANDS: impassable land your ships must sail around "
                           "(they steer themselves past it) — charted landmasses "
                           "(center±radius): " +
                           ", ".join(f"({cx},{cy})±{r}"
                                     for cx, cy, r in self.island_specs))
            self.scenario["rules"] = c["rules"]
        self.regions = []
        self.region_owner = {}
        self._contest = {}                 # rid -> [fleet, progress ticks]
        self._cellregion = None
        if self.scenario["win"] == "territory":
            self._gen_regions()
        for f in self.fleets.values():
            for n in self.nodes.values():
                f.node_mem[n.id] = (n.remaining, 0)
            f._bel_v = 1
            self._spawn(f, "trawler", "A")
            self._spawn(f, "trawler", "A")
            self._spawn(f, "scout", "B")

    # ---------- world gen ----------
    def _gen_nodes(self):
        # 4-fold mirror symmetry: place in one quadrant, reflect across both axes.
        placed = []
        tries = 0
        while len(placed) < self.cfg['fish_sites'] and tries < 500:
            tries += 1
            x = self.rng.randrange(6, self.W // 2 - 4)
            y = self.rng.randrange(6, self.H // 2 - 4)
            if cheb(x, y, 10, 10) < 7:            # keep off the harbor
                continue
            if any(cheb(x, y, px, py) < 8 for px, py in placed):
                continue
            placed.append((x, y))
        for x, y in placed:
            for mx, my in {(x, y), (self.W - 1 - x, y), (x, self.H - 1 - y), (self.W - 1 - x, self.H - 1 - y)}:
                self._add_node(mx, my, "fish", self.cfg["fish_cap"], self.cfg["fish_cap"])
        # two contested wrecks near the center line (180-degree symmetric)
        dx, dy = self.rng.randrange(3, 10), self.rng.randrange(2, 6)
        self._add_node(self.W // 2 - dx, self.H // 2 - dy, "wreck", self.cfg["wreck_cap"], self.cfg["wreck_cap"])
        self._add_node(self.W // 2 + dx, self.H // 2 + dy, "wreck", self.cfg["wreck_cap"], self.cfg["wreck_cap"])

    def _gen_islands(self):
        """Impassable landmasses, density-driven: island_coverage % of the map
        goes under convex diamond blobs (Manhattan radius, mirrored 180°).
        Convexity matters — the greedy wall-slide in _move can round a convex
        obstacle but would oscillate in a concave pocket. Low coverage = a few
        small dots; high = a dense mixed-size archipelago. Placement keeps a
        sea lane of >=3 Manhattan cells between any two landmasses (ships
        always pass), and never covers harbors, command circles, or nodes."""
        cov = self.cfg["island_coverage"]
        target = self.W * self.H * cov // 100
        # size palette widens as the sea fills in: dots first, landmasses later
        sizes = (1, 1, 2, 2, 3) if cov < 8 else (1, 2, 2, 3, 3, 4)
        harbors = [(f.hx, f.hy) for f in self.fleets.values()]
        placed_cells = 0
        tries = 0
        while placed_cells < target and tries < 6000:
            tries += 1
            r = self.rng.choice(sizes)
            x = self.rng.randrange(r + 2, self.W - r - 2)
            y = self.rng.randrange(r + 2, self.H - r - 2)
            cands = {(x, y), (self.W - 1 - x, self.H - 1 - y)}
            ok = True
            for cx, cy in cands:
                if any(cheb(cx, cy, hx, hy) <= self.hr + r + 5 for hx, hy in harbors) \
                        or any(abs(cx - n.x) + abs(cy - n.y) <= r + 2
                               for n in self.nodes.values()) \
                        or any(abs(cx - px) + abs(cy - py) < r + pr + 3
                               for px, py, pr in self.island_specs) \
                        or any(abs(cx - px) + abs(cy - py) < 2 * r + 3
                               for px, py in cands if (px, py) != (cx, cy)):
                    ok = False
            if not ok:
                continue
            for cx, cy in cands:
                self.island_specs.append((cx, cy, r))
                for ix in range(cx - r, cx + r + 1):
                    for iy in range(cy - r, cy + r + 1):
                        if abs(ix - cx) + abs(iy - cy) <= r \
                                and 0 <= ix < self.W and 0 <= iy < self.H:
                            self.blocked.add((ix, iy))
                            placed_cells += 1

    def _name_region(self, x, y, pts):
        near = min(self.nodes.values(), key=lambda n: cheb(x, y, n.x, n.y) * 100 + n.id)
        for suffix in ("Waters", "Deeps", "Reach", "Expanse"):
            label = f"{near.name} {suffix}"
            if not any(p[2] == label for p in pts):
                return label
        return f"{near.name} Sea {len(pts)}"

    def _gen_regions(self):
        n = int(self.cfg['territories'])
        if int(self.cfg.get('territory_size', 0)) > 0:
            # average size in CELLS drives the count; the explicit count knob
            # only applies when territory_size is 0
            n = max(4, min(48, round(self.W * self.H
                                     / int(self.cfg['territory_size']))))
        sym = bool(self.cfg.get('territory_symmetry', True))
        pts = []
        tries = 0
        while len(pts) < n and tries < 500:
            tries += 1
            x = self.rng.randrange(4, self.W - 4)
            y = self.rng.randrange(4, self.H - 4)
            if any(cheb(x, y, px, py) < 10 for px, py, _ in pts):
                continue
            if sym:
                # 4-WAY symmetry (both axes, the fish-shoal scheme): every
                # seed lands with its three mirrors, so every admiral's CORNER
                # of the map carves into the same territory layout. (180°
                # pairs still favored the two fleets whose harbors sat on the
                # pair axis in 3-4 player games.)
                quad = [(x, y), (self.W - 1 - x, y),
                        (x, self.H - 1 - y), (self.W - 1 - x, self.H - 1 - y)]
                if len(pts) >= n - 3:
                    # fewer than 4 slots left: prefer the self-symmetric
                    # center for ONE, then let the relaxed fill below finish —
                    # count beats perfection (the parity lesson from the
                    # 180° version: 9 of 40 seeds shipped n-1 regions)
                    x2, y2 = self.W // 2, self.H // 2
                    if not any(cheb(x2, y2, px, py) < 6 for px, py, _ in pts):
                        pts.append((x2, y2, self._name_region(x2, y2, pts)))
                    break
                # the set must be self-spaced AND clear of everything placed
                ok = True
                for i, (ax, ay) in enumerate(quad):
                    if any(cheb(ax, ay, bx, by) < 10
                           for bx, by in quad[i + 1:]):
                        ok = False
                        break
                    if any(cheb(ax, ay, px, py) < 10 for px, py, _ in pts):
                        ok = False
                        break
                if not ok:
                    continue
                for ax, ay in quad:
                    pts.append((ax, ay, self._name_region(ax, ay, pts)))
                continue
            pts.append((x, y, self._name_region(x, y, pts)))
        while len(pts) < n and tries < 800:
            # dense map ran the paired placement dry: fill the shortfall with
            # relaxed spacing so the configured count is always honored
            tries += 1
            x = self.rng.randrange(4, self.W - 4)
            y = self.rng.randrange(4, self.H - 4)
            if any(cheb(x, y, px, py) < 6 for px, py, _ in pts):
                continue
            pts.append((x, y, self._name_region(x, y, pts)))
        self.regions = [dict(id=i, x=p[0], y=p[1], name=p[2]) for i, p in enumerate(pts)]
        self.region_owner = {r["id"]: None for r in self.regions}
        # cell -> territory: nearest seat by Chebyshev, ties by straight-line
        # distance, then lower id. The euclidean tiebreak keeps mirrored maps
        # symmetric along the middle — pure lower-id gave every equidistant
        # centerline cell to the earlier-placed seat, visibly denting the
        # 4-way symmetry down the map's spine. (cellmap=2 in replay meta; the
        # viewer mirrors this rule exactly.)
        self._cellregion = [[min(self.regions, key=lambda r:
                                 (cheb(cx, cy, r["x"], r["y"]),
                                  (cx - r["x"]) ** 2 + (cy - r["y"]) ** 2,
                                  r["id"]))["id"]
                             for cy in range(self.H)] for cx in range(self.W)]

    def _presence(self):
        present = {r["id"]: {} for r in self.regions}
        for s in self.ships.values():
            rid = self._cellregion[s.x][s.y]
            present[rid][s.fleet] = present[rid].get(s.fleet, 0) + 1
        return present

    def _capture_tick(self):
        """Timed captures (2026-08-09 rules): a capture PROGRESSES only in
        SOLE possession — no ship of any hostile fleet (the owner included)
        may stand in the territory. A hostile arriving PAUSES the clock where
        it is; the claim only LAPSES (progress zeroed, territory kept by its
        owner) when every capturing ship leaves. Ship counts never matter —
        you clear the sea, then you hold it.

        Runs EVERY tick, +1 per tick: capture_ticks means exactly what it
        says, and the viewer's pie fills smoothly. (When this only ran on the
        scoring cadence, a 50-tick capture on the 50-tick default cadence
        jumped 0→done in one step — the pie was born full.)"""
        cap = self.cfg["territory_capture_ticks"]
        present = self._presence()
        for r in self.regions:
            rid = r["id"]
            owner = self.region_owner[rid]
            occ = present[rid]
            took = None
            hostile_here = lambda f: any(
                c > 0 and g != f and self._hostile(g, f)
                for g, c in occ.items())
            c2 = self._contest.get(rid)
            if c2 and occ.get(c2[0], 0) <= 0:
                self._contest.pop(rid, None)        # claimant left: lapses
                c2 = None
            if c2:
                if not hostile_here(c2[0]):
                    c2[1] += 1
                    if c2[1] >= cap:
                        took = c2[0]
                # else: an enemy is in the territory — progress holds, paused
            elif not (owner is not None and occ.get(owner, 0) > 0):
                # no capture underway and the holder is absent: a hostile
                # fleet alone in the territory starts the clock
                cands = sorted(
                    f for f, c in occ.items()
                    if c > 0 and f != owner
                    and (owner is None or self._hostile(f, owner))
                    and not hostile_here(f))
                if cands:
                    self._contest[rid] = [cands[0], 1]
            if took is not None:
                self._contest.pop(rid, None)
                self.region_owner[rid] = took
                self._ev("region", region=rid, name=r["name"], fleet=took,
                         prev=owner)

    def _territory_tick(self):
        """The scoring cadence (every territory_tick ticks): +1 point per held
        territory — plus the legacy INSTANT-flip rules when capture_ticks=0
        (timed captures live in _capture_tick, which runs every tick)."""
        cap = self.cfg.get("territory_capture_ticks", 0)
        present = self._presence() if cap <= 0 else None
        for r in self.regions:
            rid = r["id"]
            owner = self.region_owner[rid]
            if cap <= 0:
                occ = present[rid]
                took = None
                # legacy instant flips: holder on station keeps it, else the
                # clear strongest hostile presence takes it (pre-timer rules)
                if owner is not None and occ.get(owner, 0) > 0:
                    pass
                else:
                    rivals = {f: c for f, c in occ.items()
                              if c > 0 and f != owner
                              and (owner is None or self._hostile(f, owner))}
                    if rivals:
                        top = max(rivals.values())
                        best = sorted(f for f, c in rivals.items() if c == top)
                        if len(best) == 1:
                            took = best[0]
                if took is not None:
                    self.region_owner[rid] = took
                    self._ev("region", region=rid, name=r["name"], fleet=took,
                             prev=owner)
            owner = self.region_owner[rid]
            if owner is not None and self.fleets[owner].alive:
                self.fleets[owner].territory += 1

    def _add_node(self, x, y, kind, amount, cap, name=None):
        # island names are GEOGRAPHY — only world-gen features draw from the
        # pool; play-created wrecks are named for what sank there (pass name)
        if name is None:
            name = self._island_pool.pop() if self._island_pool \
                else f"Islet {self.next_node_id}"
        n = Node(self.next_node_id, x, y, kind, amount, cap, name)
        self.next_node_id += 1
        self.nodes[n.id] = n
        return n

    def _spawn(self, fleet, preset, squad):
        s = Ship(self.next_ship_id, fleet.id, squad, fleet.hx, fleet.hy, preset,
                 stats=self.class_stats(fleet, preset))
        s.moved_t = s.gained_t = self.t
        self.next_ship_id += 1
        self.ships[s.id] = s
        self._ev("spawn", fleet=fleet.id, ship=s.id, preset=preset, squad=squad)
        return s

    # ---------- events / frames ----------
    def _ev(self, kind, **kw):
        kw["t"] = self.t
        kw["k"] = kind
        self.events.append(kw)

    def _nrow(self, n):
        """OPT-M2: frames are write-once, so identical [id, remaining] pairs can be
        the SAME list object across frames — same JSON, a fraction of the heap."""
        k = (n.id, n.remaining)
        r = self._nrows.get(k)
        if r is None:
            r = self._nrows[k] = [n.id, n.remaining]
        return r

    def _frame(self):
        self.frames.append({
            "t": self.t,
            "s": [[s.id, s.fleet, s.x, s.y, (s.hull * 100) // s.hull_max, s.cargo]
                  for s in self.ships.values()],
            "n": [self._nrow(n) for n in self.nodes.values()],
            "f": [[f.id, f.flag_hull, f.cargo, f.bank, f.score(), 1 if f.alive else 0,
                   f.hx, f.hy]
                  for f in self.fleets.values()],
            **({"r": [[r["id"], self.region_owner[r["id"]]
                       if self.region_owner[r["id"]] is not None else -1]
                      + (lambda c2: [c2[0], min(99, (c2[1] * 100)
                                                // max(1, self.cfg.get(
                                                    "territory_capture_ticks", 1)))]
                         if c2 else [])(self._contest.get(r["id"]))
                      for r in self.regions]} if self.regions else {}),
        })

    # ---------- summaries (the SAME API future LLM admirals consume) ----------
    def summary_for(self, fleet):
        vis_cells = set()
        own = []
        for s in self.ships.values():
            if s.fleet != fleet.id:
                continue
            own.append(dict(id=s.id, squad=s.squad, preset=s.preset, x=s.x, y=s.y,
                            hull_pct=(s.hull * 100) // s.hull_max, cargo=s.cargo,
                            role=s.orders["role"]))
        enemies = []
        if self.cfg["contact_ttl"] > 0:
            # the accumulated plot: live sightings (age_s 0) + stale contacts at
            # their last-known position — everything the FLEET saw since last window
            for rec in fleet.contacts.values():
                age = self.t - rec["t"]
                enemies.append(dict(fleet=rec["fleet"],
                                    admiral=self.fleets[rec["fleet"]].name,
                                    preset=rec["preset"], x=rec["x"], y=rec["y"],
                                    cargo_laden=rec["laden"],
                                    age_s=round(age / 10, 1)))
            enemies.sort(key=lambda e: e["age_s"])
            del enemies[60:]
        else:
            for s in self.ships.values():
                if not self._hostile(s.fleet, fleet.id):
                    continue
                if self._fleet_sees(fleet, s.x, s.y):
                    enemies.append(dict(fleet=s.fleet,
                                        admiral=self.fleets[s.fleet].name,
                                        preset=s.preset, x=s.x, y=s.y,
                                        cargo_laden=s.cargo > 0, age_s=0.0))
        for n in self.nodes.values():
            if self._fleet_sees(fleet, n.x, n.y):
                fleet.node_mem[n.id] = (n.remaining, self.t)
                fleet._bel_v = getattr(fleet, "_bel_v", 0) + 1
        nodes = [dict(id=n.id, name=n.name, x=n.x, y=n.y, kind=n.kind,
                      believed=self.believed(fleet, n)) for n in self.nodes.values()]
        messages, fleet.inbox = fleet.inbox, []
        reports, fleet.reports_pending = fleet.reports_pending[:12], []
        warnings, fleet.warnings = list(fleet.warnings), []
        if fleet.queued_signal is not None:
            warnings.append(
                f"⚠ signal flag QUEUED, not yet hoisted — waiting on funds "
                f"(cost {self.cfg['signal_cost']}, treasury {fleet.cargo}"
                + (", or scuttle any ship at sea to raise funds NOW"
                   if self.cfg["scuttle"] else "") + "); it "
                'raises automatically when affordable, or cancel with '
                '{"signal": {"cancel": true}}')
        region_view = [dict(id=r["id"], name=r["name"], x=r["x"], y=r["y"],
                            owner=(self.fleets[self.region_owner[r["id"]]].name
                                   if self.region_owner[r["id"]] is not None else None),
                            **({"contested_by":
                                self.fleets[self._contest[r["id"]][0]].name,
                                "capture_pct": min(99, self._contest[r["id"]][1]
                                * 100 // max(1, self.cfg.get(
                                    "territory_capture_ticks", 1)))}
                               if r["id"] in self._contest else {}))
                       for r in self.regions] if self.regions else None
        out = dict(
            # full scenario incl. rules — SYSTEM directs admirals to scenario.rules
            # for exact numbers (a prior refactor silently dropped it; fixed 2026-08-06)
            scenario=dict(self.scenario),
            t=self.t, window=self.t // self.cfg['window'], messages=messages, you=dict(
                fleet=fleet.id, treasury=fleet.cargo, hauled=fleet.bank,
                # kills = the COUNT of ships you sank; kill_score = the points
                # they earned (a model once read '150' as 150 sinkings)
                kills=fleet.kill_count, kill_score=fleet.kills,
                flag_hull=fleet.flag_hull, harbor=(fleet.hx, fleet.hy),
                ships=own, builds=len(fleet.build_q) + len(fleet.builds),
                signal_cd=fleet.signal_cd,
                **({"yard": dict(slots=fleet.yards,
                                 max_slots=self.cfg["shipyard_max"],
                                 expanding=bool(fleet.yard_done_t),
                                 busy=self._yard.get(fleet.id, 0),
                                 building=len(fleet.builds),
                                 queued=len(fleet.build_q),
                                 repairing=sum(1 for s2 in self.ships.values()
                                               if s2.fleet == fleet.id
                                               and s2.repair_at))}
                   if self.cfg["shipyard_slots"] > 0 else {}),
                recent_hits=fleet.recent_hits,
                combat=[dict(ship=k[0], vs=self.fleets[k[1]].name,
                             dealt=v["dealt"], taken=v["taken"],
                             at=[v["x"], v["y"]])
                        for k, v in list(fleet.combat.items())[:30]],
                orders={k: dict(v) for k, v in fleet.pending.items()},
                **({"relocating_to": list(fleet.flag_target)}
                   if fleet.flag_target else {}),
                **({"team_mates": [g.id for g in self.fleets.values()
                                   if g.team == fleet.team and g.id != fleet.id]}
                   if any(g.team == fleet.team and g.id != fleet.id
                          for g in self.fleets.values()) else {}),
                programs={sq: p.text
                          for sq, p in fleet.pending_programs.items()},
                designs=dict(fleet.designs),
                refits=dict(fleet.pending_refits),
                warnings=warnings[:10]),
            enemies=enemies, nodes=nodes, reports=reports,
            parley_log=fleet.parley_log[-200:],
            admirals={f.id: f.name for f in self.fleets.values()},
            scores={f.id: f.score() for f in self.fleets.values()},
            harbors={f.id: (f.hx, f.hy) for f in self.fleets.values() if f.alive},
            alive=[f.id for f in self.fleets.values() if f.alive],
        )
        if region_view is not None:
            out["regions"] = region_view
        return out

    def _beliefs(self, fleet):
        """OPT-I: believed() depends only on (fleet.node_mem, node, self.t) — never on
        the asking ship. Memoize per fleet; every node_mem write bumps fleet._bel_v."""
        key = (self.t, getattr(fleet, "_bel_v", 0))
        if getattr(fleet, "_bel_k", None) != key:
            fleet._bel_k = key
            fleet._bel = {n.id: self.believed(fleet, n) for n in self.nodes.values()}
        return fleet._bel

    def believed(self, fleet, node):
        """Fleet's belief about a node's stock. Charted knowledge: fish shoals are known
        to regenerate, so belief grows with time-since-last-look (capped). Without this,
        a fully-harvested map sent every fleet idle-blind in port forever (P0.1 bug)."""
        val, seen = fleet.node_mem.get(node.id, (0, 0))
        if node.kind == "fish":
            val = min(node.cap, val + (self.t - seen) // self.cfg["fish_regen_period"])
        return val

    def _fleet_sees(self, fleet, x, y):
        team = fleet.team
        hr4 = self.hr + 4
        fleets = self.fleets
        for g in fleets.values():          # own + allied command circles
            if g.alive and g.team == team:
                dx = x - g.hx
                if dx < 0:
                    dx = -dx
                if dx > hr4:
                    continue
                dy = y - g.hy
                if dy < 0:
                    dy = -dy
                if dy <= hr4:
                    return True
        for s in self.ships.values():      # own + allied lookouts (shared vision)
            if fleets[s.fleet].team == team:
                v = 4 + s.stats["lookout"] * 2
                dx = x - s.x
                if dx < 0:
                    dx = -dx
                if dx > v:
                    continue
                dy = y - s.y
                if dy < 0:
                    dy = -dy
                if dy <= v:
                    return True
        return False

    def _update_contacts(self):
        """Accumulate the admiral's plot every tick: ships see enemies continuously
        between windows, and the admiral must know what its fleet knew — not just
        whatever happened to be in sight at the 1-in-window snapshot."""
        if self.cfg["contact_ttl"] <= 0:
            return
        t = self.t
        ttl = self.cfg["contact_ttl"]
        hr4 = self.hr + 4
        fleets = self.fleets
        # OPT-D: the watcher plot is identical for every fleet on a team and does not
        # change during this pass -> build it once instead of per (fleet, enemy ship)
        watch = {}
        for g in fleets.values():
            if g.alive:
                watch.setdefault(g.team, ([], []))[0].append((g.hx, g.hy))
        for s in self.ships.values():
            watch.setdefault(fleets[s.fleet].team, ([], []))[1].append(
                (s.x, s.y, 4 + s.stats["lookout"] * 2))
        for f in fleets.values():
            if not f.alive:
                continue
            host = self._hostile_to[f.id]
            harbors, lookouts = watch.get(f.team, ((), ()))
            cont = f.contacts
            for s in self.ships.values():
                if s.fleet not in host:
                    continue               # allies are not contacts
                x, y = s.x, s.y
                seen = False
                for hx, hy in harbors:
                    dx = x - hx
                    if dx < 0:
                        dx = -dx
                    if dx > hr4:
                        continue
                    dy = y - hy
                    if dy < 0:
                        dy = -dy
                    if dy <= hr4:
                        seen = True
                        break
                if not seen:
                    for wx, wy, v in lookouts:
                        dx = x - wx
                        if dx < 0:
                            dx = -dx
                        if dx > v:
                            continue
                        dy = y - wy
                        if dy < 0:
                            dy = -dy
                        if dy <= v:
                            seen = True
                            break
                if seen:
                    cont[s.id] = dict(fleet=s.fleet, preset=s.preset,
                                      x=x, y=y, laden=s.cargo > 0, t=t)
            for sid in [k for k, v in cont.items() if t - v["t"] > ttl]:
                del cont[sid]

    # ---------- ship classes ----------
    STAT_KEYS = ("speed", "hold", "guns", "armor", "hull", "lookout")

    def _clean_design(self, name, st):
        """Validate a custom class: proper name, all six stats >=1 ints, budget."""
        if not isinstance(name, str) or not (1 <= len(name) <= 14) \
                or not name.replace("-", "").replace("_", "").isalnum() \
                or name in PRESETS or not isinstance(st, dict):
            return None
        try:
            clean = {k: int(st[k]) for k in self.STAT_KEYS}
        except (KeyError, TypeError, ValueError):
            return None
        if any(v < 1 for v in clean.values()):
            return None
        total = sum(clean.values())
        if self.cfg["flex_design"]:
            if not (6 <= total <= self.cfg["design_points_max"]):
                return None
        elif total != self.cfg["design_points"]:
            return None
        return clean

    def class_cost(self, fleet, name):
        """Build cost for a class. Fixed under standard rules; scales linearly
        with the class's point total under flex_design."""
        base = self.cfg["ship_cost"]
        if not self.cfg["flex_design"]:
            return base
        st = self.class_stats(fleet, name)
        if st is None:
            return base
        return max(1, round(base * sum(st.values()) / self.cfg["design_points"]))

    def refit_class_cost(self, fleet, name):
        base = self.cfg["refit_cost"]
        if not self.cfg["flex_design"]:
            return base
        st = self.class_stats(fleet, name)
        if st is None:
            return base
        return max(1, round(base * sum(st.values()) / self.cfg["design_points"]))

    def class_stats(self, fleet, name):
        """Resolve a class name for a fleet: built-in, operator, or own design."""
        return PRESETS.get(name) or self.shared_designs.get(name) \
            or fleet.designs.get(name)

    # ---------- admiral actions ----------
    def _clean_order(self, fleet, od):
        """Sanitize one order dict from an admiral (or a signal preset) into a
        bounded, engine-safe standing order. None = rejected."""
        if not isinstance(od, dict):
            return None
        if od.get("role") not in self.roles_allowed:
            # every other rejection path warns — a silently-dropped order left
            # admirals convinced their command was in force
            fleet.warnings.append(
                f"⚠ order REJECTED: role {str(od.get('role'))[:20]!r} is not "
                f"available this match (allowed: {sorted(self.roles_allowed)})")
            return None
        clean = dict(DEFAULT_ORDER)
        clean["role"] = od["role"]
        r = od.get("rally")
        if isinstance(r, (list, tuple)) and len(r) == 2:
            try:
                clean["rally"] = (max(0, min(self.W - 1, int(r[0]))),
                                  max(0, min(self.H - 1, int(r[1]))))
            except (TypeError, ValueError):
                pass
        try:
            clean["aggression"] = max(0, min(3, int(od.get("aggression",
                                                           clean["aggression"]))))
            clean["retreat_hull_pct"] = max(0, min(90, int(od.get("retreat_hull_pct",
                                                          clean["retreat_hull_pct"]))))
        except (TypeError, ValueError):
            pass
        tf = od.get("target_fleet")
        if isinstance(tf, str):
            tf = next((f.id for f in self.fleets.values() if f.name == tf), None)
        clean["target_fleet"] = tf if isinstance(tf, int) and tf in self.fleets \
            and tf != fleet.id else None
        return clean

    @staticmethod
    def _as_list(v):
        return v if isinstance(v, list) else []

    @staticmethod
    def _as_dict(v):
        return v if isinstance(v, dict) else {}

    ACTION_FIELDS = ("orders", "programs", "designs", "refit", "reassign",
                     "relocate", "build", "build_yard", "parley", "scuttle",
                     "signal")
    META_FIELDS = frozenset(("thoughts", "scratchpad", "_usage"))

    def _apply_actions(self, fleet, actions):
        # LLM output is hostile input: every field gets type-normalized (a model
        # once sent "build" as an object — dict[:4] is a KeyError, and this ran
        # outside the never-crash guard: it killed a whole game. 2026-08-07)
        if not isinstance(actions, dict):
            return
        self._warn_mark = len(fleet.warnings)
        try:
            for k in actions:
                if k not in self.ACTION_FIELDS and k not in self.META_FIELDS:
                    fleet.warnings.append(
                        f"unknown field '{str(k)[:24]}' IGNORED — check the "
                        "api reference for the accepted reply fields")
            # every field applies in ISOLATION, in a fixed order: one
            # malformed field can no longer silently drop the fields after it
            # (that ate Kimi's build_yard, 2026-08-09 — the reply's earlier
            # fields threw and the whole tail was discarded)
            for k in self.ACTION_FIELDS:
                if k not in actions:
                    continue
                try:
                    self._apply_actions_body(fleet, {k: actions[k]})
                except Exception as e:
                    fleet.warnings.append(
                        f"⚠ your '{k}' field was REJECTED "
                        f"({type(e).__name__}: {str(e)[:200]}) — every other "
                        "field still applied")
        finally:
            # the decision (thoughts + token accounting) is recorded even when
            # an action field throws — losing the admiral's reasoning from
            # the replay because one field was malformed was worse than the bug
            self._record_decision(fleet, actions)

    def _apply_actions_body(self, fleet, actions):
        for squad, od in self._as_dict(actions.get("orders")).items():
            clean = self._clean_order(fleet, od)
            if clean is None or not isinstance(squad, str) or not squad:
                continue
            fleet.pending[squad[:1].upper()] = clean
        if self.cfg["programs"]:
            for squad, text in self._as_dict(actions.get("programs")).items():
                if not isinstance(squad, str) or not squad or not isinstance(text, str):
                    continue
                sq = squad[:1].upper()
                if not text.strip():                 # empty = remove the program
                    if fleet.pending_programs.pop(sq, None) is not None:
                        self._ev("program", fleet=fleet.id, squad=sq, cleared=True)
                    continue
                if len(text) > self.cfg["program_chars"]:
                    reason = (f"program is {len(text)} chars — the limit is "
                              f"{self.cfg['program_chars']}")
                    self._ev("program_rejected", fleet=fleet.id, squad=sq,
                             reason=reason)
                    fleet.warnings.append(
                        f"⚠ squadron {sq} program REJECTED: {reason}. The "
                        "squadron keeps its previous program (or none).")
                    continue
                try:
                    prog = conn.compile_program(text)
                except Exception as e:      # ConnError is the normal case, but a
                    # degenerate program can raise RecursionError etc. — any
                    # compile failure must reject THIS program, not the window
                    self._ev("program_rejected", fleet=fleet.id, squad=sq,
                             reason=str(e)[:200])
                    fleet.warnings.append(
                        f"⚠ squadron {sq} program COMPILE ERROR: {str(e)[:200]}. "
                        "Fix and resubmit — the squadron keeps its previous "
                        "program (or none).")
                    continue
                fleet.pending_programs[sq] = prog
                self._ev("program", fleet=fleet.id, squad=sq, text=text)
        if self.cfg["allow_designs"]:
            for name, st in self._as_dict(actions.get("designs")).items():
                clean = self._clean_design(name, st)
                if clean is None or (name not in fleet.designs
                                     and len(fleet.designs) >= 4) \
                        or name in self.shared_designs:
                    reason = ("stats must be 6 ints >=1 "
                              + (f"totalling 6-{self.cfg['design_points_max']}"
                                 if self.cfg["flex_design"] else
                                 f"summing to exactly {self.cfg['design_points']}")
                              + "; max 4 classes; name unused+alphanumeric")
                    self._ev("design_rejected", fleet=fleet.id, name=str(name)[:20],
                             reason=reason)
                    fleet.warnings.append(
                        f"⚠ ship design {str(name)[:20]!r} REJECTED: {reason}")
                    continue
                # PAY FOR WHAT YOU DEFINED: cost is charged when a build is
                # queued or a refit starts, but stats resolve at COMPLETION —
                # so redefining a class with work already paid for handed out a
                # 24-point hull for a 6-point price under flex_design (and a
                # free re-spec at fixed points). Refuse while that class has
                # in-flight work; redefining an idle class is still free.
                if name in fleet.designs:
                    inflight = None
                    if any(p == name for p, _sq in fleet.build_q):
                        inflight = "a queued build"
                    elif any(b[0] == name for b in fleet.builds):
                        inflight = "a build in the yard"
                    elif any(s.fleet == fleet.id and s.refit_to == name
                             for s in self.ships.values()):
                        inflight = "a refit under way"
                    if inflight:
                        self._ev("design_rejected", fleet=fleet.id, name=name,
                                 reason=f"{inflight} already paid for this class")
                        fleet.warnings.append(
                            f"⚠ ship design {name!r} REJECTED: {inflight} is "
                            "already paid for at the current stats — redefine "
                            "it once that work completes")
                        continue
                fleet.designs[name] = clean
                self._ev("design", fleet=fleet.id, name=name, stats=clean)
        for squad, cls in self._as_dict(actions.get("refit")).items():
            if not isinstance(squad, str) or not squad:
                continue
            sq = squad[:1].upper()
            # refit is keyed by SQUADRON, never by ship id. {"refit": {"12": ...}}
            # used to land in pending_refits["1"], match no squadron, and expire
            # in silence — docs/ACTIONS.md advertised ship ids, so a model could
            # follow the documentation and lose the order with no feedback.
            if not sq.isalpha():
                fleet.warnings.append(
                    f"⚠ refit IGNORED: {str(squad)[:20]!r} is not a squadron — "
                    "refit is keyed by squadron letter (A-F), not by ship id")
                continue
            if cls is None or cls == "":
                fleet.pending_refits.pop(sq, None)
            elif isinstance(cls, str) and self.class_stats(fleet, cls) is not None:
                fleet.pending_refits[sq] = cls
            else:
                fleet.warnings.append(
                    f"⚠ refit of squadron {sq} IGNORED: {str(cls)[:20]!r} is not "
                    "a class you can build")
        for sid, sq in self._as_dict(actions.get("reassign")).items():
            try:
                sid = int(sid)
            except (TypeError, ValueError):
                fleet.warnings.append(f"⚠ reassign IGNORED: {str(sid)[:20]!r} is "
                                      "not a ship id")
                continue
            s = self.ships.get(sid)
            if s is None or s.fleet != fleet.id or not isinstance(sq, str) \
                    or not sq[:1].isalpha():
                fleet.warnings.append(f"⚠ reassign of ship {sid} IGNORED — not "
                                      "your ship, or bad squadron letter")
                continue
            fleet.pending_reassign[sid] = sq[:1].upper()
        if self.cfg["flag_move"]:
            rel = self._as_list(actions.get("relocate"))
            if rel:
                tx = None
                if len(rel) == 2:
                    try:
                        tx, ty = int(rel[0]), int(rel[1])
                    except (TypeError, ValueError):
                        tx = None
                if tx is not None and 0 <= tx < self.W and 0 <= ty < self.H:
                    fleet.flag_target = (tx, ty)
                    self._ev("relocate_order", fleet=fleet.id, to=[tx, ty])
                else:
                    fleet.warnings.append(
                        f"⚠ relocate IGNORED: need [x, y] on the "
                        f"{self.W}x{self.H} map, got {str(rel)[:40]}")
        for b in self._as_list(actions.get("build"))[:4]:
            if not isinstance(b, dict):     # {"build": ["trawler"]} is ordinary
                continue                    # LLM output — never let it throw
            preset, squad = b.get("preset"), b.get("squad", "A")
            squad = squad[:1].upper() if isinstance(squad, str) and squad else "A"
            if not isinstance(preset, str) \
                    or self.class_stats(fleet, preset) is None:
                continue
            cost = self.class_cost(fleet, preset)
            if fleet.cargo < cost:
                fleet.warnings.append(
                    f"build {preset} DROPPED — insufficient funds "
                    f"(cost {cost}, treasury {fleet.cargo})")
            elif len(fleet.build_q) >= 3:
                fleet.warnings.append(f"build {preset} DROPPED — build queue full (3)")
            else:
                fleet.cargo -= cost
                fleet.build_q.append((preset, squad))
        if actions.get("build_yard") and self.cfg["shipyard_slots"] > 0:
            yc, yt = self.cfg["shipyard_cost"], self.cfg["shipyard_ticks"]
            if fleet.yard_done_t:
                fleet.warnings.append("build_yard IGNORED — an expansion is "
                                      "already under construction")
            elif fleet.yards >= self.cfg["shipyard_max"]:
                fleet.warnings.append(
                    f"build_yard IGNORED — your harbor is at its maximum of "
                    f"{self.cfg['shipyard_max']} yard slots")
            elif fleet.cargo < yc:
                fleet.warnings.append(
                    f"build_yard DROPPED — insufficient funds (cost {yc}, "
                    f"treasury {fleet.cargo})")
            else:
                fleet.cargo -= yc
                fleet.yard_done_t = self.t + yt
                self._ev("yard_start", fleet=fleet.id,
                         ready=fleet.yard_done_t)
        sent = 0
        for pm in self._as_list(actions.get("parley")) if self.cfg["parley"] else []:
            if sent >= 2 or not isinstance(pm, dict):
                continue
            text = one_line(pm.get("text", ""), 280)
            if not text:
                continue
            to = pm.get("to")
            targets = [f for f in self.fleets.values()
                       if f.alive and f.id != fleet.id
                       and (to == "all" or to == f.id or to == f.name)]
            if not targets:
                continue
            w = self.t // self.cfg['window']
            for tf in targets:
                tf.inbox.append(dict(sender=fleet.id, text=text))
                tf.parley_log.append(dict(w=w, frm=fleet.name, text=text))
            fleet.parley_log.append(dict(
                w=w, to="all" if to == "all" else targets[0].name, text=text))
            self._ev("parley", fleet=fleet.id,
                     to="all" if to == "all" else targets[0].id, text=text)
            sent += 1
        if self.cfg["scuttle"]:
            for sid in self._as_list(actions.get("scuttle"))[:20]:
                try:
                    s = self.ships.get(int(sid))
                except (TypeError, ValueError):
                    fleet.warnings.append(f"⚠ scuttle IGNORED: {str(sid)[:20]!r} "
                                          "is not a ship id")
                    continue
                if s is None or s.fleet != fleet.id:
                    fleet.warnings.append(f"⚠ scuttle of ship {sid} IGNORED — "
                                          "no such ship of yours (already sunk?)")
                    continue
                fleet.cargo += self.cfg["scuttle_value"]
                if s.cargo > 0:            # her cargo goes down with her
                    self._add_node(s.x, s.y, "wreck", s.cargo, s.cargo,
                                   name=f"{fleet.name} {s.preset} wreck")
                self._ev("sink", fleet=s.fleet, ship=s.id, x=s.x, y=s.y,
                         preset=s.preset, by=None, cause="scuttle")
                for f2 in self.fleets.values():
                    f2.contacts.pop(s.id, None)
                del self.ships[s.id]
        sig = actions.get("signal")
        if isinstance(sig, dict) and sig.get("cancel"):
            fleet.queued_signal = None
            sig = None
        if sig:
            if fleet.signal_cd > 0:
                fleet.warnings.append(
                    f"signal NOT hoisted — cooldown ({fleet.signal_cd} windows left)")
            elif fleet.cargo < self.cfg["signal_cost"]:
                fleet.queued_signal = sig      # raises itself the moment funds exist
            else:
                self._exec_signal(fleet, sig)

    def _exec_signal(self, fleet, sig):
        """Execute a hoist (funds + cooldown already cleared by the caller)."""
        mode = self.cfg["signal_mode"]
        at_sea = [s for s in self.ships.values() if s.fleet == fleet.id
                  and cheb(s.x, s.y, fleet.hx, fleet.hy) > self.hr]
        hoisted = None
        if isinstance(sig, dict) and ("return" in sig or "return_safe" in sig):
            # RETURN TO PORT — the flag every mode understands, two urgencies:
            # return = NOW (beeline, even through danger); return_safe = turn
            # back now too, but evade any enemy encountered en route
            safe = "return" not in sig
            squads = sig["return_safe"] if safe else sig["return"]
            for s in at_sea:
                if squads == "all" or (isinstance(squads, (list, tuple))
                                       and s.squad in squads):
                    if safe:
                        if not s.recall:     # NOW already hoisted stays NOW
                            s.recall_safe = True
                    else:
                        s.recall, s.recall_safe = True, False
                    self._ev("orders", ship=s.id, fleet=fleet.id,
                             via="signal-return-safe" if safe
                             else "signal-return",
                             od=dict(role="return_safe" if safe else "return"))
            hoisted = "return_safe" if safe else "return"
        elif mode == "preset" and isinstance(sig, dict) and sig.get("hoist"):
            p = self.signal_presets.get(str(sig["hoist"]))
            if p:
                for squad, od in p.items():
                    clean = self._clean_order(fleet, od)
                    if clean is None:
                        continue
                    fleet.pending[squad[:1].upper()] = clean
                    for s in at_sea:
                        if s.squad == squad[:1].upper():
                            s.orders = dict(clean)
                            self._ev("orders", ship=s.id, fleet=fleet.id,
                                     via=f"signal:{sig['hoist']}",
                                     od=self._od_compact(clean))
                hoisted = str(sig["hoist"])
        elif mode == "custom":
            # classic instant push of current standing orders, payload-capped
            payload = json.dumps({sq: self._od_compact(od)
                                  for sq, od in fleet.pending.items()},
                                 separators=(",", ":"))
            if len(payload) <= self.cfg["signal_max_chars"]:
                for s in at_sea:
                    if s.squad in fleet.pending \
                            and s.orders != fleet.pending[s.squad]:
                        s.orders = dict(fleet.pending[s.squad])
                        self._ev("orders", ship=s.id, fleet=fleet.id,
                                 via="signal", od=self._od_compact(s.orders))
                    newp = fleet.pending_programs.get(s.squad)
                    if newp is not None and (s.program is None
                                             or s.program.text != newp.text):
                        s.program = newp
                        s.pmem = newp.init_mem()
                        self._ev("orders", ship=s.id, fleet=fleet.id,
                                 via="signal-program", od=dict(program=True))
                hoisted = "orders-push"
        if hoisted is not None:              # only a valid hoist costs anything
            fleet.cargo -= self.cfg['signal_cost']
            fleet.signal_cd = self.cfg['signal_cd']
            self._ev("signal", fleet=fleet.id, flag=hoisted)
        return hoisted is not None

    # ---------- catch-up window pipelining ----------
    def _pipe_window(self, live, t):
        """Catch-up pipelining: each admiral has at most ONE reply in flight.
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

    @staticmethod
    def _od_compact(od):
        return dict(role=od["role"], rally=od.get("rally"), aggr=od.get("aggression"),
                    retreat=od.get("retreat_hull_pct"), tf=od.get("target_fleet"))

    def _order_for(self, ship):
        return ship.orders

    def _anchor(self, ship, od):
        f = self.fleets[ship.fleet]
        if od["role"] == "escort":
            xs = [s for s in self.ships.values()
                  if s.fleet == ship.fleet and self._order_for(s)["role"] == "forage"]
            if xs:
                return (sum(s.x for s in xs) // len(xs), sum(s.y for s in xs) // len(xs))
        if od["role"] in ("blockade", "assault") and od.get("target_fleet") is not None:
            tf = self.fleets.get(od["target_fleet"])
            if tf and tf.alive:
                if od["role"] == "assault":
                    return (tf.hx, tf.hy)
                mx = tf.hx + sign(self.W // 2 - tf.hx) * 5
                my = tf.hy + sign(self.H // 2 - tf.hy) * 5
                return (mx, my)
        return tuple(od.get("rally") or (f.hx, f.hy))

    def _nearest_enemy(self, ship, rng_):
        best, bd = None, 10 ** 9
        host = self._hostile_to[ship.fleet]
        sx, sy = ship.x, ship.y
        for s in self.ships.values():
            if s.fleet not in host:
                continue
            dx = s.x - sx
            if dx < 0:
                dx = -dx
            if dx > rng_ or dx >= bd:
                continue
            dy = s.y - sy
            if dy < 0:
                dy = -dy
            if dy > rng_ or dy >= bd:
                continue
            d = dx if dx > dy else dy
            if d < bd:
                best, bd = s, d
        return best, bd

    def _pick_node(self, ship):
        f = self.fleets[ship.fleet]
        best, bkey = None, None
        bel = self._beliefs(f)
        for n in self.nodes.values():
            believed = bel[n.id]
            if believed <= 0:
                continue
            key = (cheb(ship.x, ship.y, n.x, n.y) * 10 - min(believed, 30), n.id)
            if bkey is None or key < bkey:
                best, bkey = n, key
        return best

    def _program_sensors(self, ship):
        """The ship's sensor readout — everything a helm program can read. Keys
        must exactly match conn.SENSORS (single source of truth over there)."""
        f = self.fleets[ship.fleet]
        hd = cheb(ship.x, ship.y, f.hx, f.hy)
        sen = {"self.x": ship.x, "self.y": ship.y,
               "self.hull_pct": (ship.hull * 100) // ship.hull_max,
               "self.cargo": ship.cargo, "self.hold_cap": ship.hold_cap,
               "self.power": ship.power(), "self.speed": ship.stats["speed"],
               "self.docked": 1.0 if hd <= self.hr else 0.0, "self.tick": self.t,
               "self.full": 1.0 if ship.cargo >= ship.hold_cap else 0.0,
               # stuck/idle detect a ship WEDGED AT SEA — they read 0 while
               # docked. A resting ship in its own harbor isn't moving or
               # gaining by design, so an un-clamped counter let a program
               # rule like `when self.stuck > N: helm.home()` self-latch a
               # docked ship forever (helm.home() when already home is a
               # no-op, so the counter never resets) — an absorbing state
               # that trapped whole trawler fleets in port (domination-5b g5).
               "self.stuck": 0 if hd <= self.hr else self.t - ship.moved_t,
               "self.idle": 0 if hd <= self.hr else self.t - ship.gained_t,
               "harbor.x": f.hx, "harbor.y": f.hy, "harbor.dist": hd}
        # territory awareness: the ground truth under the keel, so programs
        # can hold a point "until the territory is ours" without waiting a
        # whole decision window for the admiral to notice
        if self.regions:
            rid = self._cellregion[ship.x][ship.y]
            own = self.region_owner.get(rid)
            c2 = self._contest.get(rid)
            cap = max(1, self.cfg.get("territory_capture_ticks", 1))
            sen["terr.id"] = rid
            sen["terr.owner"] = -1 if own is None else own
            sen["terr.mine"] = 1.0 if (own is not None and
                                       not self._hostile(own, ship.fleet)) else 0.0
            sen["terr.capture"] = min(99, c2[1] * 100 // cap) if c2 else 0
        else:
            sen["terr.id"] = -2
            sen["terr.owner"] = -2
            sen["terr.mine"] = 0.0
            sen["terr.capture"] = 0
        if self._rank_tick != self.t:      # per-tick squadron rosters (cached)
            self._rank_tick = self.t
            self._ranks = {}
            for s2 in self.ships.values():
                self._ranks.setdefault((s2.fleet, s2.squad), []).append(s2.id)
            for ids in self._ranks.values():
                ids.sort()
        ids = self._ranks.get((ship.fleet, ship.squad), [ship.id])
        sen["self.rank"] = ids.index(ship.id) if ship.id in ids else 0
        sen["self.count"] = len(ids)
        en, ed, ecount = None, 10 ** 9, 0
        al, ad = None, 10 ** 9
        host = self._hostile_to[ship.fleet]
        vis = ship.vision
        sx, sy, sfid, sid = ship.x, ship.y, ship.fleet, ship.id
        for s2 in self.ships.values():
            dx = s2.x - sx
            if dx < 0:
                dx = -dx
            dy = s2.y - sy
            if dy < 0:
                dy = -dy
            d = dx if dx > dy else dy
            # team-aware: a teammate's ship is NEITHER enemy nor fleet-mate
            # (enemy sensors previously counted allies as enemies in team games)
            if s2.fleet in host:
                if d <= vis:
                    ecount += 1
                    if d < ed:
                        en, ed = s2, d
            elif s2.fleet == sfid and s2.id != sid and d < ad:
                al, ad = s2, d
        sen.update({"enemy.found": 1.0 if en else 0.0,
                    "enemy.x": en.x if en else 0, "enemy.y": en.y if en else 0,
                    "enemy.dist": ed if en else 9999,
                    "enemy.laden": 1.0 if en is not None and en.cargo > 0 else 0.0,
                    "enemy.power": en.power() if en else 0,
                    "enemy.stronger": 1.0 if en is not None
                    and en.power() > ship.power() else 0.0,
                    "enemy.count": ecount,
                    "ally.found": 1.0 if al else 0.0,
                    "ally.dist": ad if al else 9999})
        nd, ndd, nst = None, 10 ** 9, 0
        bel = self._beliefs(f)
        for n in self.nodes.values():
            b = bel[n.id]
            if b > 0:
                d = cheb(sx, sy, n.x, n.y)
                if d < ndd:
                    nd, ndd, nst = n, d, b
        sen.update({"node.found": 1.0 if nd else 0.0,
                    "node.x": nd.x if nd else 0, "node.y": nd.y if nd else 0,
                    "node.dist": ndd if nd else 9999, "node.stock": nst,
                    "node.kind": 1.0 if nd is not None and nd.kind == "wreck" else 0.0})
        od = ship.orders
        tf = od.get("target_fleet")
        rv = self.fleets.get(tf) if tf is not None else None
        rv = rv if rv is not None and rv.alive else None
        r = od.get("rally") or (f.hx, f.hy)
        # nearest hostile FLAGSHIP — the domination target. Harbor positions are
        # on the map (public), so x/y/dist are always given; hull is revealed
        # ONLY when THIS ship is close enough to scout it (get in, take the risk).
        host = self._hostile_to[ship.fleet]
        fx = fy = 0
        fdist = 9999
        fhull = -1.0
        best_ef = None
        for ef in self.fleets.values():
            if ef.id in host and ef.alive:
                d = cheb(ship.x, ship.y, ef.hx, ef.hy)
                if d < fdist:
                    fdist, best_ef, fx, fy = d, ef, ef.hx, ef.hy
        ybusy = -1.0
        if best_ef is not None and fdist <= self.hr + 6:
            fhull = float(best_ef.flag_hull)
            if self.cfg["shipyard_slots"] > 0:
                ybusy = float(self._yard.get(best_ef.id, 0))
        sen.update({"rival.found": 1.0 if rv else 0.0,
                    "rival.x": rv.hx if rv else 0, "rival.y": rv.hy if rv else 0,
                    "rival.flag_x": fx, "rival.flag_y": fy,
                    "rival.flag_dist": fdist, "rival.flag_hull": fhull,
                    "rival.yard_busy": ybusy,
                    "orders.rally_x": r[0], "orders.rally_y": r[1],
                    "orders.aggression": od.get("aggression", 0),
                    "orders.retreat": od.get("retreat_hull_pct", 0)})
        return sen, en

    def _run_program(self, ship):
        """One program tick. Returns a movement target/None (like _ship_target),
        or NotImplemented to fall back to role logic on a runtime fault."""
        sen, en = self._program_sensors(ship)
        try:
            out = ship.program.run(sen, ship.pmem)
        except conn.ConnError as e:
            self._intent(ship, f"program fault: {e} — standing orders take over")
            f = self.fleets[ship.fleet]
            w = (f"⚠ squadron {ship.squad} program RUNTIME FAULT: {str(e)[:160]}"
                 " — its ships fell back to standing orders")
            if w not in f.warnings:        # fires per tick; report once per window
                f.warnings.append(w)
            return NotImplemented
        f = self.fleets[ship.fleet]
        if out is None:
            self._intent(ship, "program: no rule fired — holding")
            return None
        verb, args, ln = out
        ship.assault_flag = (verb == "assault")   # fresh each tick; read by combat
        if verb == "assault":
            # sail onto the nearest hostile flagship and prioritize its guns.
            # Positions are public (harbors on the map); no target_fleet needed.
            host = self._hostile_to[ship.fleet]
            best, bd = None, 10 ** 9
            for ef in self.fleets.values():
                if ef.id in host and ef.alive:
                    d = cheb(ship.x, ship.y, ef.hx, ef.hy)
                    if d < bd:
                        best, bd = ef, d
            if best is not None:
                self._intent(ship, f"program L{ln}: assault {best.name}'s flagship")
                return (best.hx, best.hy) if (best.hx, best.hy) != (ship.x, ship.y) else None
            self._intent(ship, f"program L{ln}: assault — no enemy flagship left")
            return None
        if verb == "goto":
            try:
                x = max(0, min(self.W - 1, int(args[0])))
                y = max(0, min(self.H - 1, int(args[1])))
            except (OverflowError, ValueError):
                # conn._fin now faults on non-finite arithmetic at the source, so
                # this is reachable only from a pre-fix checkpoint whose pmem
                # holds Infinity. int() on it raised OverflowError here and took
                # the whole run down; hold instead, and say so in the intent log.
                self._intent(ship, f"program L{ln}: goto(non-finite) — holding")
                return None
            self._intent(ship, f"program L{ln}: goto({x},{y})")
            return (x, y) if (x, y) != (ship.x, ship.y) else None
        if verb == "home":
            self._intent(ship, f"program L{ln}: home")
            return (f.hx, f.hy)
        if verb == "hold":
            self._intent(ship, f"program L{ln}: hold")
            return None
        if verb == "gather":
            self._intent(ship, f"program L{ln}: gather")
            return None                     # stationary-on-node gathering kicks in
        if verb == "attack":
            if en is not None:
                self._intent(ship, f"program L{ln}: attack {en.preset} {en.id}")
                return (en.x, en.y)
            self._intent(ship, f"program L{ln}: attack — no enemy in sight, holding")
            return None
        if en is not None:                  # flee
            self._intent(ship, f"program L{ln}: flee from {en.preset} {en.id}")
            return (max(0, min(self.W - 1, ship.x + sign(ship.x - en.x) * 4)),
                    max(0, min(self.H - 1, ship.y + sign(ship.y - en.y) * 4)))
        self._intent(ship, f"program L{ln}: flee — no enemy in sight, holding")
        return None

    def _ship_target(self, ship):
        """Returns (tx, ty) or None (hold position). Sets ship.intent — the recorded
        'what am I doing and why' that replay review (human or agent) reads."""
        ship.assault_flag = False          # _run_program re-sets it this tick if
        f = self.fleets[ship.fleet]        # helm.assault() fires; else it stays off
        if ship.repair_at:                  # in the yard: hold until repaired
            self._intent(ship, "in drydock: under repair")
            return None
        if ship.refit_to is not None:       # in the drydock: hold until works finish
            self._intent(ship, f"in drydock: refitting to {ship.refit_to}")
            return None
        if ship.recall:                     # RETURN flag: home for orders, no detours
            self._intent(ship, "recalled by signal: making for port to collect orders")
            return (f.hx, f.hy)
        if ship.recall_safe:                # RETURN-SAFE: home NOW, defensively —
            # the NOW recall beelines even through a gun line; this one breaks
            # off and evades any enemy met en route, then resumes homing
            # (same 6/10 trigger/release hysteresis as the autopilot flee)
            enemy, d = self._nearest_enemy(ship, ship.vision)
            evading = ship.intent.startswith("return-safe: evading")
            if enemy is not None and d <= (10 if evading else 6):
                self._intent(ship, f"return-safe: evading {enemy.preset} "
                                   f"{enemy.id}, then home")
                return (max(0, min(self.W - 1,
                                   ship.x + sign(ship.x - enemy.x) * 4)),
                        max(0, min(self.H - 1,
                                   ship.y + sign(ship.y - enemy.y) * 4)))
            self._intent(ship, "return-safe: making for port, avoiding trouble")
            return (f.hx, f.hy)
        if ship.program is not None:        # a conn program has this ship
            r = self._run_program(ship)
            if r is not NotImplemented:     # NotImplemented = runtime fault -> role
                return r
        if not self.cfg["role_fallback"]:   # hard mode: your code IS the fleet
            self._intent(ship, "idle: no conn program (role autopilot disabled)")
            return None
        od = self._order_for(ship)
        role = od["role"]
        ax, ay = self._anchor(ship, od)
        # retreat gate
        if ship.hull * 100 < od["retreat_hull_pct"] * ship.hull_max:
            self._intent(ship, f"retreat: hull below {od['retreat_hull_pct']}%, running home")
            return (f.hx, f.hy)
        # engagement
        enemy, d = self._nearest_enemy(ship, ship.vision)
        if enemy is not None:
            aggr = od["aggression"]
            engage = (aggr == 3 or
                      (aggr == 2 and ship.power() >= enemy.power()) or
                      (aggr == 1 and enemy.id in ship.attackers))
            if role == "raid" and aggr >= 2 and enemy.cargo > 0:
                engage = True
            if engage and cheb(ax, ay, enemy.x, enemy.y) <= self.cfg['leash']:
                self._intent(ship, f"engage: pursuing enemy ship {enemy.id} ({enemy.preset})")
                return (enemy.x, enemy.y)
            # hysteresis: start fleeing inside 6, keep fleeing until clear of 10 —
            # equal trigger/release radii caused the flee<->resume flip-flop loop
            # that the intent log diagnosed (P0.1)
            fleeing = ship.intent.startswith("flee")
            # escorted courage: a stronger friendly already covering the threat means no
            # flee — this is what makes pickets/escorts actually protect anyone
            _ep, _ex, _ey, _fid, _sid = (enemy.power(), enemy.x, enemy.y,
                                         ship.fleet, ship.id)
            covered = any(s2.fleet == _fid and s2.id != _sid
                          and -4 <= s2.x - _ex <= 4 and -4 <= s2.y - _ey <= 4
                          and s2.power() >= _ep
                          for s2 in self.ships.values())
            if aggr == 0 and covered and d <= (10 if fleeing else 6):
                self._intent(ship, "steady: threat covered by our escort, working on")
            elif aggr == 0 and d <= (10 if fleeing else 6):
                if ship.cargo > 0:
                    ship.haul_home = True   # commit: land this cargo before going back out
                    self._intent(ship, "flee: threat nearby, laden, running home")
                    return (f.hx, f.hy)
                self._intent(ship, "flee: threat nearby, evading")
                return (max(0, min(self.W - 1, ship.x + sign(ship.x - enemy.x) * 4)),
                        max(0, min(self.H - 1, ship.y + sign(ship.y - enemy.y) * 4)))
        # role behavior
        if role == "forage":
            if ship.cargo >= ship.hold_cap or (ship.haul_home and ship.cargo > 0):
                ship.node_id = None
                self._intent(ship, "haul: hold full, heading to port" if ship.cargo >= ship.hold_cap
                             else "haul: delivering early (was threatened)")
                return (f.hx, f.hy)
            # committed-target foraging: keep the chosen node until it's empty or we're
            # laden — re-picking every tick caused visible flip-flop loops (P0.1 viewer feedback)
            n = self.nodes.get(ship.node_id) if ship.node_id is not None else None
            if n is not None and self.believed(f, n) <= 0:
                n = None
            if n is None:
                n = self._pick_node(ship)
                ship.node_id = n.id if n is not None else None
            if n is None:
                self._intent(ship, "idle: no known cargo anywhere, waiting in port")
                return (f.hx, f.hy)
            if (ship.x, ship.y) == (n.x, n.y):
                if n.remaining <= 0:
                    f.node_mem[n.id] = (0, self.t)
                    f._bel_v = getattr(f, "_bel_v", 0) + 1
                    ship.node_id = None
                    self._intent(ship, f"{n.name} is fished dry, re-picking")
                    return None
                self._intent(ship, f"gather: loading at {n.name}")
                return None            # sit and gather
            self._intent(ship, f"sail: heading to {n.name}")
            return (n.x, n.y)
        if role == "scout":
            ring = [(ax + 10, ay), (ax + 7, ay + 7), (ax, ay + 10), (ax - 7, ay + 7),
                    (ax - 10, ay), (ax - 7, ay - 7), (ax, ay - 10), (ax + 7, ay - 7)]
            tx, ty = ring[ship.wp_i % 8]
            tx, ty = max(1, min(self.W - 2, tx)), max(1, min(self.H - 2, ty))
            self._intent(ship, f"scout: patrol leg {ship.wp_i % 8}")
            if (ship.x, ship.y) == (tx, ty):
                ship.wp_i += 1
                return None
            return (tx, ty)
        if role == "assault":
            tf = self.fleets.get(od.get("target_fleet") if od.get("target_fleet") is not None else -1)
            if tf is not None and tf.alive:
                self._intent(ship, f"assault: attacking {tf.name}'s flagship")
                return (tf.hx, tf.hy)      # sail onto the flagship; combat loop does the rest
        # guard / escort / raid / blockade: loiter at anchor (escort's anchor is the
        # forager centroid, which moves every tick — keep its intent string stable)
        stn = "screening the foragers" if role == "escort" else f"station ({ax},{ay})"
        if cheb(ship.x, ship.y, ax, ay) > 2:
            self._intent(ship, f"{role}: moving to {stn}")
            return (ax, ay)
        self._intent(ship, f"{role}: holding {stn}")
        return None

    def _combat_note(self, fid, ship_key, other, dealt, taken, x, y):
        """Accumulate an engagement on a fleet's between-window combat report.
        ship_key = own ship id or "flagship". Aggregated per (ship, rival)."""
        rec = self.fleets[fid].combat.setdefault(
            (ship_key, other), dict(dealt=0, taken=0, x=x, y=y))
        rec["dealt"] += dealt
        rec["taken"] += taken
        rec["x"], rec["y"] = x, y

    def _hostile(self, fa, fb):
        """Team-aware enmity: same team (default: own id) = never hostile."""
        return self.fleets[fa].team != self.fleets[fb].team

    _DIRS = ((1, 0), (1, 1), (0, 1), (-1, 1), (-1, 0), (-1, -1), (0, -1), (1, -1))
    _DIRORD = {}                      # OPT-F: (dx,dy) -> bearing-sorted dirs (9 keys)

    def _step_cell(self, x, y, tx, ty, prev=None):
        """One grid step toward (tx,ty) that respects impassable land. All eight
        directions, tried in bearing order (closest to the target heading first),
        skipping the cell we just came from — the no-backtrack memory is what
        lets a ship ROUND a convex island instead of oscillating at its tip.
        If the target itself is on land, pin against the shore instead of
        orbiting it forever. Returns the same cell only when truly boxed in."""
        dx, dy = sign(tx - x), sign(ty - y)
        if (tx, ty) in self.blocked:       # sail AT land: wall up, don't orbit
            cands = ((dx, dy), (dx, 0), (0, dy))
            for cx, cy in cands:
                nx = max(0, min(self.W - 1, x + cx))
                ny = max(0, min(self.H - 1, y + cy))
                if (nx, ny) != (x, y) and (nx, ny) not in self.blocked:
                    return nx, ny
            return x, y
        back = None
        order = self._DIRORD.get((dx, dy))
        if order is None:
            order = self._DIRORD[(dx, dy)] = tuple(sorted(
                self._DIRS, key=lambda c: ((c[0] - dx) ** 2 + (c[1] - dy) ** 2, c)))
        for cx, cy in order:
            nx = max(0, min(self.W - 1, x + cx))
            ny = max(0, min(self.H - 1, y + cy))
            if (nx, ny) == (x, y) or (nx, ny) in self.blocked:
                continue
            if prev is not None and (nx, ny) == prev:
                back = (nx, ny)            # only retreat if it's the sole way out
                continue
            return nx, ny
        return back or (x, y)

    def _move(self, ship, tx, ty):
        ship.move_acc += ship.stats["speed"]
        while ship.move_acc >= MOVE_THRESH:
            ship.move_acc -= MOVE_THRESH
            if (ship.x, ship.y) == (tx, ty):
                break
            if not self.blocked:           # open sea: the original straight step
                ship.x = max(0, min(self.W - 1, ship.x + sign(tx - ship.x)))
                ship.y = max(0, min(self.H - 1, ship.y + sign(ty - ship.y)))
                ship.moved_t = self.t
                continue
            nx, ny = self._step_cell(ship.x, ship.y, tx, ty, ship.nav_prev)
            if (nx, ny) == (ship.x, ship.y):
                break                      # pinned against land this tick
            ship.nav_prev = (ship.x, ship.y)
            ship.x, ship.y = nx, ny
            ship.moved_t = self.t

    # ---------- tick ----------
    def tick(self):
        t = self.t
        # shipyard occupancy, counted ONCE per tick: every build/refit/repair
        # in progress holds a slot; start sites check + increment this, so a
        # slot freed by a completion opens up next tick (deterministic)
        if self.cfg["shipyard_slots"] > 0:
            yard = {fid: len(f.builds) for fid, f in self.fleets.items()}
            for s in self.ships.values():
                if s.refit_to is not None or s.repair_at:
                    yard[s.fleet] += 1
            self._yard = yard
        # decision windows: all admirals see the same tick's state; LLM calls run
        # concurrently (lockstep), results apply in fleet-id order (deterministic)
        if t == 0 and self.cfg["warmup"]:
            # pre-game planning: admirals study the rules + opening view and write
            # an opening plan (kept in their context; shown in the replay)
            planners = [f for f in self.fleets.values()
                        if f.alive and hasattr(f.bot, "plan")]

            def _plan(f):
                try:
                    return f.bot.plan(self.summary_for(f))
                except Exception as e:
                    return dict(plan="", err=f"{type(e).__name__}: {e}"[:120],
                                tin=0, tout=0, ms=0, cost=0.0)

            if len(planners) > 1:
                with ThreadPoolExecutor(max_workers=len(planners)) as ex:
                    outs = list(ex.map(_plan, planners))
            else:
                outs = [_plan(f) for f in planners]
            for f, o in zip(planners, outs):
                head = ("(opening plan) " + o["plan"]) if o.get("plan") \
                    else f"(warmup failed: {o.get('err')})"
                self.decisions.append(dict(
                    t=0, fleet=f.id, thoughts=head[:400], plan=o.get("plan", ""),
                    u=dict(model=getattr(f.bot, "model_label", None),
                           tin=o.get("tin", 0), tout=o.get("tout", 0),
                           ms=o.get("ms", 0), cost=o.get("cost", 0.0),
                           err=o.get("err"))))
        if t % self.cfg['window'] == 0:
            live = [f for f in self.fleets.values() if f.alive]
            for f in live:
                if f.signal_cd > 0:
                    f.signal_cd -= 1
            if self.cfg["pipeline_depth"] > 0:
                self._pipe_window(live, t)
            else:
                jobs = []
                for f in live:
                    summary = self.summary_for(f)
                    bot_rng = random.Random((self.seed << 8) ^ (f.id << 4) ^ (t // self.cfg['window']))
                    jobs.append((f, summary, bot_rng))

                def _decide(job):
                    f, summary, bot_rng = job
                    try:
                        return f.bot.decide(summary, bot_rng)
                    except Exception as e:              # a broken admiral never crashes the sim
                        return dict(thoughts=f"(admiral error: {e})")

                if len(jobs) > 1:
                    with ThreadPoolExecutor(max_workers=len(jobs)) as ex:
                        results = list(ex.map(_decide, jobs))
                else:
                    results = [_decide(j) for j in jobs]
                for (f, _, _), actions in zip(jobs, results):
                    try:
                        self._apply_actions(f, actions)
                    except Exception as e:  # NOTHING an admiral emits kills the sim
                        # per-field isolation lives INSIDE _apply_actions —
                        # reaching this is an engine fault, not a bad field
                        f.warnings.append(
                            f"⚠ an internal error interrupted your orders "
                            f"this window "
                            f"({type(e).__name__}: {str(e)[:200]})")
                    f.recent_hits = 0
                    f.combat = {}
        # passive income + queued signals (both fire the tick funds allow)
        if self.cfg["income_amount"] > 0 and t > 0 \
                and t % self.cfg["income_period"] == 0:
            for f in self.fleets.values():
                if f.alive:
                    f.cargo += self.cfg["income_amount"]
        for f in self.fleets.values():
            if f.alive and f.queued_signal is not None and f.signal_cd == 0 \
                    and f.cargo >= self.cfg["signal_cost"]:
                sig, f.queued_signal = f.queued_signal, None
                self._exec_signal(f, sig)
        # builds — shipyard model: up to shipyard_slots concurrent works per
        # fleet, shared with refits + repairs (the strategic capacity squeeze)
        slots = self.cfg["shipyard_slots"]
        if slots <= 0:                       # legacy: one serialized build
            for f in self.fleets.values():
                if not f.alive or not f.build_q:
                    continue
                f.build_t += 1
                if f.build_t >= self.cfg['build_ticks']:
                    f.build_t = 0
                    preset, squad = f.build_q.pop(0)
                    self._spawn(f, preset, squad)
        else:
            for f in self.fleets.values():
                if not f.alive:
                    continue
                if f.yard_done_t and t >= f.yard_done_t:   # expansion done
                    f.yard_done_t = 0
                    f.yards += 1
                    self._ev("yard_built", fleet=f.id, slots=f.yards)
                done = [b for b in f.builds if t >= b[2]]
                if done:
                    f.builds = [b for b in f.builds if t < b[2]]
                    for preset, squad, _ in done:
                        self._spawn(f, preset, squad)
                        self._yard[f.id] -= 1
                while f.build_q and self._yard.get(f.id, 0) < f.yards:
                    preset, squad = f.build_q.pop(0)
                    f.builds.append([preset, squad, t + self.cfg["build_ticks"]])
                    self._yard[f.id] = self._yard.get(f.id, 0) + 1
                    self._ev("build_start", fleet=f.id, preset=preset,
                             ready=t + self.cfg["build_ticks"])
        # drydock clock: refit + repair works complete on TIME, wherever the
        # ship lies — completion used to live inside the docked branch, so a
        # flagship that relocated away froze her refitting ships forever
        # (cost already paid)
        for s in self.ships.values():
            if s.refit_to is not None and t >= s.refit_at:
                f = self.fleets[s.fleet]
                st = self.class_stats(f, s.refit_to)
                if st is not None:
                    s.preset = s.refit_to
                    s.stats = dict(st)
                    s.hull_max = 20 + st["hull"] * 10
                    s.hull = min(s.hull, s.hull_max)
                    self._ev("refit", fleet=f.id, ship=s.id, to=s.refit_to)
                s.refit_to = None
            if s.repair_at and t >= s.repair_at:
                s.repair_at = 0
                s.hull = s.hull_max
                self._ev("repaired", fleet=s.fleet, ship=s.id)
        # flagship relocation: the anchorage (and the whole command circle) sails
        if self.cfg["flag_move"]:
            for f in self.fleets.values():
                if not f.alive or f.flag_target is None:
                    continue
                f.flag_acc += self.cfg["flag_speed"]
                while f.flag_acc >= MOVE_THRESH and f.flag_target is not None:
                    f.flag_acc -= MOVE_THRESH
                    if (f.hx, f.hy) == f.flag_target:
                        f.flag_target = None
                        self._ev("flag_arrive", fleet=f.id, at=[f.hx, f.hy])
                        break
                    nx, ny = self._step_cell(f.hx, f.hy, *f.flag_target, f.flag_prev)
                    if (nx, ny) == (f.hx, f.hy):     # pinned against land: give up
                        f.flag_target = None
                        self._ev("flag_arrive", fleet=f.id, at=[f.hx, f.hy])
                        break
                    f.flag_prev = (f.hx, f.hy)
                    f.hx, f.hy = nx, ny
        # ships act (id order = deterministic)
        for s in list(self.ships.values()):
            f = self.fleets[s.fleet]
            docked = cheb(s.x, s.y, f.hx, f.hy) <= self.hr
            if docked:
                s.recall = s.recall_safe = False
                nsq = f.pending_reassign.pop(s.id, None)
                if nsq is not None and nsq != s.squad:   # transfer applies in port;
                    s.squad = nsq                        # new squad's orders/program
                    self._ev("reassign", fleet=f.id, ship=s.id, squad=nsq)
                if s.trip_start is not None:         # back from a voyage: file report
                    if self.cfg["voyage_reports"]:
                        line = (f"{s.preset} #{s.id} ({s.squad}) returned: "
                                f"{(t - s.trip_start) / 10:.0f}s out, delivered "
                                f"{s.cargo}, gathered {s.trip_gathered}, took "
                                f"{s.trip_hits} hits, ranged {s.trip_far} cells")
                        f.reports_pending.append(line)
                        self._ev("report", fleet=f.id, ship=s.id, s=line)
                    s.trip_start = None
                    s.trip_gathered = s.trip_hits = s.trip_far = 0
                if s.cargo:
                    f.cargo += s.cargo
                    f.bank += s.cargo
                    self._ev("deposit", fleet=f.id, ship=s.id, amount=s.cargo)
                    s.cargo = 0
                # docking completes the delivery commitment even when she
                # arrived EMPTY (cargo lost at sea) — a sticky flag made her
                # haul home after every single gathered unit next trip
                s.haul_home = False
                new_od = f.pending.get(s.squad) or DEFAULT_ORDER
                if s.orders != new_od:
                    s.orders = dict(new_od)
                    self._ev("orders", ship=s.id, fleet=f.id, via="port",
                             od=self._od_compact(new_od))
                newp = f.pending_programs.get(s.squad)
                if newp is not None:
                    if s.program is None or s.program.text != newp.text:
                        s.program = newp
                        s.pmem = newp.init_mem()
                        self._ev("orders", ship=s.id, fleet=f.id, via="port-program",
                                 od=dict(program=True))
                elif s.program is not None:          # program was cleared fleet-side
                    s.program = None
                    s.pmem = {}
                yslots = self.cfg["shipyard_slots"]
                yfree = yslots <= 0 or self._yard.get(f.id, 0) < f.yards
                rf = f.pending_refits.get(s.squad)
                if rf and rf != s.preset and s.refit_to is None \
                        and s.repair_at == 0 and yfree \
                        and f.cargo >= self.refit_class_cost(f, rf):
                    st = self.class_stats(f, rf)
                    if st is not None:
                        f.cargo -= self.refit_class_cost(f, rf)   # paid up front
                        # -1 => match build_ticks (no instant fleet-swaps)
                        rticks = self.cfg["refit_ticks"]
                        if rticks < 0:
                            rticks = self.cfg["build_ticks"]
                        if rticks <= 0:
                            s.preset = rf
                            s.stats = dict(st)
                            s.hull_max = 20 + st["hull"] * 10
                            s.hull = min(s.hull, s.hull_max)
                            self._ev("refit", fleet=f.id, ship=s.id, to=rf)
                        else:                        # held in the drydock
                            s.refit_to = rf
                            s.refit_at = t + rticks
                            if yslots > 0:
                                self._yard[f.id] = self._yard.get(f.id, 0) + 1
                                yfree = self._yard[f.id] < f.yards
                            self._ev("refit_start", fleet=f.id, ship=s.id, to=rf,
                                     ready=s.refit_at)
                if yslots <= 0:              # legacy: free trickle repair
                    if s.hull < s.hull_max and t % self.cfg['repair_period'] == 0:
                        s.hull = min(s.hull_max, s.hull + 2)
                elif s.hull < s.hull_max and s.repair_at == 0 \
                        and s.refit_to is None:
                    # yard repair: cost + time scale with missing hull — always
                    # cheaper and quicker than a new build (defaults 40%/40%),
                    # so limping home beats going down swinging
                    miss = s.hull_max - s.hull
                    ccost = self.class_cost(f, s.preset)
                    cost = max(1, -(-miss * ccost
                                    * self.cfg["repair_cost_pct"]
                                    // (s.hull_max * 100)))
                    rt = max(1, -(-miss * self.cfg["build_ticks"]
                                  * self.cfg["repair_ticks_pct"]
                                  // (s.hull_max * 100)))
                    if not yfree:
                        pass                 # she waits docked for a slot
                    elif f.cargo < cost:
                        w = (f"⚠ repair of ship {s.id} WAITING — needs {cost} "
                             f"treasury (have {f.cargo}); she sits damaged in "
                             "port until you can pay")
                        if w not in f.warnings:
                            f.warnings.append(w)
                    else:
                        f.cargo -= cost
                        s.repair_at = t + rt
                        self._yard[f.id] = self._yard.get(f.id, 0) + 1
                        self._ev("repair_start", fleet=f.id, ship=s.id,
                                 cost=cost, ready=s.repair_at)
            else:
                if s.trip_start is None:             # casting off
                    s.trip_start = t
                s.trip_far = max(s.trip_far, cheb(s.x, s.y, f.hx, f.hy))
            tgt = self._ship_target(s)
            if tgt is not None and tgt != (s.x, s.y):
                self._move(s, *tgt)
            elif t % self.cfg['gather_period'] == 0 and s.cargo < s.hold_cap:
                # gathering? (both gates are loop-invariant — checked before the scan)
                sx, sy = s.x, s.y
                for n in self.nodes.values():
                    if n.x == sx and n.y == sy and n.remaining > 0:
                        n.remaining -= 1
                        s.cargo += 1
                        s.gained_t = self.t
                        s.trip_gathered += 1
                        break
        # combat
        for s in list(self.ships.values()):
            if s.volley_cd > 0:
                s.volley_cd -= 1
                continue
            # (cooldown is set to volley-1 below: decrement-then-skip plus a
            # full reload made the real cadence volley+1 ticks — ships fired
            # every 6 while the rules promised 5 and flagships kept 5)
            if s.orders["role"] == "assault" or s.assault_flag:  # flag IS the target
                # (role=assault OR a conn helm.assault() this tick — both make
                # the enemy flagship the priority over any adjacent escort)
                hit_flag = False
                for f in self.fleets.values():
                    if f.alive and self._hostile(f.id, s.fleet) \
                            and cheb(s.x, s.y, f.hx, f.hy) <= 1:
                        dmg = max(1, s.stats["guns"] * 2 - 3)
                        f.flag_hull -= dmg
                        f.recent_hits += 1
                        self._combat_note(s.fleet, s.id, f.id, dmg, 0, f.hx, f.hy)
                        self._combat_note(f.id, "flagship", s.fleet, 0, dmg,
                                          f.hx, f.hy)
                        s.volley_cd = self.cfg['volley'] - 1
                        hit_flag = True
                        break
                if hit_flag:
                    continue
            enemy, d = self._nearest_enemy(s, 1)
            tgt_fleet = None
            if enemy is not None:
                dmg = max(1, s.stats["guns"] * 2 - enemy.stats["armor"])
                enemy.hull -= dmg
                enemy.trip_hits += 1
                enemy.attackers.add(s.id)
                self.fleets[enemy.fleet].recent_hits += 1
                self._combat_note(s.fleet, s.id, enemy.fleet, dmg, 0,
                                  enemy.x, enemy.y)
                self._combat_note(enemy.fleet, enemy.id, s.fleet, 0, dmg,
                                  enemy.x, enemy.y)
                s.volley_cd = self.cfg['volley'] - 1
            else:
                for f in self.fleets.values():
                    if f.alive and self._hostile(f.id, s.fleet) \
                            and cheb(s.x, s.y, f.hx, f.hy) <= 1:
                        dmg = max(1, s.stats["guns"] * 2 - 3)
                        f.flag_hull -= dmg
                        f.recent_hits += 1
                        self._combat_note(s.fleet, s.id, f.id, dmg, 0, f.hx, f.hy)
                        self._combat_note(f.id, "flagship", s.fleet, 0, dmg,
                                          f.hx, f.hy)
                        s.volley_cd = self.cfg['volley'] - 1
                        tgt_fleet = f
                        break
            _ = tgt_fleet
        # flagship battery: the WHOLE command circle is a kill zone — any
        # hostile inside it takes fire, up to flag_targets ships per volley
        # (nearest first, id-ordered for determinism). A big enough fist
        # still overwhelms the battery; a lone snooper does not survive it.
        if t % self.cfg['volley'] == 0:
          ntgt = self.cfg.get("flag_targets", 4)
          for f in self.fleets.values():
            if not f.alive:
                continue
            host = self._hostile_to[f.id]
            fhx, fhy = f.hx, f.hy
            inzone = []
            for s in self.ships.values():
                if s.fleet not in host:
                    continue
                dx = s.x - fhx
                if dx < 0:
                    dx = -dx
                if dx > self.hr:
                    continue
                dy = s.y - fhy
                if dy < 0:
                    dy = -dy
                if dy > self.hr:
                    continue
                inzone.append(((dx if dx > dy else dy), s.id, s))
            inzone.sort(key=lambda e: (e[0], e[1]))
            for _, _, tgt in inzone[:ntgt]:
                dmg = max(1, 5 * 2 - tgt.stats["armor"])
                tgt.hull -= dmg
                tgt.trip_hits += 1
                tgt.flag_attackers.add(f.id)    # so a flagship-only kill still
                                                # credits someone (was nobody)
                self.fleets[tgt.fleet].recent_hits += 1
                self._combat_note(f.id, "flagship", tgt.fleet, dmg, 0,
                                  tgt.x, tgt.y)
                self._combat_note(tgt.fleet, tgt.id, f.id, 0, dmg,
                                  tgt.x, tgt.y)
        # (end flagship battery)
        # deaths
        for s in [s for s in self.ships.values() if s.hull <= 0]:
            if s.cargo > 0:
                n = self._add_node(s.x, s.y, "wreck", s.cargo, s.cargo,
                                   name=f"{self.fleets[s.fleet].name} "
                                        f"{s.preset} wreck")
                for f in self.fleets.values():
                    f.node_mem[n.id] = (s.cargo, self.t) if self._fleet_sees(f, s.x, s.y) \
                        else (0, self.t)
                    f._bel_v = getattr(f, "_bel_v", 0) + 1
            # credit the sink to the fleet of any attacker still afloat
            by = None
            for aid in sorted(s.attackers):
                a = self.ships.get(aid)
                if a is not None and a.fleet != s.fleet:
                    self.fleets[a.fleet].kills += self.cfg['kill_score']
                    self.fleets[a.fleet].kill_count += 1
                    by = a.fleet
                    break
            if by is None:                  # sunk purely by a flagship battery:
                for fid in sorted(s.flag_attackers):   # credit that flagship's
                    if fid != s.fleet and self.fleets[fid].alive:   # fleet
                        self.fleets[fid].kills += self.cfg['kill_score']
                        self.fleets[fid].kill_count += 1
                        by = fid
                        break
            self._ev("sink", fleet=s.fleet, ship=s.id, x=s.x, y=s.y, preset=s.preset,
                     by=by)
            for f in self.fleets.values():           # witnessed sinks clear the plot;
                if f.id != s.fleet and s.id in f.contacts \
                        and self._fleet_sees(f, s.x, s.y):
                    del f.contacts[s.id]             # unwitnessed ghosts persist (fog)
            del self.ships[s.id]
        # flagship deaths
        for f in self.fleets.values():
            if f.alive and f.flag_hull <= 0:
                f.alive = False
                # a dead fleet's capture claims die with it NOW — leaving them
                # for the next territory evaluation had frames reporting a
                # ghost "contested_by" for up to territory_tick ticks
                for rid in [r for r, c in self._contest.items()
                            if c[0] == f.id]:
                    self._contest.pop(rid, None)
                # credit: the HOSTILE fleet with a ship nearest the harbor gets
                # the bounty — same-fleet is not enough, a TEAMMATE standing by
                # must never profit from its ally's elimination
                best, bd = None, 10 ** 9
                for s in self.ships.values():
                    if self._hostile(s.fleet, f.id):
                        d = cheb(s.x, s.y, f.hx, f.hy)
                        if d < bd:
                            best, bd = s, d
                if best is not None:
                    self.fleets[best.fleet].kills += self.cfg['flag_kill_score']
                    self.fleets[best.fleet].kill_count += 1
                for s in [s for s in self.ships.values() if s.fleet == f.id]:
                    del self.ships[s.id]
                # elimination is ANNOUNCED to every admiral, so ghost contacts
                # of the dead fleet are impossible data — purge them from every
                # plot (they'd otherwise haunt state.enemies for contact_ttl)
                for g in self.fleets.values():
                    for sid in [sid for sid, rec in g.contacts.items()
                                if rec["fleet"] == f.id]:
                        del g.contacts[sid]
                f.contacts = {}
                # optional: a destroyed flagship spills a rich wreck at its
                # harbor — a bounty for whoever made the kill, and bait for
                # vultures quick enough to bring trawlers over
                if self.cfg.get("flag_salvage", 0) > 0:
                    sv = self.cfg["flag_salvage"]
                    n = self._add_node(f.hx, f.hy, "wreck", sv, sv,
                                       name=f"{f.name} flagship wreck")
                    for g in self.fleets.values():
                        g.node_mem[n.id] = (sv, self.t) if self._fleet_sees(g, f.hx, f.hy) \
                            else (0, self.t)
                        g._bel_v = getattr(g, "_bel_v", 0) + 1
                self._ev("flag_sunk", fleet=f.id,
                         by=best.fleet if best is not None else None)
        if self.regions and self.cfg.get("territory_capture_ticks", 0) > 0:
            self._capture_tick()               # per-tick capture clocks
        if self.regions and t % self.cfg['territory_tick'] == 0:
            self._territory_tick()             # scoring (and legacy flips)
        # node regen
        for n in self.nodes.values():
            if n.kind == "fish" and n.remaining < n.cap:
                n.regen_acc += 1
                if n.regen_acc >= self.cfg['fish_regen_period']:
                    n.regen_acc = 0
                    n.remaining += 1
        self._update_contacts()
        if t % FRAME_EVERY == 0:
            self._frame()
        if self.live is not None and (t + 1) % self.cfg["window"] == 0:
            self._live_flush()             # one flush per completed window chunk
        self.t += 1

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

    def live_header(self):
        """The live stream's opening line: everything the viewer needs before any
        frame exists (same shapes as replay())."""
        return dict(header=True, meta=dict(
            seed=self.seed, w=self.W, h=self.H, tick_hz=TICKS_PER_SEC,
            frame_every=FRAME_EVERY, window=self.cfg["window"],
            presets={**PRESETS, **self.shared_designs}, harbor_r=self.hr,
            ship_cost=self.cfg["ship_cost"], scenario=self.scenario,
            config={k: v for k, v in self.cfg.items() if k != "rules"},
            islands=sorted(self.blocked) or None,
            teams={f.id: f.team for f in self.fleets.values()}
            if len({f.team for f in self.fleets.values()}) < len(self.fleets)
            else None,
            regions=self.regions or None,
            cellmap=2 if self.regions else None),
            fleets=[dict(id=f.id, name=f.name, harbor=(f.hx, f.hy),
                         model=getattr(f.bot, "model_label", None),
                         designs=dict(f.designs), prompt=None)
                    for f in self.fleets.values()],
            nodes=[dict(id=n.id, name=n.name, x=n.x, y=n.y, kind=n.kind, cap=n.cap)
                   for n in self.nodes.values()],
            run=self.provenance,
            max_ticks=self.max_ticks)

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
            tw = max(tscores, key=lambda tm: (talive[tm], tscores[tm], -tm))
            out["teams"] = teams
            out["team_scores"] = tscores
            # winner (a fleet id, for every existing pipeline) = the winning
            # team's best member
            out["winner"] = max((f.id for f in self.fleets.values() if f.team == tw),
                                key=lambda k: (self.fleets[k].alive, scores[k], -k))
        else:
            out["winner"] = max(scores,
                                key=lambda k: (self.fleets[k].alive, scores[k], -k))
        return out

    def replay(self, result):
        # v2: intern the intent strings — 36k intent events carry ~200 distinct
        # phrasings; store them ONCE in meta.intern and reference by index. Big
        # cut to the parsed browser object graph. self.events is never mutated;
        # only the intent dicts are shallow-copied with s -> index.
        # v3: the frame stream itself is encoded (sim/replay_codec.py) —
        # serialization only; self.frames stays full rows for checkpoints,
        # determinism hashes, and the live stream.
        import replay_codec
        intern = []
        imap = {}
        ev_out = []
        for e in self.events:
            if e.get("k") == "intent":
                s = e["s"]
                i = imap.get(s)
                if i is None:
                    i = imap[s] = len(intern)
                    intern.append(s)
                ev_out.append({**e, "s": i})
            else:
                ev_out.append(e)
        return dict(
            meta=dict(seed=self.seed, w=self.W, h=self.H, tick_hz=TICKS_PER_SEC,
                      # the CONFIGURED window, not the module constant — the
                      # viewer aligns decisions to frames off this value, and
                      # live_header already stamped it correctly
                      replay_version=3, intern=intern,
                      frame_every=FRAME_EVERY, window=self.cfg["window"],
                      presets={**PRESETS, **self.shared_designs}, harbor_r=self.hr,
                      ship_cost=self.cfg["ship_cost"], scenario=self.scenario,
                      config={k: v for k, v in self.cfg.items() if k != "rules"},
                      islands=sorted(self.blocked) or None,
                      teams={f.id: f.team for f in self.fleets.values()}
                      if len({f.team for f in self.fleets.values()}) < len(self.fleets)
                      else None,
                      regions=self.regions or None,
                      cellmap=2 if self.regions else None),
            fleets=[dict(id=f.id, name=f.name, harbor=(f.hx, f.hy),
                         model=getattr(f.bot, "model_label", None),
                         designs=dict(f.designs),
                         # operator prompts are stamped for review: a lopsided
                         # result with custom prompts is not a model comparison
                         prompt=getattr(f.bot, "custom_prompt", "") or None)
                    for f in self.fleets.values()],
            nodes=[dict(id=n.id, name=n.name, x=n.x, y=n.y, kind=n.kind, cap=n.cap)
                   for n in self.nodes.values()],
            run=self.provenance,
            frames=replay_codec.encode_frames(self.frames),
            events=ev_out, decisions=self.decisions,
            result=result)

    # ---------- durable checkpoints (freeze/thaw, plain JSON) ----------
    # Design: __init__ already rebuilds every STATIC/DERIVED field (resolved
    # cfg, rules text, harbors, world gen, _hostile_to, caches) from
    # (players, seed, scenario) — so a checkpoint records only the MUTABLE
    # runtime state and thaw() overlays it onto a freshly-constructed engine
    # running CURRENT code. Version tolerance falls out: a field this build
    # doesn't record keeps the fresh constructor's default, an unknown field
    # from a newer build is ignored, and game-instance world state (nodes,
    # islands, regions, rules text) is overlaid verbatim so a code change
    # can't shift the map or the prompt mid-game.

    CKPT_VERSION = 1

    def _freeze_ship(self, s):
        return dict(
            id=s.id, fleet=s.fleet, squad=s.squad, x=s.x, y=s.y,
            preset=s.preset, stats=s.stats, hull=s.hull, hull_max=s.hull_max,
            cargo=s.cargo, move_acc=s.move_acc, volley_cd=s.volley_cd,
            attackers=sorted(s.attackers),
            flag_attackers=sorted(s.flag_attackers), wp_i=s.wp_i,
            orders=s.orders, intent=s.intent, node_id=s.node_id,
            haul_home=s.haul_home, recall=s.recall,
            recall_safe=s.recall_safe,
            program=s.program.text if s.program else None, pmem=s.pmem,
            trip_start=s.trip_start, trip_gathered=s.trip_gathered,
            trip_hits=s.trip_hits, trip_far=s.trip_far,
            refit_to=s.refit_to, refit_at=s.refit_at, repair_at=s.repair_at,
            nav_prev=list(s.nav_prev) if s.nav_prev else None,
            moved_t=s.moved_t, gained_t=s.gained_t,
            assault_flag=s.assault_flag)

    def _freeze_fleet(self, f):
        return dict(
            id=f.id, flag_hull=f.flag_hull, cargo=f.cargo, bank=f.bank,
            kills=f.kills, kill_count=f.kill_count, alive=f.alive,
            hx=f.hx, hy=f.hy, team=f.team,
            pending=f.pending,
            pending_reassign={str(k): v for k, v in f.pending_reassign.items()},
            flag_target=list(f.flag_target) if f.flag_target else None,
            flag_acc=f.flag_acc,
            flag_prev=list(f.flag_prev) if f.flag_prev else None,
            build_q=[list(b) for b in f.build_q],
            builds=[list(b) for b in f.builds], build_t=f.build_t,
            yards=f.yards, yard_done_t=f.yard_done_t,
            signal_cd=f.signal_cd,
            node_mem={str(k): list(v) for k, v in f.node_mem.items()},
            contacts={str(k): v for k, v in f.contacts.items()},
            inbox=f.inbox, parley_log=f.parley_log,
            pending_programs={sq: p.text for sq, p in f.pending_programs.items()},
            pending_refits=f.pending_refits, designs=f.designs,
            reports_pending=f.reports_pending, queued_signal=f.queued_signal,
            warnings=f.warnings, territory=f.territory,
            recent_hits=f.recent_hits,
            combat=[[list(k), v] for k, v in f.combat.items()],
            bel_v=getattr(f, "_bel_v", 1))

    def freeze(self):
        """The engine's mutable state as one JSON-able dict. Call only while
        parked at a window boundary with in-flight calls drained (run() pauses
        exactly there). Counterpart: Engine.thaw()."""
        rs = self.rng.getstate()
        return dict(
            ckpt_version=self.CKPT_VERSION,
            seed=self.seed, max_ticks=self.max_ticks, t=self.t,
            cfg={k: v for k, v in self.cfg.items() if k != "rules"},
            rules=self.cfg["rules"], scenario_view=self.scenario,
            rng=[rs[0], list(rs[1]), rs[2]],
            next_ship_id=self.next_ship_id, next_node_id=self.next_node_id,
            provenance=self.provenance,
            pipe_ot=self._pipe_ot,
            outage_streak=self._outage_streak,
            last_snap={str(k): v for k, v in self._last_snap.items()},
            lf=self._lf, le=self._le, ld=self._ld,
            blocked=sorted(list(c) for c in self.blocked),
            island_specs=[list(sp) for sp in self.island_specs],
            island_pool=self._island_pool,     # name pool DRAINS during play
                                               # (new wrecks pop from it)
            regions=self.regions,
            region_owner={str(k): v for k, v in self.region_owner.items()},
            contest={str(k): v for k, v in self._contest.items()},
            nodes=[dict(id=n.id, x=n.x, y=n.y, kind=n.kind,
                        remaining=n.remaining, cap=n.cap,
                        regen_acc=n.regen_acc, name=n.name)
                   for n in self.nodes.values()],
            ships=[self._freeze_ship(s) for s in self.ships.values()],
            fleets=[self._freeze_fleet(f) for f in self.fleets.values()],
            events=self.events, frames=self.frames, decisions=self.decisions)

    @staticmethod
    def _norm_orders(o):
        """Checkpointed orders come back with JSON lists where the engine uses
        tuples (rally), and may lack keys a NEWER build added — start from
        DEFAULT_ORDER so new knobs get their defaults."""
        out = dict(DEFAULT_ORDER)
        out.update(o or {})
        r = out.get("rally")
        if isinstance(r, list):
            out["rally"] = tuple(r)
        return out

    def _thaw_program(self, text, fleet, where):
        """Recompile a checkpointed conn program on the CURRENT parser. A
        program the new grammar rejects is dropped — with the admiral told via
        warnings — rather than crashing the resume (version tolerance)."""
        if not text:
            return None
        try:
            return conn.compile_program(text)
        except Exception as e:
            fleet.warnings.append(
                f"[resume] your {where} conn program no longer compiles after "
                f"an update and was dropped — resubmit it ({str(e)[:120]})")
            return None

    @classmethod
    def thaw(cls, data, players):
        """Rebuild a frozen engine on current code: construct fresh from the
        checkpointed (seed, cfg), then overlay the recorded mutable state.
        players: the same (name, bot) list, rebuilt by the caller (run_config
        rebuilds bots from checkpoint provenance + freeze_state)."""
        eng = cls(players, seed=data["seed"], max_ticks=data.get("max_ticks"),
                  scenario=data.get("cfg"))
        st = data.get("rng")
        if st:
            eng.rng.setstate((st[0], tuple(st[1]), st[2]))
        eng.t = data.get("t", 0)
        eng.next_ship_id = data.get("next_ship_id", eng.next_ship_id)
        eng.next_node_id = data.get("next_node_id", eng.next_node_id)
        eng.provenance = data.get("provenance", eng.provenance)
        eng._pipe_ot = data.get("pipe_ot")
        eng._last_snap = {int(k): v
                          for k, v in data.get("last_snap", {}).items()}
        eng._lf, eng._le, eng._ld = (data.get("lf", 0), data.get("le", 0),
                                     data.get("ld", 0))
        # game-instance world state, verbatim (map gen may have changed since)
        if "rules" in data:
            eng.cfg["rules"] = data["rules"]
        if "scenario_view" in data:
            eng.scenario = data["scenario_view"]
        if "blocked" in data:
            eng.blocked = {tuple(c) for c in data["blocked"]}
        if "island_specs" in data:
            eng.island_specs = [tuple(sp) for sp in data["island_specs"]]
        if "island_pool" in data:
            eng._island_pool = list(data["island_pool"])
        eng._outage_streak = int(data.get("outage_streak", 0))
        eng._outage_eval_t = eng.t         # the pre-pause window is spent
        if "regions" in data:
            eng.regions = data["regions"] or []
            eng.region_owner = {int(k): v
                                for k, v in data.get("region_owner",
                                                     {}).items()}
            eng._contest = {int(k): list(v)
                            for k, v in data.get("contest", {}).items()}
            # SAME tiebreak as _gen_regions (cheb, euclid², id) — a thawed
            # run must land on the identical cell map or resume diverges
            eng._cellregion = [[min(eng.regions, key=lambda r:
                                    (cheb(cx, cy, r["x"], r["y"]),
                                     (cx - r["x"]) ** 2 + (cy - r["y"]) ** 2,
                                     r["id"]))["id"]
                                for cy in range(eng.H)]
                               for cx in range(eng.W)] if eng.regions else None
        if "nodes" in data:
            eng.nodes = {}
            for nd in data["nodes"]:
                n = Node(nd["id"], nd["x"], nd["y"], nd.get("kind", "fish"),
                         nd.get("remaining", 0), nd.get("cap", 0),
                         nd.get("name", ""))
                n.regen_acc = nd.get("regen_acc", 0)
                eng.nodes[n.id] = n
        for fd in data.get("fleets", []):
            f = eng.fleets.get(fd.get("id"))
            if f is None:
                continue
            for k in ("flag_hull", "cargo", "bank", "kills", "kill_count",
                      "alive", "hx", "hy", "team", "flag_acc", "build_t",
                      "yards", "yard_done_t",
                      "signal_cd", "pending_refits", "designs",
                      "reports_pending", "queued_signal", "warnings",
                      "territory", "recent_hits", "inbox", "parley_log"):
                if k in fd:
                    setattr(f, k, fd[k])
            f._bel_v = fd.get("bel_v", 1)
            if "pending" in fd:
                f.pending = {sq: cls._norm_orders(o)
                             for sq, o in fd["pending"].items()}
            f.pending_reassign = {int(k): v
                                  for k, v in fd.get("pending_reassign",
                                                     {}).items()}
            ft = fd.get("flag_target")
            f.flag_target = tuple(ft) if ft else None
            fp = fd.get("flag_prev")
            f.flag_prev = tuple(fp) if fp else None
            f.build_q = [tuple(b) for b in fd.get("build_q", [])]
            f.builds = [list(b) for b in fd.get("builds", [])]
            f.node_mem = {int(k): tuple(v)
                          for k, v in fd.get("node_mem", {}).items()}
            f.contacts = {int(k): v for k, v in fd.get("contacts", {}).items()}
            f.pending_programs = {}
            for sq, text in fd.get("pending_programs", {}).items():
                p = eng._thaw_program(text, f, f"squadron {sq} (pending)")
                if p is not None:
                    f.pending_programs[sq] = p
            f.combat = {tuple(k): v for k, v in fd.get("combat", [])}
        eng.ships = {}
        for sd in data.get("ships", []):
            s = Ship(sd["id"], sd["fleet"], sd.get("squad", "A"),
                     sd["x"], sd["y"], sd.get("preset", "trawler"),
                     stats=sd.get("stats"))
            for k in ("hull", "hull_max", "cargo", "move_acc", "volley_cd",
                      "wp_i", "intent", "node_id", "haul_home", "recall",
                      "recall_safe",
                      "trip_start", "trip_gathered", "trip_hits", "trip_far",
                      "refit_to", "refit_at", "repair_at", "moved_t",
                      "gained_t", "assault_flag", "pmem"):
                if k in sd:
                    setattr(s, k, sd[k])
            s.attackers = set(sd.get("attackers", []))
            s.flag_attackers = set(sd.get("flag_attackers", []))
            s.orders = cls._norm_orders(sd.get("orders"))
            np_ = sd.get("nav_prev")
            s.nav_prev = tuple(np_) if np_ else None
            fl = eng.fleets.get(s.fleet)
            s.program = eng._thaw_program(sd.get("program"), fl,
                                          f"ship {s.id}") if fl else None
            eng.ships[s.id] = s
        eng.events = data.get("events", [])
        eng.frames = data.get("frames", [])
        eng.decisions = data.get("decisions", [])
        # derived state that depends on overlaid fields
        eng._hostile_to = {a: frozenset(b for b in eng.fleets
                                        if eng.fleets[b].team
                                        != eng.fleets[a].team)
                           for a in eng.fleets}
        return eng
