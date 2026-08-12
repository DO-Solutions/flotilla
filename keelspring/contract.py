"""The Game contract — what a game provides to the engine (split Stage 2,
docs/ENGINE_SPLIT.md).

The engine is complete on its own: runs, series, tournaments, memos,
checkpoints, live streaming, providers, cost telemetry. A game plugs in by
registering ONE object carrying its rules and vocabulary. Registration is
runtime-validated — a missing piece fails loudly at startup, never as an
AttributeError three subsystems deep.

    from keelspring import contract
    contract.set_game(contract.Game(
        name="flotilla",
        engine=core.Engine,            # class: (players, seed=, max_ticks=,
                                       #   scenario=) -> engine; .run(),
                                       #   .replay(result), .freeze()/.thaw()
        bots=bots.BOTS,                # {name: scripted admiral}
        schema=config_schema,          # module-like: SCHEMA, resolve(),
                                       #   section_resolve(), defaults()
        digest_for=series.digest_for,  # fog-honest replay digest for memos
    ))

Consumers either read `contract.game()` at call time or register a binder
with `contract.on_set(fn)` — binders run immediately if a game is already
registered, so import order never matters. The contract grows with the
split (unit presets, frame codec, UI copy arrive with the server stage);
every addition lands here first, documented and validated.
"""

_REQUIRED = {
    "name": "the game's identifier (str)",
    "engine": "the match engine class/factory",
    "bots": "mapping of scripted-admiral name -> bot",
    "schema": "config-schema module: SCHEMA + resolve/section_resolve/defaults",
    "digest_for": "callable(replay, fleet_id, game_no, total, full_info) -> str",
}

# optional pieces default harmlessly: a game without a ship-program language
# simply has no API card to append to prompts
_OPTIONAL = {"api_reference": None,   # ship-language teaching card
             "presets": {},           # built-in unit classes {name: stats}
             "ship_stats": ()}        # designer stat names, in display order

_game = None
_binders = []


class Game:
    """A plain, validated carrier for everything the game provides."""

    def __init__(self, **provided):
        missing = [k for k in _REQUIRED if k not in provided]
        if missing:
            raise TypeError(
                "incomplete Game registration — missing: "
                + ", ".join(f"{k} ({_REQUIRED[k]})" for k in missing))
        for k in ("resolve", "section_resolve", "defaults", "SCHEMA"):
            if not hasattr(provided["schema"], k):
                raise TypeError(f"Game.schema lacks {k}()")
        if not callable(provided["engine"]):
            raise TypeError("Game.engine must be callable")
        if not callable(provided["digest_for"]):
            raise TypeError("Game.digest_for must be callable")
        for k, dflt in _OPTIONAL.items():
            provided.setdefault(k, dflt)
        self.__dict__.update(provided)


def set_game(g):
    """Register the game and notify every binder (idempotent re-registration
    is allowed: tests rebuild engines freely)."""
    global _game
    if not isinstance(g, Game):
        raise TypeError("set_game() takes a contract.Game")
    _game = g
    for fn in _binders:
        fn(g)
    return g


def game():
    if _game is None:
        raise RuntimeError(
            "no game registered — import the game's entry module first "
            "(Flotilla: `import run_config` or `import llm`), or call "
            "keelspring.contract.set_game() with your own Game")
    return _game


def on_set(fn):
    """Run fn(game) at every registration — and immediately if one exists."""
    _binders.append(fn)
    if _game is not None:
        fn(_game)
    return fn
