#!/usr/bin/env python3
r"""
make_inline_canon.py

Usage:
    python make_inline_canon.py -i canon-law-browser.html -o canon-law-browser-inline.html

Reads an HTML file containing declarations like:

    const CANON_JSON_URL    = 'https://.../canon-law.json';
    const CANON_JSON_INLINE = null;

Fetches each *_JSON_URL (or reads it as a local path), then rewrites the
file so that the matching *_JSON_INLINE constant is populated with the
fetched JSON. The *_URL constants are left untouched.

Works for any HTML that follows the same `<PREFIX>_JSON_URL` /
`<PREFIX>_JSON_INLINE` pattern -- the Canon Law browser, the Catechism
browser, or any future variant -- since all `<PREFIX>_JSON_URL` constants
are discovered automatically.

Highlights:
    - Re-runnable: an already-populated *_JSON_INLINE is replaced cleanly.
    - Safe: backslashes, quotes, and Unicode escapes in the JSON survive
      intact (the const value is spliced in with plain string slicing,
      not re.sub replacement, which would have eaten escapes).
    - Accepts URLs (http/https/file) and local filesystem paths.
    - Fails fast: all fetches complete before any output is written.
"""

import argparse
import json
import re
import sys
import urllib.request
import urllib.error
from pathlib import Path


# ---------------------------------------------------------------------------
# Declaration scanning
# ---------------------------------------------------------------------------
def find_const_span(html, name):
    """
    Locate ``const NAME = <value>;`` and return
    ``(start, end, value_start, value_end)``.

    ``start:end``             spans the whole statement (including the ``;``).
    ``value_start:value_end`` spans just the value.

    Returns ``None`` if the declaration is not found.

    Handles ``null``, quoted strings, arrays, objects, and any mix of
    those, while respecting string literals (single, double, and backtick)
    and balanced brackets -- so a value like ``{"a":"b;c"}`` or a
    pretty-printed JSON object spanning multiple lines is scanned
    correctly.
    """
    m = re.search(r"\bconst\s+" + re.escape(name) + r"\s*=\s*", html)
    if not m:
        return None

    i = m.end()
    n = len(html)
    depth = 0
    in_string = None          # None | "'" | '"' | '`'
    escaped = False

    while i < n:
        c = html[i]

        if in_string is not None:
            if escaped:
                escaped = False
            elif c == '\\':
                escaped = True
            elif c == in_string:
                in_string = None
            i += 1
            continue

        if c in ("'", '"', '`'):
            in_string = c
        elif c in '{[(':
            depth += 1
        elif c in '}])':
            depth -= 1
            if depth < 0:
                return None        # malformed -- bail
        elif c == ';' and depth == 0:
            return (m.start(), i + 1, m.end(), i)

        i += 1

    return None


def find_url_declarations(html):
    """
    Return a list of ``(url_name, inline_name)`` pairs discovered in the
    file. Only names matching ``<PREFIX>_JSON_URL`` are considered; the
    sibling name is derived by replacing ``_URL`` with ``_INLINE``.
    """
    names = []
    for m in re.finditer(r"\bconst\s+(\w+_JSON_URL)\b", html):
        name = m.group(1)
        if name not in names:
            names.append(name)

    return [(u, u[:-len("_URL")] + "_INLINE") for u in names]


def read_const_value(html, name):
    span = find_const_span(html, name)
    if span is None:
        return None
    _, _, v_start, v_end = span
    return html[v_start:v_end].strip()


def source_from_literal(value):
    """
    Turn the raw JS value into a usable source string (URL or path), or
    ``None`` if it isn't a plain string literal we can fetch.
    """
    if value is None:
        return None
    v = value.strip()
    if v == "null" or v == "":
        return None
    if len(v) >= 2 and v[0] in ('"', "'") and v[-1] == v[0]:
        return v[1:-1]
    return None


# ---------------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------------
def fetch_json(source, base_dir):
    """
    Read JSON from a URL or a local path. ``source`` may be:

      - ``http://`` / ``https://`` / ``file://`` URL
      - an absolute filesystem path
      - a path relative to ``base_dir`` (the input HTML's directory)
    """
    if re.match(r"^[a-zA-Z][\w+.-]*://", source):
        print(f"  fetching {source}", file=sys.stderr)
        req = urllib.request.Request(
            source,
            headers={"User-Agent": "make_inline_canon/1.0 (+https://example.invalid)"},
        )
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            sys.exit(f"HTTP error fetching {source}: {e.code} {e.reason}")
        except urllib.error.URLError as e:
            sys.exit(f"Network error fetching {source}: {e.reason}")
        except json.JSONDecodeError as e:
            sys.exit(f"Invalid JSON at {source}: {e}")

    print(f"  reading local file {source}", file=sys.stderr)
    path = Path(source)
    if not path.is_absolute():
        path = base_dir / path
    if not path.is_file():
        sys.exit(f"File not found: {path}")
    try:
        return json.loads(path.read_bytes())
    except json.JSONDecodeError as e:
        sys.exit(f"Invalid JSON at {path}: {e}")


# ---------------------------------------------------------------------------
# Inlining
# ---------------------------------------------------------------------------
def replace_const(html, name, value):
    """
    Replace ``const NAME = <old>;`` with ``const NAME = <value>;``.

    Uses plain string slicing rather than ``re.sub`` so that any
    backslashes in the serialized JSON -- e.g. escaped quotes or Unicode
    escapes -- are written through verbatim.
    """
    span = find_const_span(html, name)
    if span is None:
        raise RuntimeError(f"Could not find declaration for {name}")

    start, end, _, _ = span
    serialized = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    replacement = f"const {name} = {serialized};"
    return html[:start] + replacement + html[end:]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("-i", "--input",  required=True, help="Input HTML file")
    ap.add_argument("-o", "--output", required=True, help="Output HTML file")
    args = ap.parse_args()

    src = Path(args.input)
    if not src.is_file():
        sys.exit(f"Input file not found: {src}")

    html = src.read_text(encoding="utf-8")
    base_dir = src.parent

    pairs = find_url_declarations(html)
    if not pairs:
        sys.exit("No `*_JSON_URL` declarations found in input HTML.")

    print(f"Found {len(pairs)} URL declaration(s):", file=sys.stderr)
    for url_name, inline_name in pairs:
        print(f"  {url_name}  ->  {inline_name}", file=sys.stderr)

    # Fetch everything first, so failures happen before any output.
    fetched = {}
    for url_name, inline_name in pairs:
        source = source_from_literal(read_const_value(html, url_name))
        if not source:
            print(f"  {url_name} is null/empty -- skipping", file=sys.stderr)
            continue
        print(f"-> {url_name} = {source}", file=sys.stderr)
        fetched[inline_name] = fetch_json(source, base_dir)

    if not fetched:
        sys.exit("Nothing to inline (no URLs or paths could be resolved).")

    # Inline
    out_html = html
    for inline_name, payload in fetched.items():
        if find_const_span(out_html, inline_name) is None:
            # The matching *_INLINE declaration is absent -- add one just
            # after the *_URL it belongs to.
            url_name = inline_name[:-len("_INLINE")] + "_URL"
            url_span = find_const_span(out_html, url_name)
            if url_span is None:
                sys.exit(f"Cannot place {inline_name}: no anchor found")
            print(f"  warning: no `const {inline_name}` found; inserting one",
                  file=sys.stderr)
            out_html = (out_html[:url_span[1]]
                        + f"\nconst {inline_name} = null;"
                        + out_html[url_span[1]:])

        size = len(json.dumps(payload))
        print(f"-> inlining {inline_name} ({size:,} bytes)", file=sys.stderr)
        out_html = replace_const(out_html, inline_name, payload)

    dst = Path(args.output)
    dst.write_text(out_html, encoding="utf-8")

    out_bytes = len(out_html.encode("utf-8"))
    print(f"Wrote {dst} ({out_bytes:,} bytes / "
          f"{out_bytes / 1024 / 1024:.2f} MiB)", file=sys.stderr)
    if out_bytes > 20 * 1024 * 1024:
        print("  note: output exceeds 20 MiB -- some browsers may be slow "
              "to load such a large inline payload.", file=sys.stderr)


if __name__ == "__main__":
    main()