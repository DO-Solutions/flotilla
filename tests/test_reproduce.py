"""Replay provenance: EVERY setting a match ran with — defaults included, all
sections, per-player — is stamped so any install reproduces it exactly."""
import json
import os
import sys
import tempfile
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "sim"))
import run_config
from run_config import bot_provenance, merged_scenario
from llm import LLMAdmiral
from config_schema import SCHEMA, resolve


def main():
    tmp = tempfile.mkdtemp(prefix="flotilla-repro-")
    cfg = {"mode": "series", "seed": 7, "outdir": tmp,
           "bots": ["merchant", "corsair"],
           "scenario": {"width": 64, "height": 36, "max_ticks": 300,
                        "role_fallback": True},
           "admirals": {"memo_chars": 999, "warmup": False, "think": False},
           "series": {"games": 1, "memos": False}}
    cfgpath = os.path.join(tmp, "cfg.json")
    json.dump(cfg, open(cfgpath, "w"))
    argv = sys.argv
    sys.argv = ["run_config.py", cfgpath]
    try:
        run_config.main()
    finally:
        sys.argv = argv
    rp = json.load(open(os.path.join(tmp, "g1.json")))
    conf = rp["meta"]["config"]

    # 1) ALL 72 knobs stamped, at their ACTUAL values (not schema defaults)
    allkeys = {k for sec in SCHEMA.values() for k in sec}
    assert allkeys <= set(conf), f"missing: {allkeys - set(conf)}"
    assert conf["memo_chars"] == 999, "admirals override must reach the stamp"
    assert conf["warmup"] is False and conf["think"] is False
    assert conf["games"] == 1 and conf["memos"] is False, "series section too"
    assert conf["width"] == 64 and conf["role_fallback"] is True

    # 2) warmup:false now actually reaches the ENGINE (was a silent no-op:
    #    admirals-section knob read by core, never previously passed through)
    assert not any(d.get("plan") for d in rp["decisions"]), \
        "warmup off must suppress the planning phase"

    # 3) run provenance: mode/seeds/per-player exact settings
    run = rp["run"]
    assert run["mode"] == "series" and run["base_seed"] == 7
    assert run["game_seed"] == 7
    assert [p["label"] for p in run["players"]] == ["merchant", "corsair"]
    assert all(p["scripted"] for p in run["players"])

    # 4) LLM player provenance carries the full resolved settings
    a = LLMAdmiral("openai-gpt-5.6-sol", label="GPT", temperature=0.7,
                   max_tokens=1234, timeout=77, think=True, memo_chars=500,
                   prompt="be bold", base_prompt="You are a pirate.")
    pv = bot_provenance("GPT", a)
    assert pv["model"] == "openai-gpt-5.6-sol" and pv["label"] == "GPT"
    assert pv["temperature"] == 0.7 and pv["max_tokens"] == 1234
    assert pv["timeout_s"] == 77 and pv["think"] is True
    assert pv["prompt"] == "be bold" and pv["base_prompt"] == "You are a pirate."

    # 5) round trip: split the stamp back into sections -> resolves cleanly and
    #    reproduces the same engine config on a DIFFERENT-defaults install
    engine_secs = {"world", "economy", "combat", "pacing", "scenario"}
    buckets = {"scenario": {}, "admirals": {}, "series": {}, "tournament": {}}
    sec_of = {k: s for s, ks in SCHEMA.items() for k in ks}
    for k, v in conf.items():
        if k in ("rules", "description"):
            continue
        b = "scenario" if sec_of.get(k) in engine_secs else sec_of.get(k)
        if b in buckets:
            buckets[b][k] = v
    cfg2 = {"mode": run["mode"], "seed": run["base_seed"], **buckets}
    re_resolved = resolve(merged_scenario(cfg2))
    for k in allkeys:
        if k == "description":
            continue
        assert re_resolved[k] == conf[k], f"round-trip drift on {k}"
    print("PASS test_reproduce")


if __name__ == "__main__":
    main()
