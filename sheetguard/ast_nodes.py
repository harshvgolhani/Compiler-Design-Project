"""Abstract Syntax Tree node classes.

Every node knows how to:
  * list its children          -> used by tree walkers / analysis passes
  * describe itself            -> label() for the ASCII / graphical AST view
  * serialise itself           -> to_dict() for the JSON API & the sidebar
  * turn itself back into text -> unparse(), a tiny *code generator* that
                                  re-emits a formula with minimal parentheses
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterator, List, Optional

from .refs import a1, index_to_col, quote_sheet

# Operator precedence (higher binds tighter) - mirrors Excel / Google Sheets.
PRECEDENCE = {
    "=": 1, "<>": 1, "<": 1, ">": 1, "<=": 1, ">=": 1,
    "&": 2,
    "+": 3, "-": 3,
    "*": 4, "/": 4,
    "^": 5,
}
UNARY_PREC = 6
POSTFIX_PREC = 7
ATOM_PREC = 9


class Node:
    span = (0, 0)          # (start, end) offsets in the source formula
    kind = "Node"

    def at(self, start: int, end: int) -> "Node":
        """Attach a source span (used by the parser) and return self."""
        self.span = (start, end)
        return self

    def children(self) -> List["Node"]:
        return []

    def label(self) -> str:
        return self.kind

    def prec(self) -> int:
        return ATOM_PREC

    def unparse(self) -> str:  # pragma: no cover - overridden
        raise NotImplementedError

    def to_dict(self) -> dict:
        d = {"kind": self.kind, "label": self.label(), "span": list(self.span)}
        kids = self.children()
        if kids:
            d["children"] = [k.to_dict() for k in kids]
        return d

    def walk(self) -> Iterator["Node"]:
        """Pre-order traversal of this subtree."""
        yield self
        for c in self.children():
            yield from c.walk()

    def __str__(self) -> str:
        return "=" + self.unparse()


# ---------------------------------------------------------------- literals
@dataclass(eq=False)
class Number(Node):
    value: float = 0.0
    text: str = ""
    kind = "Number"

    def label(self):
        return f"Number {self.unparse()}"

    def unparse(self):
        if self.text:
            return self.text
        v = self.value
        if float(v).is_integer() and abs(v) < 1e15:
            return str(int(v))
        return repr(float(v))


@dataclass(eq=False)
class String(Node):
    value: str = ""
    kind = "String"

    def label(self):
        return f'String "{self.value}"'

    def unparse(self):
        return '"' + self.value.replace('"', '""') + '"'


@dataclass(eq=False)
class Bool(Node):
    value: bool = False
    kind = "Bool"

    def label(self):
        return f"Bool {self.unparse()}"

    def unparse(self):
        return "TRUE" if self.value else "FALSE"


@dataclass(eq=False)
class ErrorLit(Node):
    value: str = "#N/A"
    kind = "Error"

    def label(self):
        return f"Error {self.value}"

    def unparse(self):
        return self.value


@dataclass(eq=False)
class Missing(Node):
    """An omitted argument, e.g. the middle one in IF(A1,,0)."""
    kind = "Missing"

    def label(self):
        return "(missing argument)"

    def unparse(self):
        return ""


@dataclass(eq=False)
class ArrayLit(Node):
    rows: List[List[Node]] = field(default_factory=list)
    kind = "Array"

    def children(self):
        return [x for r in self.rows for x in r]

    def label(self):
        return f"Array {len(self.rows)}x{len(self.rows[0]) if self.rows else 0}"

    def unparse(self):
        return "{" + ";".join(",".join(x.unparse() for x in r) for r in self.rows) + "}"


# ---------------------------------------------------------------- references
@dataclass(eq=False)
class CellRef(Node):
    col: int = 1
    row: int = 1
    abs_col: bool = False
    abs_row: bool = False
    sheet: Optional[str] = None
    kind = "CellRef"

    def address(self) -> str:
        return a1(self.col, self.row)

    def label(self):
        return f"CellRef {self.unparse()}"

    def unparse(self):
        prefix = quote_sheet(self.sheet) + "!" if self.sheet else ""
        return prefix + a1(self.col, self.row, self.abs_col, self.abs_row)


@dataclass(eq=False)
class RangeRef(Node):
    """A rectangular range. ``row1/row2`` are None for whole columns (A:C),
    ``col1/col2`` are None for whole rows (2:5)."""
    col1: Optional[int] = None
    row1: Optional[int] = None
    col2: Optional[int] = None
    row2: Optional[int] = None
    abs_col1: bool = False
    abs_row1: bool = False
    abs_col2: bool = False
    abs_row2: bool = False
    sheet: Optional[str] = None
    kind = "RangeRef"

    def normalized(self) -> "RangeRef":
        """Return a copy whose corners are ordered (top-left -> bottom-right)."""
        c1, c2 = self.col1, self.col2
        r1, r2 = self.row1, self.row2
        if c1 is not None and c2 is not None and c1 > c2:
            c1, c2 = c2, c1
        if r1 is not None and r2 is not None and r1 > r2:
            r1, r2 = r2, r1
        return RangeRef(c1, r1, c2, r2, self.abs_col1, self.abs_row1, self.abs_col2,
                        self.abs_row2, self.sheet).at(*self.span)

    @property
    def shape(self):
        """(rows, cols) - None for an unbounded dimension."""
        rows = None if self.row1 is None else abs(self.row2 - self.row1) + 1
        cols = None if self.col1 is None else abs(self.col2 - self.col1) + 1
        return rows, cols

    def label(self):
        r, c = self.shape
        dims = f"  [{r if r else '∞'}×{c if c else '∞'}]"
        return f"Range {self.unparse()}{dims}"

    def unparse(self):
        prefix = quote_sheet(self.sheet) + "!" if self.sheet else ""
        if self.row1 is None:   # whole columns
            return (f"{prefix}{'$' if self.abs_col1 else ''}{index_to_col(self.col1)}:"
                    f"{'$' if self.abs_col2 else ''}{index_to_col(self.col2)}")
        if self.col1 is None:   # whole rows
            return (f"{prefix}{'$' if self.abs_row1 else ''}{self.row1}:"
                    f"{'$' if self.abs_row2 else ''}{self.row2}")
        return (prefix + a1(self.col1, self.row1, self.abs_col1, self.abs_row1) + ":" +
                a1(self.col2, self.row2, self.abs_col2, self.abs_row2))


@dataclass(eq=False)
class NameRef(Node):
    """A defined name (named range / named constant) such as ``TaxRate``."""
    name: str = ""
    kind = "NameRef"

    def label(self):
        return f"Name {self.name}"

    def unparse(self):
        return self.name


# ---------------------------------------------------------------- operators
@dataclass(eq=False)
class BinOp(Node):
    op: str = "+"
    left: Node = None
    right: Node = None
    kind = "BinOp"

    def children(self):
        return [self.left, self.right]

    def label(self):
        return f"BinOp {self.op}"

    def prec(self):
        return PRECEDENCE[self.op]

    def unparse(self):
        p = self.prec()
        lt = self.left.unparse()
        rt = self.right.unparse()
        # all binary operators are left-associative in spreadsheet languages
        if self.left.prec() < p:
            lt = f"({lt})"
        if self.right.prec() <= p:
            rt = f"({rt})"
        return f"{lt}{self.op}{rt}"


@dataclass(eq=False)
class UnaryOp(Node):
    op: str = "-"
    operand: Node = None
    kind = "UnaryOp"

    def children(self):
        return [self.operand]

    def label(self):
        return f"Unary {self.op}"

    def prec(self):
        return UNARY_PREC

    def unparse(self):
        inner = self.operand.unparse()
        if self.operand.prec() < UNARY_PREC:
            inner = f"({inner})"
        return f"{self.op}{inner}"


@dataclass(eq=False)
class Percent(Node):
    operand: Node = None
    kind = "Percent"

    def children(self):
        return [self.operand]

    def label(self):
        return "Postfix %"

    def prec(self):
        return POSTFIX_PREC

    def unparse(self):
        inner = self.operand.unparse()
        if self.operand.prec() < POSTFIX_PREC:
            inner = f"({inner})"
        return f"{inner}%"


@dataclass(eq=False)
class FuncCall(Node):
    name: str = ""
    args: List[Node] = field(default_factory=list)
    kind = "FuncCall"

    def children(self):
        return list(self.args)

    def label(self):
        return f"Call {self.name}/{len(self.args)}"

    def unparse(self):
        return f"{self.name}(" + ",".join(a.unparse() for a in self.args) + ")"


REFERENCE_KINDS = (CellRef, RangeRef, NameRef)
