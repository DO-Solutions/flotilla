"""Schema machinery — game-agnostic (split Stage 1c, docs/ENGINE_SPLIT.md).

The knob CONTENT is the game's: the schema dict (sections of {key: spec}),
its rename aliases, and the doc header prose. Everything here validates and
renders whatever content it is handed — defaults, bounds clamping, loud
unknown-key rejection, alias mapping, and the two generated docs (markdown +
machine-readable JSON). Spec shape per knob: t (int/float/bool/enum/str),
d (default), lo/hi or opts, doc; optional show_if/labels UI hints ride along
untouched."""


def unalias(aliases, overrides):
    out = {}
    for k, v in (overrides or {}).items():
        nk = aliases.get(k, k)
        if nk in out and nk != k:
            continue                      # the new name was set explicitly: it wins
        out[nk] = v
    return out


def defaults(schema):
    return {k: spec["d"] for sec in schema.values() for k, spec in sec.items()}


def section_resolve(schema, aliases, section, overrides=None):
    """resolve() for ONE schema section (admirals/series/tournament): defaults
    merged with overrides, unknown keys rejected, values clamped to bounds.
    These sections used to bypass validation entirely — series.games=0 (schema
    lo=1) ran to completion and filed an empty green run into the library."""
    spec_map = schema[section]
    cfg = {k: s["d"] for k, s in spec_map.items()}
    for k, v in unalias(aliases, overrides).items():
        if k not in spec_map:
            raise KeyError(f"unknown {section} key '{k}' — see config-schema.json")
        spec = spec_map[k]
        if spec["t"] == "int":
            v = max(spec["lo"], min(spec["hi"], int(v)))
        elif spec["t"] == "float":
            v = max(spec["lo"], min(spec["hi"], float(v)))
        elif spec["t"] == "enum" and v not in spec["opts"]:
            raise ValueError(f"{section} key '{k}' must be one of "
                             f"{spec['opts']}, got {v!r}")
        elif spec["t"] == "bool":
            v = bool(v)
        cfg[k] = v
    return cfg


def resolve(schema, aliases, overrides=None):
    """Merge overrides onto defaults; unknown keys rejected loudly (agents get a
    clear error, not silent misconfiguration). Values are clamped to bounds."""
    cfg = defaults(schema)
    known = set(cfg)
    for k, v in unalias(aliases, overrides).items():
        if k in ("rules",):
            continue
        if k not in known:
            raise KeyError(f"unknown config key '{k}' — see config-schema.json")
        spec = next(s[k] for s in schema.values() if k in s)
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


def schema_json(schema):
    import json
    return json.dumps({sec: {k: {kk: vv for kk, vv in spec.items()}
                             for k, spec in knobs.items()}
                       for sec, knobs in schema.items()}, indent=1)


def config_md(schema, header):
    """The markdown reference: the game's header prose, then every section as
    a table. The header is content — the engine never writes game copy."""
    out = list(header)
    for sec, knobs in schema.items():
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
