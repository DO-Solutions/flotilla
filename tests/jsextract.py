#!/usr/bin/env python3
"""Pull a named top-level declaration out of viewer/index.html.

Shared by every test that treats the viewer's inline JS as a subject rather
than re-implementing it: extracting by NAME means a rename or a shape change
fails the test instead of quietly testing a stale copy. Lives in its own module
so importing it never executes another suite's top-level assertions.
"""


def _strip_positions(src):
    """Index set of offsets that are inside a comment or string literal, so the
    brace matcher never counts a brace that isn't code."""
    masked = list(src)
    i, n = 0, len(src)
    while i < n:
        c = src[i]
        two = src[i:i + 2]
        if two == "//":
            j = src.find("\n", i)
            j = n if j < 0 else j
            for k in range(i, j):
                masked[k] = " "
            i = j
        elif two == "/*":
            j = src.find("*/", i + 2)
            j = n if j < 0 else j + 2
            for k in range(i, j):
                masked[k] = " "
            i = j
        elif c in "\"'`":
            j = i + 1
            while j < n and src[j] != c:
                j += 2 if src[j] == "\\" else 1
            j = min(j + 1, n)
            for k in range(i, j):
                masked[k] = " "
            i = j
        else:
            i += 1
    return "".join(masked)


def extract(src, name):
    """Pull one top-level `function name(...){...}` or `const name = ...;` out of
    the viewer source, by brace/semicolon matching over a comment- and
    string-masked copy."""
    masked = _strip_positions(src)
    for pat in (f"\nfunction {name}(", f"\nconst {name} "):
        at = masked.find(pat)
        if at < 0:
            continue
        at += 1                                     # past the newline
        if pat.startswith("\nconst"):
            end = masked.find(";", at)
            if end < 0:
                return None
            return src[at:end + 1]
        brace = masked.find("{", at)
        if brace < 0:
            return None
        depth, i = 0, brace
        while i < len(masked):
            if masked[i] == "{":
                depth += 1
            elif masked[i] == "}":
                depth -= 1
                if depth == 0:
                    return src[at:i + 1]
            i += 1
        return None
    return None
