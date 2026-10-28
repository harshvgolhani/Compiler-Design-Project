"""A1-style address helpers shared by every phase.

Columns are 1-based integers internally (A=1, Z=26, AA=27 ... XFD=16384).
Rows are 1-based integers. A *key* is the fully-qualified, canonical
address of a cell, e.g. ``"Sales!B7"`` - it is what the dependency graph
and the symbol table use as an identifier.
"""
from __future__ import annotations

import re
from functools import lru_cache

MAX_COLS = 16384      # XFD - the Excel / Google Sheets column limit
MAX_ROWS = 1048576

_CELL_RE = re.compile(r"^(\$?)([A-Za-z]{1,3})(\$?)([0-9]+)$")
_COL_RE = re.compile(r"^(\$?)([A-Za-z]{1,3})$")


def col_to_index(letters: str) -> int:
    """'A' -> 1, 'Z' -> 26, 'AA' -> 27."""
    n = 0
    for ch in letters.upper():
        n = n * 26 + (ord(ch) - 64)
    return n


def index_to_col(n: int) -> str:
    """1 -> 'A', 27 -> 'AA'."""
    out = []
    while n > 0:
        n, rem = divmod(n - 1, 26)
        out.append(chr(65 + rem))
    return "".join(reversed(out))


@lru_cache(maxsize=1 << 16)
def parse_cell(text: str):
    """Parse 'B7' / '$B$7' -> (col, row, abs_col, abs_row) or None if not a valid cell."""
    m = _CELL_RE.match(text)
    if not m:
        return None
    col = col_to_index(m.group(2))
    row = int(m.group(4))
    if col > MAX_COLS or row < 1 or row > MAX_ROWS:
        return None
    return col, row, bool(m.group(1)), bool(m.group(3))


def parse_col(text: str):
    """Parse 'B' / '$B' -> (col, abs_col) or None."""
    m = _COL_RE.match(text)
    if not m:
        return None
    col = col_to_index(m.group(2))
    if col > MAX_COLS:
        return None
    return col, bool(m.group(1))


def a1(col: int, row: int, abs_col: bool = False, abs_row: bool = False) -> str:
    return f"{'$' if abs_col else ''}{index_to_col(col)}{'$' if abs_row else ''}{row}"


def quote_sheet(name: str) -> str:
    """Quote a sheet name when needed: My Sheet -> 'My Sheet'."""
    if re.match(r"^[A-Za-z_][A-Za-z0-9_.]*$", name) and not parse_cell(name):
        return name
    return "'" + name.replace("'", "''") + "'"


def make_key(sheet: str, col: int, row: int) -> str:
    return f"{sheet}!{a1(col, row)}"


@lru_cache(maxsize=1 << 18)
def split_key(key: str):
    """'Sales!B7' -> ('Sales', 2, 7)."""
    sheet, _, ref = key.rpartition("!")
    col, row, _, _ = parse_cell(ref)
    return sheet, col, row
