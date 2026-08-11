"""Flotilla's Game registration + the runner's compat surface (split Stage 2).

The orchestration (matches, series, memos, tournaments, checkpoints) moved to
engine/runner.py; the CLI entry stays HERE because it is a documented
interface — the server and every aux worker invoke `sim/run_config.py`.
This module's real job is the contract: it assembles Flotilla's Game object
and registers it, which binds the runner to this game's rules, bots, schema,
and fog digest. Imported as `run_config`, it aliases to the runner module so
every existing name keeps working.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import core                                # noqa: E402
import bots                                # noqa: E402
import series                              # noqa: E402
import config_schema                       # noqa: E402
import conn                                # noqa: E402
from engine import contract                # noqa: E402

contract.set_game(contract.Game(
    name="flotilla",
    engine=core.Engine,
    bots=bots.BOTS,
    schema=config_schema,
    digest_for=series.digest_for,
    api_reference=conn.api_reference,      # the ship-language teaching card
    presets=core.PRESETS,                  # built-in classes (designer/API)
    ship_stats=core.SHIP_STATS,            # designer stat names
))

from engine import runner as _runner       # noqa: E402

if __name__ == "__main__":
    _runner.main()
else:
    sys.modules[__name__] = _runner
