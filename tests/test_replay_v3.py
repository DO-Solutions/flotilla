"""Replay v3 codec: encode/decode round-trips BYTE-IDENTICAL full frames across
every mechanic that touches the stream — combat deaths (ships vanish), wreck
creation (nodes appear mid-game), flagship damage + relocation + elimination
(fleet static churn), territory regions. Also reports the size cut."""
import json
import os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "sim"))
from core import Engine
from bots import BOTS
import replay_codec


def roundtrip(scn, seed, players=None):
    eng = Engine(players or [("Red", BOTS["corsair"]), ("Blu", BOTS["corsair"])],
                 seed=seed, scenario=scn)
    r = eng.run()
    rp = eng.replay(r)
    # replay() now emits the encoded stream itself — prove the SHIPPED replay
    # decodes byte-identical to the engine's full in-memory frames
    assert rp["meta"]["replay_version"] == 3, "replay() must stamp v3"
    enc = rp["frames"]
    assert enc is not eng.frames, "replay() must not hand out the live list"
    sf = replay_codec.ship_fleet_map(eng.events)
    dec = replay_codec.decode_frames(enc, sf)
    a = json.dumps(eng.frames, sort_keys=True, separators=(",", ":"))
    b = json.dumps(dec, sort_keys=True, separators=(",", ":"))
    assert a == b, "v3 round-trip diverged"
    e = json.dumps(enc, separators=(",", ":"))
    return len(a), len(e)


def legacy_rows():
    """Pre-relocation replays carry 6-col fleet rows (no hx/hy) — the codec
    must round-trip them byte-identical too (the library migration hit these)."""
    frames = [
        {"t": 0, "s": [[1, 0, 5, 5, 100, 0], [2, 1, 30, 20, 100, 3]],
         "n": [[1, 40], [2, 40]], "f": [[0, 200, 30, 0, 0, 1], [1, 200, 30, 0, 0, 1]]},
        {"t": 2, "s": [[1, 0, 6, 5, 90, 0]],
         "n": [[1, 39], [2, 40]], "f": [[0, 180, 31, 2, 1, 1], [1, 200, 30, 0, 0, 1]]},
        {"t": 4, "s": [[1, 0, 7, 5, 90, 2]],
         "n": [[1, 39], [2, 40]], "f": [[0, 180, 32, 2, 1, 1], [1, 200, 30, 0, 0, 0]]},
    ]
    enc = replay_codec.encode_frames(frames)
    dec = replay_codec.decode_frames(enc, {1: 0, 2: 1})
    a = json.dumps(frames, sort_keys=True, separators=(",", ":"))
    b = json.dumps(dec, sort_keys=True, separators=(",", ":"))
    assert a == b, "legacy 6-col fleet rows diverged"
    assert len(enc[1]["f"][1]) == 4, "unchanged legacy row should encode dynamic"
    assert len(enc[2]["f"][1]) == 6, "alive-flip must re-emit the full 6-col row"
    print("  legacy 6-col fleet rows: round-trip OK")


def main():
    legacy_rows()
    base = {"width": 72, "height": 40, "max_ticks": 3000,
            "role_fallback": True, "warmup": False}
    tot_a = tot_e = 0
    for name, scn, seed in [
        ("combat", base, 21),
        ("territory+islands", {**base, "win": "territory",
                               "island_coverage": 6}, 22),
        ("relocation", {**base, "flag_move": True}, 23),
        ("flag_salvage", {**base, "flag_salvage": 150}, 24),
    ]:
        a, e = roundtrip(scn, seed)
        tot_a += a
        tot_e += e
        print(f"  {name}: {a} -> {e} bytes ({100 * e / a:.0f}%)")
    print(f"PASS test_replay_v3 (frames {tot_a} -> {tot_e} bytes, "
          f"{100 * tot_e / tot_a:.0f}%)")


if __name__ == "__main__":
    main()
