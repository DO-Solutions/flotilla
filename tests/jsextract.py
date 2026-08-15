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
    string-masked copy.

    The declaration is located in the RAW source and the mask is then built
    FROM THAT POINT ON, rather than masking the whole file first. The masker is
    a quote/comment scanner with no regex-literal support, so one regex holding
    a quote character — `escHtml`'s `/[&<>"']/g` is the live example — flips its
    quote parity, and from there every string boundary is off by one. Masking
    the whole file made that damage contagious: declarations AFTER such a regex
    silently vanished, and a caller doing `extract(src, "thing")` got None with
    no hint why. Starting the mask at the declaration means a function is only
    affected by its own body.

    A declaration is matched at a line start, which is unambiguous enough here:
    the viewer indents everything nested, so a top-level `function foo(` in
    column 0 is the real one. Callers should still assert that what they asked
    for came back — every suite here does."""
    for pat in (f"\nfunction {name}(", f"\nconst {name} "):
        at = src.find(pat)
        if at < 0:
            continue
        at += 1                                     # past the newline
        masked = _strip_positions(src[at:])         # parity starts fresh here
        if pat.startswith("\nconst"):
            end = masked.find(";")
            if end < 0:
                return None
            return src[at:at + end + 1]
        brace = masked.find("{")
        if brace < 0:
            return None
        depth, i = 0, brace
        while i < len(masked):
            if masked[i] == "{":
                depth += 1
            elif masked[i] == "}":
                depth -= 1
                if depth == 0:
                    return src[at:at + i + 1]
            i += 1
        return None
    return None
