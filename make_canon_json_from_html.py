#!/usr/bin/env python3
"""
canon_law_parser.py

Parse the Code of Canon Law HTML file into structured JSON, preserving the
full canonical hierarchy (BOOK → PART → SECTION → TITLE → CHAPTER → ARTICLE → canon)
and making every cross-reference between canons machine-readable.

Usage:
    python canon_law_parser.py -i canon-law.html -o canon-law.json
    python canon_law_parser.py -i canon-law.html -o canon-law.json --indent 0

Requirements:
    pip install beautifulsoup4
"""

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

try:
    from bs4 import BeautifulSoup, Tag
except ImportError:
    sys.stderr.write("ERROR: beautifulsoup4 is required.\n"
                     "  pip install beautifulsoup4\n")
    sys.exit(1)


# ---------------------------------------------------------------------------
# Regex patterns
# ---------------------------------------------------------------------------
CANON_HEADER_RE = re.compile(r'^\s*Canon\s+(\d+)\s*$', re.IGNORECASE)
BOOK_HEADER_RE  = re.compile(r'^\s*BOOK\s+([IVXLC]+|\d+)\.?\s*(.*)$', re.IGNORECASE)

# Node-type line in the index: e.g. "TITLE IV. SINGULAR ADMINISTRATIVE ACTS (...)"
NODE_TYPE_RE = re.compile(
    r'^\s*(BOOK|PART|SECTION|TITLE|CHAPTER|ART)\.?\s+'
    r'([IVXLCDM]+|\d+)\s*[.:]?\s*'
    r'(.+?)$',
    re.IGNORECASE,
)

# Canon range in the index, e.g. "(Cann. 7 - 22)" or "(747 - 755)"
CANON_RANGE_RE = re.compile(
    r'\(\s*Cann?\.\s*(\d+)\s*(?:[-\u2013]\s*(\d+))?\s*\)',
    re.IGNORECASE,
)
PLAIN_RANGE_RE = re.compile(r'\(\s*(\d+)\s*[-\u2013]\s*(\d+)\s*\)')

# Cross-reference prefixes inside canon text, e.g. "can.", "cann.", "canon"
CANON_PREFIX_RE = re.compile(r'\b(cann?\.|canons?|canon)\s+', re.IGNORECASE)

# Fallback book ranges (used only if the index cannot be parsed)
KNOWN_BOOK_RANGES = [
    (1,    203,  'I'),
    (204,  746,  'II'),
    (747,  833,  'III'),
    (834,  1253, 'IV'),
    (1254, 1310, 'V'),
    (1311, 1399, 'VI'),
    (1400, 1752, 'VII'),
]
BOOK_ORDER = ['I', 'II', 'III', 'IV', 'V', 'VI', 'VII', 'VIII', 'IX', 'X']


# ---------------------------------------------------------------------------
# Text utilities
# ---------------------------------------------------------------------------
def clean_text(s):
    if not s:
        return ''
    s = s.replace('\u00a0', ' ')
    s = re.sub(r'[ \t\r\f\v]+', ' ', s)
    s = re.sub(r' *\n *', '\n', s)
    s = re.sub(r'\n{3,}', '\n\n', s)
    # Fix common mojibake from double-encoded UTF-8
    s = s.replace('\u00c2\u00a7', '\u00a7')       # Â§ -> §
    s = s.replace('\u00c2\u00a7\u00c2\u00a7', '\u00a7\u00a7')
    return s.strip()


def to_roman(n):
    vals = [(1000, 'M'), (900, 'CM'), (500, 'D'), (400, 'CD'),
            (100, 'C'), (90, 'XC'), (50, 'L'), (40, 'XL'),
            (10, 'X'), (9, 'IX'), (5, 'V'), (4, 'IV'), (1, 'I')]
    out = ''
    for v, s in vals:
        while n >= v:
            out += s
            n -= v
    return out


def known_book_for(num):
    for lo, hi, roman in KNOWN_BOOK_RANGES:
        if lo <= num <= hi:
            return roman
    return None


def parse_range(text):
    """Return [lo, hi] if *text* contains a canon range, else None."""
    m = CANON_RANGE_RE.search(text)
    if m:
        lo = int(m.group(1))
        hi = int(m.group(2)) if m.group(2) else lo
        return [lo, hi]
    m = PLAIN_RANGE_RE.search(text)
    if m:
        return [int(m.group(1)), int(m.group(2))]
    return None


# ---------------------------------------------------------------------------
# Hierarchy parsing (from the index section)
# ---------------------------------------------------------------------------
def parse_li(li):
    """Recursively parse an <li> element (plus any nested <ul>) into a node."""
    text_parts, nested_uls = [], []
    for child in li.children:
        if isinstance(child, Tag) and child.name == 'ul':
            nested_uls.append(child)
        elif isinstance(child, Tag):
            text_parts.append(child.get_text())
        elif isinstance(child, str):
            text_parts.append(child)

    text = clean_text(''.join(text_parts))
    if not text:
        return None

    type_m = NODE_TYPE_RE.match(text)
    if type_m:
        node_type = type_m.group(1).upper()
        if node_type == 'ART':
            node_type = 'ARTICLE'
        number = type_m.group(2)
    else:
        node_type, number = 'OTHER', None

    node = {
        'type':   node_type,
        'number': number,
        'name':   text,
        'range':  parse_range(text),
        'children': [],
    }

    for ul in nested_uls:
        for child_li in ul.find_all('li', recursive=False):
            child = parse_li(child_li)
            if child:
                node['children'].append(child)
    return node


def parse_index(soup):
    """Parse <section id="indexSection"> into a hierarchy tree rooted in ROOT."""
    section = soup.find('section', id='indexSection')
    if section is None:
        return None

    root = {'type': 'ROOT', 'number': None, 'name': '', 'range': None, 'children': []}
    current_book = None

    for child in section.children:
        if not isinstance(child, Tag):
            continue
        if child.name == 'h2':
            continue                       # "Index of the Code of Canon Law"
        if child.name == 'p':
            text = clean_text(child.get_text())
            if not text or text.startswith('Index of'):
                continue
            book_m = BOOK_HEADER_RE.match(text)
            if book_m:
                roman = book_m.group(1).upper()
                if roman.isdigit():
                    roman = to_roman(int(roman))
                rest = re.sub(r'\[PDF version[^\]]*\]', '', book_m.group(2)).strip()
                clean_name = f"BOOK {roman}. {rest}" if rest else f"BOOK {roman}"
                current_book = {
                    'type': 'BOOK', 'number': roman,
                    'name': clean_name, 'range': parse_range(text), 'children': [],
                }
                root['children'].append(current_book)
            elif 'INTRODUCTION' in text.upper():
                root['children'].append({
                    'type': 'INTRODUCTION', 'number': None,
                    'name': text, 'range': None, 'children': [],
                })
        elif child.name == 'ul' and current_book is not None:
            for li in child.find_all('li', recursive=False):
                node = parse_li(li)
                if node:
                    current_book['children'].append(node)

    return root


def find_path(root, canon_num):
    """Return the deepest node (list of node references) whose range contains
    *canon_num*, or [] if none matches."""
    candidates = []

    def walk(node, ancestors):
        path = ancestors + [node]
        if node.get('range') and node['range'][0] <= canon_num <= node['range'][1]:
            candidates.append(path)
        for child in node.get('children', []):
            walk(child, path)

    for child in root.get('children', []):
        walk(child, [])
    if not candidates:
        return []
    candidates.sort(key=len, reverse=True)
    return candidates[0]


def node_to_ref(node):
    """Serialise a hierarchy node as a small reference dict (no children)."""
    return {
        'type':   node.get('type'),
        'number': node.get('number'),
        'name':   node.get('name'),
        'range':  node.get('range'),
    }


def annotate_hierarchy(root, canon_paths):
    """Attach a sorted 'canons' list to each hierarchy node, listing canons
    whose deepest path node is that node."""
    def clear(node):
        node['canons'] = []
        for child in node.get('children', []):
            clear(child)
    clear(root)

    for num, path in canon_paths.items():
        if path:
            path[-1].setdefault('canons', []).append(num)

    def sort_all(node):
        node['canons'] = sorted(node.get('canons', []))
        for child in node.get('children', []):
            sort_all(child)
    sort_all(root)


# ---------------------------------------------------------------------------
# Canon text extraction
# ---------------------------------------------------------------------------
def extract_canon_paragraphs(h2):
    """Return the list of cleaned paragraph strings belonging to the canon
    headed by *h2* — handling both wrapped (<div class="canon">) and bare
    layouts."""
    parent = h2.parent
    paragraphs = []

    if (isinstance(parent, Tag) and parent.name == 'div'
            and 'canon' in (parent.get('class') or [])):
        for sib in h2.next_siblings:
            if not isinstance(sib, Tag):
                continue
            if sib.name == 'h2':
                continue
            txt = clean_text(sib.get_text())
            if txt:
                paragraphs.append(txt)
    else:
        for sib in h2.next_siblings:
            if not isinstance(sib, Tag):
                continue
            if sib.name == 'h2':
                break
            if sib.name == 'div' and 'canon' in (sib.get('class') or []):
                break
            txt = clean_text(sib.get_text())
            if txt:
                paragraphs.append(txt)

    return paragraphs


# ---------------------------------------------------------------------------
# Cross-reference extraction
# ---------------------------------------------------------------------------
def extract_references(text):
    """
    Structured canon references found in *text*.

    Handles::

        "can. 13"                     -> [{"number": 13}]
        "cann. 37 and 51"             -> [{…37…}, {…51…}]
        "cann. 137-144"               -> every canon from 137 to 144
        "can. 76, §1"                 -> [{number: 76, paragraph: "§1"}]
        "cann. 1127, §§1-2"           -> paragraph = "§§1-2"
        "cann. 194, §1, n. 2"         -> … subsection = "n. 2"
    """
    refs = []
    for m in CANON_PREFIX_RE.finditer(text):
        i, n = m.end(), len(text)
        while i < n:
            while i < n and text[i].isspace():
                i += 1
            num_m = re.match(r'(\d+)(?:\s*[-\u2013]\s*(\d+))?', text[i:])
            if not num_m:
                break

            lo = int(num_m.group(1))
            hi = int(num_m.group(2)) if num_m.group(2) else lo
            if hi < lo or hi - lo > 500:
                break

            start = len(refs)
            refs.extend({'number': k} for k in range(lo, hi + 1))
            end = len(refs)
            i += num_m.end()

            para_m = re.match(
                r'\s*,?\s*(§{1,2}\s*\d+(?:\s*[-\u2013]\s*\d+)?)', text[i:])
            if para_m:
                para = para_m.group(1).strip()
                for r in refs[start:end]:
                    r['paragraph'] = para
                i += para_m.end()

                sub_m = re.match(
                    r'\s*,?\s*(n{1,2}\.?\s*\d+(?:\s*[-\u2013]\s*\d+)?)', text[i:])
                if sub_m:
                    sub = sub_m.group(1).strip()
                    for r in refs[start:end]:
                        r['subsection'] = sub
                    i += sub_m.end()

            sep_m = re.match(r'\s*(?:,\s*)?(?:and\s+|&\s*)?', text[i:])
            if sep_m and sep_m.end() > 0:
                j = i + sep_m.end()
                if j < n and re.match(r'\d', text[j:]):
                    i = j
                    continue
            break

    return refs


# ---------------------------------------------------------------------------
# Main parsing
# ---------------------------------------------------------------------------
def compute_stats(canons):
    stats = {'book': 0, 'part': 0, 'section': 0, 'title': 0,
             'chapter': 0, 'article': 0, 'other': 0, 'none': 0}
    for c in canons.values():
        path = c.get('path') or []
        if not path:
            stats['none'] += 1
            continue
        t = path[-1].get('type', '').lower()
        stats[t if t in stats else 'other'] += 1
    return stats


def count_nodes(root):
    if root is None:
        return 0
    n = 1
    for child in root.get('children', []):
        n += count_nodes(child)
    return n


def parse(input_path):
    html = Path(input_path).read_text(encoding='utf-8')
    soup = BeautifulSoup(html, 'html.parser')

    hierarchy_root = parse_index(soup)

    canons = {}
    canon_paths = {}     # canon_number -> list of hierarchy-node references

    for h2 in soup.find_all('h2'):
        text = h2.get_text(strip=True)
        if not text:
            continue
        canon_m = CANON_HEADER_RE.match(text)
        if not canon_m:
            continue

        num = int(canon_m.group(1))
        paragraphs = extract_canon_paragraphs(h2)
        body = '\n\n'.join(paragraphs)

        path_refs = []
        if hierarchy_root is not None:
            path_refs = find_path(hierarchy_root, num)
            if not path_refs:                       # fallback to the book
                book_roman = known_book_for(num)
                if book_roman:
                    for top in hierarchy_root.get('children', []):
                        if (top.get('type') == 'BOOK'
                                and top.get('number') == book_roman):
                            path_refs = [top]
                            break

        canon_paths[num] = path_refs
        canons[str(num)] = {
            'number':     num,
            'path':       [node_to_ref(n) for n in path_refs],
            'text':       body,
            'references': extract_references(body),
        }

    if hierarchy_root is not None:
        annotate_hierarchy(hierarchy_root, canon_paths)
        hierarchy_out = hierarchy_root.get('children', [])
    else:
        hierarchy_out = []

    # Summary "books" array (kept for backwards compatibility)
    books = []
    if hierarchy_root is not None:
        def collect_canons(node):
            out = list(node.get('canons', []))
            for child in node.get('children', []):
                out.extend(collect_canons(child))
            return sorted(out)

        for node in hierarchy_out:
            if node.get('type') == 'BOOK':
                books.append({
                    'roman':         node['number'],
                    'title':         node['name'],
                    'canon_numbers': collect_canons(node),
                })

    stats         = compute_stats(canons)
    total_refs    = sum(len(c['references']) for c in canons.values())

    return {
        'metadata': {
            'title':                'The Code of Canon Law',
            'source':               str(input_path),
            'canon_count':          len(canons),
            'book_count':           len(books),
            'hierarchy_node_count': count_nodes(hierarchy_root),
            'reference_count':      total_refs,
            'parsed_at':            datetime.now(timezone.utc).isoformat(),
        },
        'stats':     stats,
        'books':     books,
        'hierarchy': hierarchy_out,
        'canons':    canons,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(
        description='Parse the Code of Canon Law HTML file into structured JSON.')
    ap.add_argument('-i', '--input',  required=True, help='Input HTML file')
    ap.add_argument('-o', '--output', required=True, help='Output JSON file')
    ap.add_argument('--indent', type=int, default=2,
                    help='JSON indentation (default: 2; use 0 for compact)')
    args = ap.parse_args()

    data = parse(args.input)
    if data['metadata']['canon_count'] == 0:
        sys.stderr.write('WARNING: No canons were parsed. Check the input file.\n')

    if args.indent > 0:
        payload = json.dumps(data, ensure_ascii=False, indent=args.indent)
    else:
        payload = json.dumps(data, ensure_ascii=False, separators=(',', ':'))

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(payload, encoding='utf-8')

    md = data['metadata']
    st = data['stats']
    print(f"Parsed {md['canon_count']} canons, "
          f"{md['hierarchy_node_count']} hierarchy nodes, "
          f"{md['reference_count']} cross-references -> {out_path}")
    print(f"  Canon depth coverage:")
    for k in ('book', 'part', 'section', 'title', 'chapter', 'article', 'other', 'none'):
        if st.get(k):
            print(f"    {k:8s}: {st[k]}")


if __name__ == '__main__':
    main()