#!/usr/bin/env python3
"""Build a self-contained, spoiler-free series bundle HTML from a series dir.

Importable (server.py's /api/bundle uses build_bundle) or CLI:
  make_bundle.py --series-dir <dir> --name <title> --out <file.html>
"""
import argparse
import json
import os

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def js_embed_safe(s):
    """Neutralize the three sequences that can break out of a <script> block
    when JSON is embedded in HTML: </script>, and the JS line terminators
    U+2028/U+2029. All are valid inside a JSON string as \\u escapes, so the
    parsed value is byte-identical — this is XSS defense, not a format change.
    (Model output — thoughts, parley, conn program text — reaches PUBLIC,
    anonymous showcase pages, so a raw </script> was stored XSS.)"""
    return (s.replace("<", "\\u003c").replace(">", "\\u003e")
            .replace("\u2028", "\\u2028").replace("\u2029", "\\u2029"))


def build_bundle(series_dir, name, out):
    """STREAM the replays' raw bytes into the template instead of parsing and
    re-serializing them — every writer in the tree stores replays as compact
    JSON, so the output is byte-identical while a 5-game 58 MB series drops
    from ~5.4s / ~900 MB peak RSS to ~0.06s / ~60 MB (measured)."""
    s = json.load(open(os.path.join(series_dir, "series.json")))
    tpl = open(os.path.join(HERE, "viewer", "index.html")).read()
    marker = "/*" + "EMBED_REPLAY" + "*/null"
    head, _, tail = tpl.partition(marker)
    with open(out, "w") as fh:
        fh.write(head)
        fh.write('{"series":{"name":' + js_embed_safe(json.dumps(name))
                 + ',"games":[')
        for i, g in enumerate(s["games"]):
            if i:
                fh.write(",")
            fh.write('{"game":' + json.dumps(g["game"]) + ',"replay":')
            with open(os.path.join(series_dir,
                                   os.path.basename(g["file"]))) as gf:
                for chunk in iter(lambda: gf.read(1 << 20), ""):
                    fh.write(js_embed_safe(chunk))
            fh.write("}")
        fh.write("]}}")
        fh.write(tail)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--series-dir", required=True)
    ap.add_argument("--name", required=True)
    ap.add_argument("--out", required=True)
    a = ap.parse_args()
    build_bundle(a.series_dir, a.name, a.out)
    print(f"{a.out}: {os.path.getsize(a.out) // 1048576}MB")


if __name__ == "__main__":
    main()
