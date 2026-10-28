"""AST-to-AST transformations used by the analysis and fix passes.

* clone()      deep copy of a tree
* shift()      move a formula as if copy-pasted by (dc, dr): relative refs move,
               absolute ($) refs stay - this is exactly what a spreadsheet does
* r1c1()       position-independent canonical form. =B6*C6 in row 6 and =B7*C7
               in row 7 both become  RC[-2]*RC[-1]  - so copied formulas compare
               equal, and a copy-paste error (=B6*C5) stands out as different.
* skeleton()   the formula with every reference replaced by '@' - two formulas
               with the same skeleton differ only in *where* they point.
"""
from __future__ import annotations

import copy

from .ast_nodes import (ArrayLit, BinOp, Bool, CellRef, ErrorLit, FuncCall, Missing, NameRef,
                        Node, Number, Percent, RangeRef, String, UnaryOp)
from .refs import MAX_COLS, MAX_ROWS


def _copy_val(v):
    if isinstance(v, Node):
        return clone(v)
    if isinstance(v, list):
        return [_copy_val(x) for x in v]
    return v                                  # str / int / float / bool / None / tuple


def clone(node: Node) -> Node:
    """Deep copy of a tree (about 10x faster than copy.deepcopy on ASTs)."""
    new = copy.copy(node)                     # shallow: copies every attribute incl. span
    for k, v in vars(node).items():
        if isinstance(v, (Node, list)):
            setattr(new, k, _copy_val(v))
    return new


def shift(node: Node, dc: int, dr: int) -> Node:
    """Return a copy of `node` as it would look after copy-pasting it dc columns
    right and dr rows down. Refs that fall off the grid become #REF!."""
    n = clone(node)

    def fix(x: Node) -> Node:
        if isinstance(x, CellRef):
            c = x.col if x.abs_col else x.col + dc
            r = x.row if x.abs_row else x.row + dr
            if not (1 <= c <= MAX_COLS and 1 <= r <= MAX_ROWS):
                return ErrorLit("#REF!")
            x.col, x.row = c, r
            return x
        if isinstance(x, RangeRef):
            if x.col1 is not None:
                x.col1 = x.col1 if x.abs_col1 else x.col1 + dc
                x.col2 = x.col2 if x.abs_col2 else x.col2 + dc
            if x.row1 is not None:
                x.row1 = x.row1 if x.abs_row1 else x.row1 + dr
                x.row2 = x.row2 if x.abs_row2 else x.row2 + dr
            for v in (x.col1, x.col2):
                if v is not None and not 1 <= v <= MAX_COLS:
                    return ErrorLit("#REF!")
            for v in (x.row1, x.row2):
                if v is not None and not 1 <= v <= MAX_ROWS:
                    return ErrorLit("#REF!")
            return x
        _map_children(x, fix)
        return x

    return fix(n)


def _map_children(x: Node, fn) -> None:
    if isinstance(x, BinOp):
        x.left, x.right = fn(x.left), fn(x.right)
    elif isinstance(x, (UnaryOp, Percent)):
        x.operand = fn(x.operand)
    elif isinstance(x, FuncCall):
        x.args = [fn(a) for a in x.args]
    elif isinstance(x, ArrayLit):
        x.rows = [[fn(v) for v in r] for r in x.rows]


def map_tree(node: Node, fn) -> Node:
    """Bottom-up rewrite: fn is applied to every node after its children."""
    _map_children(node, lambda c: map_tree(c, fn))
    return fn(node)


def _rc(abs_flag: bool, value: int, origin: int, letter: str) -> str:
    if abs_flag:
        return f"{letter}{value}"
    d = value - origin
    return letter if d == 0 else f"{letter}[{d}]"


def r1c1(node: Node, col: int, row: int) -> str:
    """Canonical R1C1 text of `node` relative to the cell (col, row)."""
    def rec(x: Node) -> str:
        if isinstance(x, CellRef):
            p = f"{x.sheet}!" if x.sheet else ""
            return p + _rc(x.abs_row, x.row, row, "R") + _rc(x.abs_col, x.col, col, "C")
        if isinstance(x, RangeRef):
            p = f"{x.sheet}!" if x.sheet else ""
            a = ((_rc(x.abs_row1, x.row1, row, "R") if x.row1 is not None else "") +
                 (_rc(x.abs_col1, x.col1, col, "C") if x.col1 is not None else ""))
            b = ((_rc(x.abs_row2, x.row2, row, "R") if x.row2 is not None else "") +
                 (_rc(x.abs_col2, x.col2, col, "C") if x.col2 is not None else ""))
            return f"{p}{a}:{b}"
        if isinstance(x, BinOp):
            return f"({rec(x.left)}{x.op}{rec(x.right)})"
        if isinstance(x, UnaryOp):
            return f"{x.op}{rec(x.operand)}"
        if isinstance(x, Percent):
            return f"{rec(x.operand)}%"
        if isinstance(x, FuncCall):
            return f"{x.name}(" + ",".join(rec(a) for a in x.args) + ")"
        if isinstance(x, NameRef):
            return x.name.upper()
        return x.unparse()
    return rec(node)


def skeleton(node: Node) -> str:
    """Formula structure with every reference replaced by '@'."""
    def rec(x: Node) -> str:
        if isinstance(x, (CellRef, RangeRef)):
            return "@"
        if isinstance(x, BinOp):
            return f"({rec(x.left)}{x.op}{rec(x.right)})"
        if isinstance(x, UnaryOp):
            return f"{x.op}{rec(x.operand)}"
        if isinstance(x, Percent):
            return f"{rec(x.operand)}%"
        if isinstance(x, FuncCall):
            return f"{x.name}(" + ",".join(rec(a) for a in x.args) + ")"
        if isinstance(x, NameRef):
            return x.name.upper()
        return x.unparse()
    return rec(node)


def is_constant_expr(node: Node) -> bool:
    """True if the subtree contains no references at all (pure literal arithmetic)."""
    return not any(isinstance(n, (CellRef, RangeRef, NameRef)) for n in node.walk()) and \
        not any(isinstance(n, FuncCall) for n in node.walk())


__all__ = ["clone", "shift", "map_tree", "r1c1", "skeleton", "is_constant_expr",
           "Number", "String", "Bool", "Missing"]
