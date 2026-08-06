"""Flotilla sim core — deterministic tick engine.

Determinism contract: integer state only, seeded random.Random, ships iterated in id
order, no wall-clock anywhere. Same seed + same admiral decisions => identical match
(verified by the harness double-run hash).
"""
import random
from concurrent.futures import ThreadPoolExecutor

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
    "trader":  dict(speed=3, hold=5, guns=1, armor=1, hull=1, lookout=1),
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


def cheb(ax, ay, bx, by):
    return max(abs(ax - bx), abs(ay - by))


def sign(v):
    return (v > 0) - (v < 0)


class Ship:
    __slots__ = ("id", "fleet", "squad", "x", "y", "stats", "hull", "hull_max",
                 "cargo", "move_acc", "volley_cd", "attackers", "wp_i", "preset", "orders",
                 "intent", "node_id", "haul_home")

    def __init__(self, sid, fleet, squad, x, y, preset):
        self.id = sid
        self.fleet = fleet
        self.squad = squad
        self.x, self.y = x, y
        self.preset = preset
        self.stats = dict(PRESETS[preset])
        self.hull_max = 20 + self.stats["hull"] * 10
        self.hull = self.hull_max
        self.cargo = 0
        self.move_acc = 0
        self.volley_cd = 0
        self.attackers = set()
        self.wp_i = 0
        self.orders = dict(DEFAULT_ORDER)   # per-ship copy; refreshed in port / by signal
        self.intent = ""                    # human/agent-readable "what am I doing"
        self.node_id = None                 # committed forage target (stops flip-flopping)
        self.haul_home = False              # threatened while laden -> commit to delivery

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
        self.banked_score = 0              # kills etc. (cargo counts via self.cargo? no:)
        self.bank = 0                      # deposited cargo (scoring)
        self.kills = 0
        self.alive = True
        self.pending = {}                  # squad -> the admiral's standing orders;
                                           # ships copy them in port or on a signal hoist
        self.build_q = []                  # list of (preset, squad)
        self.build_t = 0
        self.signal_cd = 0
        self.node_mem = {}                 # node id -> (last seen remaining, tick seen)
        self.inbox = []                    # parley messages, delivered at next window
        self.territory = 0                 # territory-mode control points
        self.recent_hits = 0               # hits taken since last window (bot signal)

    def score(self):
        return self.territory if self.mode == "territory" else self.bank + self.kills


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
    regions=16,              # territory mode: number of control regions
    max_ticks=MAX_TICKS,
    description="Timed match: score = cargo hauled to port + 8/enemy ship sunk + "
                "150/flagship destroyed. Highest score at the bell wins; "
                "losing your flagship eliminates you.")

TERRITORY_TICK = 50          # control update + scoring cadence


class Engine:
    def __init__(self, players, seed, max_ticks=None, scenario=None):
        """players: list of (name, bot) — 2 or 4. Bot: .decide(summary, rng) -> actions."""
        self.rng = random.Random(seed)
        self.seed = seed
        self.cfg = _resolve_cfg(scenario)
        c = self.cfg
        self.W, self.H = c["width"], c["height"]
        self.max_ticks = max_ticks if max_ticks is not None else c["max_ticks"]
        if not c["description"]:
            if c["win"] == "territory":
                c["description"] = (
                    "TERRITORY match: the sea is split into named regions. Hold a region "
                    "by being the only fleet with ships in it; the holder keeps it until "
                    "ALL its ships leave or sink while a rival's ship remains. Score = +1 "
                    f"per held region per {c['territory_tick'] / 10:g}s. Cargo funds "
                    "ships but does NOT score; kills do NOT score. Highest territory "
                    "score at the bell wins; losing your flagship still eliminates you.")
            else:
                c["description"] = (
                    f"Timed match: score = cargo hauled to port + {c['kill_score']}/enemy "
                    f"ship sunk + {c['flag_kill_score']}/flagship destroyed. Highest "
                    "score at the bell wins; losing your flagship eliminates you.")
        c["rules"] = (
            f"map {self.W}x{self.H}; decision window every {c['window'] / 10:g}s; match "
            f"ends t={self.max_ticks}; ships cost {c['ship_cost']} cargo, build in "
            f"{c['build_ticks'] / 10:g}s (queue max 3); signal hoist costs "
            f"{c['signal_cost']} cargo, cooldown {c['signal_cd']} windows; gather 1 "
            f"cargo/{c['gather_period']} ticks; fish shoals regen 1 cargo/"
            f"{c['fish_regen_period'] / 10:g}s; docked repair 2 hull/"
            f"{c['repair_period']} ticks; flagship hull {c['flag_hull']}.")
        self.scenario = dict(win=c["win"], description=c["description"],
                             rules=c["rules"], regions=c["regions"],
                             max_ticks=self.max_ticks)
        self.t = 0
        self.next_ship_id = 1
        self.next_node_id = 1
        self.ships = {}                    # id -> Ship (insertion ordered)
        self.events = []
        self.frames = []
        self.decisions = []
        W2, H2 = self.W, self.H
        harbors = [(10, 10), (W2 - 11, H2 - 11), (W2 - 11, 10), (10, H2 - 11)][: len(players)]
        self.fleets = {}
        for i, (name, bot) in enumerate(players):
            f = Fleet(i, name, bot, *harbors[i])
            f.mode = c["win"]
            f.cargo = c["start_cargo"]
            f.flag_hull = c["flag_hull"]
            f.pending = {
                "A": dict(DEFAULT_ORDER, role="forage", rally=(f.hx, f.hy)),
                "B": dict(DEFAULT_ORDER, role="scout", rally=(self.W // 2, self.H // 2), aggression=0),
                "C": dict(DEFAULT_ORDER, role="guard", rally=(f.hx, f.hy), aggression=2),
            }
            self.fleets[i] = f
        self.nodes = {}
        self._island_pool = list(ISLANDS)
        self.rng.shuffle(self._island_pool)
        self._gen_nodes()
        self.regions = []
        self.region_owner = {}
        self._cellregion = None
        if self.scenario["win"] == "territory":
            self._gen_regions()
        for f in self.fleets.values():
            for n in self.nodes.values():
                f.node_mem[n.id] = (n.remaining, 0)
            self._spawn(f, "trader", "A")
            self._spawn(f, "trader", "A")
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

    def _gen_regions(self):
        pts = []
        tries = 0
        while len(pts) < int(self.cfg['regions']) and tries < 500:
            tries += 1
            x = self.rng.randrange(4, self.W - 4)
            y = self.rng.randrange(4, self.H - 4)
            if any(cheb(x, y, px, py) < 10 for px, py, _ in pts):
                continue
            near = min(self.nodes.values(), key=lambda n: cheb(x, y, n.x, n.y) * 100 + n.id)
            label = f"{near.name} Waters"
            if any(p[2] == label for p in pts):
                label = f"{near.name} Deeps"
            pts.append((x, y, label))
        self.regions = [dict(id=i, x=p[0], y=p[1], name=p[2]) for i, p in enumerate(pts)]
        self.region_owner = {r["id"]: None for r in self.regions}
        self._cellregion = [[min(self.regions, key=lambda r:
                                 (cheb(cx, cy, r["x"], r["y"]), r["id"]))["id"]
                             for cy in range(self.H)] for cx in range(self.W)]

    def _territory_tick(self):
        present = {r["id"]: {} for r in self.regions}
        for s in self.ships.values():
            rid = self._cellregion[s.x][s.y]
            present[rid][s.fleet] = present[rid].get(s.fleet, 0) + 1
        for r in self.regions:
            rid = r["id"]
            owner = self.region_owner[rid]
            occ = present[rid]
            if owner is not None and occ.get(owner, 0) > 0:
                pass                                    # holder on station: no flip
            else:
                rivals = {f: c for f, c in occ.items() if c > 0 and f != owner}
                if rivals:
                    top = max(rivals.values())
                    best = sorted(f for f, c in rivals.items() if c == top)
                    if len(best) == 1:                  # clear strongest presence takes it
                        self.region_owner[rid] = best[0]
                        self._ev("region", region=rid, name=r["name"], fleet=best[0],
                                 prev=owner)
            owner = self.region_owner[rid]
            if owner is not None and self.fleets[owner].alive:
                self.fleets[owner].territory += 1

    def _add_node(self, x, y, kind, amount, cap):
        name = self._island_pool.pop() if self._island_pool \
            else f"Islet {self.next_node_id}"
        n = Node(self.next_node_id, x, y, kind, amount, cap, name)
        self.next_node_id += 1
        self.nodes[n.id] = n
        return n

    def _spawn(self, fleet, preset, squad):
        s = Ship(self.next_ship_id, fleet.id, squad, fleet.hx, fleet.hy, preset)
        self.next_ship_id += 1
        self.ships[s.id] = s
        self._ev("spawn", fleet=fleet.id, ship=s.id, preset=preset, squad=squad)
        return s

    # ---------- events / frames ----------
    def _ev(self, kind, **kw):
        kw["t"] = self.t
        kw["k"] = kind
        self.events.append(kw)

    def _frame(self):
        self.frames.append({
            "t": self.t,
            "s": [[s.id, s.fleet, s.x, s.y, (s.hull * 100) // s.hull_max, s.cargo]
                  for s in self.ships.values()],
            "n": [[n.id, n.remaining] for n in self.nodes.values()],
            "f": [[f.id, f.flag_hull, f.cargo, f.bank, f.score(), 1 if f.alive else 0]
                  for f in self.fleets.values()],
            **({"r": [[r["id"], self.region_owner[r["id"]]
                       if self.region_owner[r["id"]] is not None else -1]
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
        for s in self.ships.values():
            if s.fleet == fleet.id:
                continue
            if self._fleet_sees(fleet, s.x, s.y):
                enemies.append(dict(fleet=s.fleet, admiral=self.fleets[s.fleet].name,
                                    preset=s.preset, x=s.x, y=s.y,
                                    cargo_laden=s.cargo > 0))
        for n in self.nodes.values():
            if self._fleet_sees(fleet, n.x, n.y):
                fleet.node_mem[n.id] = (n.remaining, self.t)
        nodes = [dict(id=n.id, name=n.name, x=n.x, y=n.y, kind=n.kind,
                      believed=self.believed(fleet, n)) for n in self.nodes.values()]
        messages, fleet.inbox = fleet.inbox, []
        region_view = [dict(id=r["id"], name=r["name"], x=r["x"], y=r["y"],
                            owner=(self.fleets[self.region_owner[r["id"]]].name
                                   if self.region_owner[r["id"]] is not None else None))
                       for r in self.regions] if self.regions else None
        out = dict(
            scenario=dict(win=self.scenario["win"],
                          description=self.scenario["description"]),
            t=self.t, window=self.t // self.cfg['window'], messages=messages, you=dict(
                fleet=fleet.id, cargo=fleet.cargo, bank=fleet.bank, kills=fleet.kills,
                flag_hull=fleet.flag_hull, harbor=(fleet.hx, fleet.hy),
                ships=own, builds=len(fleet.build_q), signal_cd=fleet.signal_cd,
                recent_hits=fleet.recent_hits,
                orders={k: dict(v) for k, v in fleet.pending.items()}),
            enemies=enemies, nodes=nodes,
            admirals={f.id: f.name for f in self.fleets.values()},
            scores={f.id: f.score() for f in self.fleets.values()},
            harbors={f.id: (f.hx, f.hy) for f in self.fleets.values() if f.alive},
            alive=[f.id for f in self.fleets.values() if f.alive],
        )
        if region_view is not None:
            out["regions"] = region_view
        return out

    def believed(self, fleet, node):
        """Fleet's belief about a node's stock. Charted knowledge: fish shoals are known
        to regenerate, so belief grows with time-since-last-look (capped). Without this,
        a fully-harvested map sent every fleet idle-blind in port forever (P0.1 bug)."""
        val, seen = fleet.node_mem.get(node.id, (0, 0))
        if node.kind == "fish":
            val = min(node.cap, val + (self.t - seen) // self.cfg["fish_regen_period"])
        return val

    def _fleet_sees(self, fleet, x, y):
        if cheb(x, y, fleet.hx, fleet.hy) <= HARBOR_R + 4:
            return True
        for s in self.ships.values():
            if s.fleet == fleet.id and cheb(x, y, s.x, s.y) <= s.vision:
                return True
        return False

    # ---------- admiral actions ----------
    def _apply_actions(self, fleet, actions):
        if not isinstance(actions, dict):
            return
        for squad, od in (actions.get("orders") or {}).items():
            if not isinstance(od, dict) or od.get("role") not in ROLES \
                    or not isinstance(squad, str) or not squad:
                continue
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
            fleet.pending[squad[:1].upper()] = clean
        for b in (actions.get("build") or [])[:4]:
            preset, squad = b.get("preset"), b.get("squad", "A")
            if preset in PRESETS and fleet.cargo >= self.cfg['ship_cost'] and len(fleet.build_q) < 3:
                fleet.cargo -= self.cfg['ship_cost']
                fleet.build_q.append((preset, squad))
        sent = 0
        for pm in (actions.get("parley") or []):
            if sent >= 2 or not isinstance(pm, dict):
                continue
            text = str(pm.get("text", "")).replace("\n", " ").strip()[:280]
            if not text:
                continue
            to = pm.get("to")
            targets = [f for f in self.fleets.values()
                       if f.alive and f.id != fleet.id
                       and (to == "all" or to == f.id or to == f.name)]
            if not targets:
                continue
            for tf in targets:
                tf.inbox.append(dict(sender=fleet.id, text=text))
            self._ev("parley", fleet=fleet.id,
                     to="all" if to == "all" else targets[0].id, text=text)
            sent += 1
        if actions.get("signal") and fleet.signal_cd == 0 and fleet.cargo >= self.cfg['signal_cost']:
            fleet.cargo -= self.cfg['signal_cost']
            fleet.signal_cd = self.cfg['signal_cd']
            for s in self.ships.values():        # signal reaches every ship AT SEA
                if s.fleet == fleet.id and s.squad in fleet.pending \
                        and s.orders != fleet.pending[s.squad]:
                    s.orders = dict(fleet.pending[s.squad])
                    self._ev("orders", ship=s.id, fleet=fleet.id, via="signal",
                             od=self._od_compact(s.orders))
            self._ev("signal", fleet=fleet.id)
        rec = dict(t=self.t, fleet=fleet.id,
                   thoughts=str(actions.get("thoughts", ""))[:400])
        if isinstance(actions.get("_usage"), dict):
            rec["u"] = actions["_usage"]
        self.decisions.append(rec)

    # ---------- per-ship behavior ----------
    def _intent(self, ship, s):
        if s != ship.intent:
            ship.intent = s
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
        for s in self.ships.values():
            if s.fleet == ship.fleet:
                continue
            d = cheb(ship.x, ship.y, s.x, s.y)
            if d < bd and d <= rng_:
                best, bd = s, d
        return best, bd

    def _pick_node(self, ship):
        f = self.fleets[ship.fleet]
        best, bkey = None, None
        for n in self.nodes.values():
            believed = self.believed(f, n)
            if believed <= 0:
                continue
            key = (cheb(ship.x, ship.y, n.x, n.y) * 10 - min(believed, 30), n.id)
            if bkey is None or key < bkey:
                best, bkey = n, key
        return best

    def _ship_target(self, ship):
        """Returns (tx, ty) or None (hold position). Sets ship.intent — the recorded
        'what am I doing and why' that replay review (human or agent) reads."""
        f = self.fleets[ship.fleet]
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
            covered = any(s2.fleet == ship.fleet and s2.id != ship.id
                          and s2.power() >= enemy.power()
                          and cheb(s2.x, s2.y, enemy.x, enemy.y) <= 4
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

    def _move(self, ship, tx, ty):
        ship.move_acc += ship.stats["speed"]
        while ship.move_acc >= MOVE_THRESH:
            ship.move_acc -= MOVE_THRESH
            if (ship.x, ship.y) == (tx, ty):
                break
            ship.x = max(0, min(self.W - 1, ship.x + sign(tx - ship.x)))
            ship.y = max(0, min(self.H - 1, ship.y + sign(ty - ship.y)))

    # ---------- tick ----------
    def tick(self):
        t = self.t
        # decision windows: all admirals see the same tick's state; LLM calls run
        # concurrently (lockstep), results apply in fleet-id order (deterministic)
        if t % self.cfg['window'] == 0:
            live = [f for f in self.fleets.values() if f.alive]
            for f in live:
                if f.signal_cd > 0:
                    f.signal_cd -= 1
            jobs = []
            for f in live:
                summary = self.summary_for(f)
                bot_rng = random.Random((self.seed << 8) ^ (f.id << 4) ^ (t // self.cfg['window']))
                jobs.append((f, summary, bot_rng))

            def _decide(job):
                f, summary, bot_rng = job
                try:
                    return f.bot.decide(summary, bot_rng)
                except Exception as e:                  # a broken admiral never crashes the sim
                    return dict(thoughts=f"(admiral error: {e})")

            if len(jobs) > 1:
                with ThreadPoolExecutor(max_workers=len(jobs)) as ex:
                    results = list(ex.map(_decide, jobs))
            else:
                results = [_decide(j) for j in jobs]
            for (f, _, _), actions in zip(jobs, results):
                self._apply_actions(f, actions)
                f.recent_hits = 0
        # builds
        for f in self.fleets.values():
            if not f.alive or not f.build_q:
                continue
            f.build_t += 1
            if f.build_t >= self.cfg['build_ticks']:
                f.build_t = 0
                preset, squad = f.build_q.pop(0)
                self._spawn(f, preset, squad)
        # ships act (id order = deterministic)
        for s in list(self.ships.values()):
            f = self.fleets[s.fleet]
            docked = cheb(s.x, s.y, f.hx, f.hy) <= HARBOR_R
            if docked:
                if s.cargo:
                    f.cargo += s.cargo
                    f.bank += s.cargo
                    self._ev("deposit", fleet=f.id, ship=s.id, amount=s.cargo)
                    s.cargo = 0
                    s.haul_home = False
                new_od = f.pending.get(s.squad) or DEFAULT_ORDER
                if s.orders != new_od:
                    s.orders = dict(new_od)
                    self._ev("orders", ship=s.id, fleet=f.id, via="port",
                             od=self._od_compact(new_od))
                if s.hull < s.hull_max and t % self.cfg['repair_period'] == 0:
                    s.hull = min(s.hull_max, s.hull + 2)
            tgt = self._ship_target(s)
            if tgt is not None and tgt != (s.x, s.y):
                self._move(s, *tgt)
            else:
                # gathering?
                for n in self.nodes.values():
                    if (n.x, n.y) == (s.x, s.y) and n.remaining > 0 \
                            and s.cargo < s.hold_cap and t % self.cfg['gather_period'] == 0:
                        n.remaining -= 1
                        s.cargo += 1
                        break
        # combat
        for s in list(self.ships.values()):
            if s.volley_cd > 0:
                s.volley_cd -= 1
                continue
            if s.orders["role"] == "assault":      # assault: the flagship IS the target
                hit_flag = False
                for f in self.fleets.values():
                    if f.alive and f.id != s.fleet and cheb(s.x, s.y, f.hx, f.hy) <= 1:
                        f.flag_hull -= max(1, s.stats["guns"] * 2 - 3)
                        f.recent_hits += 1
                        s.volley_cd = self.cfg['volley']
                        hit_flag = True
                        break
                if hit_flag:
                    continue
            enemy, d = self._nearest_enemy(s, 1)
            tgt_fleet = None
            if enemy is not None:
                dmg = max(1, s.stats["guns"] * 2 - enemy.stats["armor"])
                enemy.hull -= dmg
                enemy.attackers.add(s.id)
                self.fleets[enemy.fleet].recent_hits += 1
                s.volley_cd = self.cfg['volley']
            else:
                for f in self.fleets.values():
                    if f.alive and f.id != s.fleet and cheb(s.x, s.y, f.hx, f.hy) <= 1:
                        f.flag_hull -= max(1, s.stats["guns"] * 2 - 3)
                        f.recent_hits += 1
                        s.volley_cd = self.cfg['volley']
                        tgt_fleet = f
                        break
            _ = tgt_fleet
        # flagship guns (range 2)
        for f in self.fleets.values():
            if not f.alive or t % self.cfg['volley'] != 0:
                continue
            best, bd = None, 10 ** 9
            for s in self.ships.values():
                if s.fleet == f.id:
                    continue
                d = cheb(s.x, s.y, f.hx, f.hy)
                if d <= 2 and d < bd:
                    best, bd = s, d
            if best is not None:
                best.hull -= max(1, 5 * 2 - best.stats["armor"])
        # deaths
        for s in [s for s in self.ships.values() if s.hull <= 0]:
            if s.cargo > 0:
                n = self._add_node(s.x, s.y, "wreck", s.cargo, s.cargo)
                for f in self.fleets.values():
                    f.node_mem[n.id] = (s.cargo, self.t) if self._fleet_sees(f, s.x, s.y) \
                        else (0, self.t)
            # credit the sink to the fleet of any attacker still afloat
            by = None
            for aid in sorted(s.attackers):
                a = self.ships.get(aid)
                if a is not None and a.fleet != s.fleet:
                    self.fleets[a.fleet].kills += self.cfg['kill_score']
                    by = a.fleet
                    break
            self._ev("sink", fleet=s.fleet, ship=s.id, x=s.x, y=s.y, preset=s.preset,
                     by=by)
            del self.ships[s.id]
        # flagship deaths
        for f in self.fleets.values():
            if f.alive and f.flag_hull <= 0:
                f.alive = False
                for other in self.fleets.values():
                    if other is not f and other.alive:
                        pass
                # credit: fleet with a ship nearest the harbor gets the bounty
                best, bd = None, 10 ** 9
                for s in self.ships.values():
                    if s.fleet != f.id:
                        d = cheb(s.x, s.y, f.hx, f.hy)
                        if d < bd:
                            best, bd = s, d
                if best is not None:
                    self.fleets[best.fleet].kills += self.cfg['flag_kill_score']
                for s in [s for s in self.ships.values() if s.fleet == f.id]:
                    del self.ships[s.id]
                self._ev("flag_sunk", fleet=f.id)
        if self.regions and t % self.cfg['territory_tick'] == 0:
            self._territory_tick()
        # node regen
        for n in self.nodes.values():
            if n.kind == "fish" and n.remaining < n.cap:
                n.regen_acc += 1
                if n.regen_acc >= self.cfg['fish_regen_period']:
                    n.regen_acc = 0
                    n.remaining += 1
        if t % FRAME_EVERY == 0:
            self._frame()
        self.t += 1

    def run(self):
        while self.t < self.max_ticks and sum(1 for f in self.fleets.values() if f.alive) > 1:
            self.tick()
        self._frame()
        scores = {f.id: f.score() for f in self.fleets.values()}
        alive = [f.id for f in self.fleets.values() if f.alive]
        winner = max(scores, key=lambda k: (self.fleets[k].alive, scores[k], -k))
        return dict(ticks=self.t, scores=scores, alive=alive, winner=winner,
                    names={f.id: f.name for f in self.fleets.values()})

    def replay(self, result):
        return dict(
            meta=dict(seed=self.seed, w=self.W, h=self.H, tick_hz=TICKS_PER_SEC,
                      frame_every=FRAME_EVERY, window=WINDOW, presets=PRESETS, harbor_r=HARBOR_R,
                      ship_cost=self.cfg["ship_cost"], scenario=self.scenario,
                      config={k: v for k, v in self.cfg.items() if k != "rules"},
                      regions=self.regions or None),
            fleets=[dict(id=f.id, name=f.name, harbor=(f.hx, f.hy),
                         model=getattr(f.bot, "model_label", None))
                    for f in self.fleets.values()],
            nodes=[dict(id=n.id, name=n.name, x=n.x, y=n.y, kind=n.kind, cap=n.cap)
                   for n in self.nodes.values()],
            frames=self.frames, events=self.events, decisions=self.decisions,
            result=result)
