"""Phase 5 - Semantic analysis: type, unit & dimension checking.

Spreadsheets are dynamically, *silently* typed: "$45.00" typed as text, a
percentage added to a price, or a VLOOKUP asking for column 5 of a 3-column
table are all accepted without complaint. SheetGuard infers a static type
for every cell and checks every operator and function call against typing
rules, like the semantic phase of any compiler.

The type lattice
----------------
    Number[unit]   unit ∈ { plain, currency, percent }
    Date           (a serial number, but semantically different)
    Text           (flag numeric_text: text that *looks* like a number)
    Bool, Error, Empty
    Range<T>  rows×cols      (element type T = join of its cells)
    Any            ⊤ - unknown / too dynamic to say

Typing rules for units (an algebra a physicist would recognise):
    currency ± currency  -> currency          currency ± percent   -> ✗ SG302
    currency × percent   -> currency          currency × currency  -> ✗ SG302 (money²)
    currency ÷ currency  -> plain (ratio)     plain ± u            -> u
    date − date          -> plain (days)      date + date          -> ✗ SG302
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, FrozenSet, List, Optional, Tuple, Union

from . import functions
from .ast_nodes import (ArrayLit, BinOp, Bool, CellRef, ErrorLit, FuncCall, Missing, NameRef,
                        Node, Number, Percent, RangeRef, String, UnaryOp)
from .diagnostics import Diagnostic
from .graph import DependencyGraph
from .refs import make_key
from .symbols import SymbolTable
from .transforms import clone
from .workbook import Cell, Workbook


# ------------------------------------------------------------------ types
@dataclass(frozen=True)
class Ty:
    base: str                 # num | text | bool | date | error | empty | any
    unit: str = ""            # for num: '' (plain) | currency | percent | mixed
    numeric_text: bool = False

    def __str__(self) -> str:
        names = {"num": "Number", "text": "Text", "bool": "Bool", "date": "Date",
                 "error": "Error", "empty": "Empty", "any": "Any"}
        s = names[self.base]
        if self.base == "num" and self.unit:
            s += f"[{self.unit}]"
        if self.numeric_text:
            s += "(looks numeric)"
        return s

    @property
    def is_numeric(self) -> bool:
        return self.base in ("num", "date", "empty", "bool")


@dataclass(frozen=True)
class RangeTy:
    elem: Ty
    rows: Optional[int]
    cols: Optional[int]
    units: FrozenSet[str] = frozenset()
    text_cells: Tuple[str, ...] = ()          # keys of numeric-looking text cells inside
    sheet: str = ""

    def __str__(self) -> str:
        r = self.rows if self.rows else "∞"
        c = self.cols if self.cols else "∞"
        return f"Range<{self.elem}> {r}×{c}"


T = Union[Ty, RangeTy]
NUM = Ty("num")
CUR = Ty("num", "currency")
PCT = Ty("num", "percent")
TEXT = Ty("text")
BOOL = Ty("bool")
DATE = Ty("date")
ERR = Ty("error")
EMPTY = Ty("empty")
ANY = Ty("any")

_CURRENCY_RE = re.compile(r"[$€£₹¥]|\[\$|currency", re.I)
_DATE_RE = re.compile(r"(yy|dd|mmm|m/d|d/m|mm/|/mm|h:mm)", re.I)
_NUMERIC_TEXT_RE = re.compile(r"^\s*[-+(]?\s*[$€£₹¥]?\s*[-+]?\d[\d,]*(\.\d+)?\s*%?\)?\s*$")


def type_of_format(fmt: str) -> Optional[Ty]:
    if not fmt or fmt.lower() == "general":
        return None
    if _DATE_RE.search(fmt) and not _CURRENCY_RE.search(fmt):
        return DATE
    if "%" in fmt:
        return PCT
    if _CURRENCY_RE.search(fmt):
        return CUR
    return None


def parse_numeric_text(s: str) -> Optional[float]:
    """'$1,200.50' -> 1200.5 ; '12%' -> 0.12 ; 'abc' -> None"""
    if not isinstance(s, str) or not _NUMERIC_TEXT_RE.match(s):
        return None
    t = s.strip()
    neg = t.startswith("(") and t.endswith(")")
    pct = t.endswith("%") or t.endswith("%)")
    t = re.sub(r"[()$€£₹¥,%\s]", "", t)
    try:
        v = float(t)
    except ValueError:
        return None
    v = -v if neg else v
    return v / 100 if pct else v


def type_of_constant(value, fmt: str = "") -> Ty:
    if value is None or value == "":
        return EMPTY
    if isinstance(value, bool):
        return BOOL
    if isinstance(value, (int, float)):
        return type_of_format(fmt) or NUM
    if hasattr(value, "year"):
        return DATE
    if isinstance(value, str):
        if value.startswith("#") and value.upper() in {"#DIV/0!", "#REF!", "#N/A", "#VALUE!",
                                                       "#NAME?", "#NUM!", "#NULL!", "#ERROR!"}:
            return ERR
        if parse_numeric_text(value) is not None:
            return Ty("text", numeric_text=True)
        return TEXT
    return ANY


def join(a: Ty, b: Ty) -> Ty:
    """Least upper bound of two scalar types."""
    if a == b:
        return a
    if a.base == "empty":
        return b
    if b.base == "empty":
        return a
    if a.base == "error":
        return b
    if b.base == "error":
        return a
    if a.base == b.base == "num":
        if not a.unit:
            return b
        if not b.unit:
            return a
        return Ty("num", "mixed")
    if a.base == b.base == "text":
        return TEXT
    return ANY


def scalar(t: T) -> Ty:
    return t.elem if isinstance(t, RangeTy) else t


# ------------------------------------------------------------------ checker
class TypeChecker:
    def __init__(self, wb: Workbook, symbols: SymbolTable, graph: DependencyGraph):
        self.wb, self.symbols, self.graph = wb, symbols, graph
        self.types: Dict[str, T] = {}
        self.diagnostics: List[Diagnostic] = []
        self._seen = set()
        self.cell: Optional[Cell] = None
        self._guarded = 0          # > 0 inside IFERROR(...)'s first argument

    # -- helpers -----------------------------------------------------------
    def diag(self, code, severity, message, explanation="", fix="", suggested=None,
             related=None, cell: Optional[str] = None) -> None:
        key = cell or (self.cell.key if self.cell else None)
        sig = (code, key, message)
        if sig in self._seen:
            return
        self._seen.add(sig)
        self.diagnostics.append(Diagnostic(code, severity, key, message, explanation, fix,
                                           suggested, related or [], phase="types"))

    def cell_type(self, key: str) -> Ty:
        if key in self.types:
            return scalar(self.types[key])
        c = self.wb.cell(key)
        if c is None:
            return EMPTY
        if c.is_formula:
            return ANY            # not yet typed (cycle) - be conservative
        t = type_of_constant(c.raw, c.fmt)
        self.types[key] = t
        return t

    # -- driver ------------------------------------------------------------
    def run(self) -> "TypeChecker":
        for c in self.wb.cells():
            if not c.is_formula:
                self.types[c.key] = type_of_constant(c.raw, c.fmt)
        blocked = {k for comp in self.graph.cycles for k in comp} | set(self.graph.blocked)
        for key in self.graph.order + sorted(blocked):
            c = self.wb.cell(key)
            if c is None or not c.is_formula:
                continue
            if c.ast is None:
                self.types[key] = ERR
                continue
            self.cell = c
            t = ANY if key in blocked else self.infer(c.ast)
            if key not in blocked:
                # an explicit number format on the formula cell refines a plain number
                fmt_t = type_of_format(c.fmt)
                if isinstance(t, Ty) and t.base == "num" and not t.unit and fmt_t is not None \
                        and fmt_t.base == "num":
                    t = fmt_t
            else:
                self.infer(c.ast)       # still check the expression, ignoring the result
            self.types[key] = t
        self.cell = None
        self._numeric_text_cells()
        return self

    def _numeric_text_cells(self) -> None:
        """Numbers stored as text that formulas actually read."""
        for c in self.wb.cells():
            if c.is_formula or not isinstance(c.raw, str):
                continue
            v = parse_numeric_text(c.raw)
            if v is None:
                continue
            users = self.graph.dependents(c.key, transitive=False)
            if not users:
                continue
            raw = int(v) if float(v).is_integer() else v
            self.diag("SG305", "warning", f'number stored as text: "{c.raw}"',
                      explanation=f'{c.address} holds the *text* "{c.raw}", not the number {raw}. '
                                  f"Arithmetic like {users[0].split('!')[1]} may coerce it, but "
                                  f"SUM/AVERAGE/COUNT silently skip text — totals come out wrong "
                                  f"with no error shown.",
                      fix=f"Store it as the number {raw} (and use a currency format for the "
                          f"$ sign).",
                      suggested=str(raw), related=users, cell=c.key)

    # -- inference ---------------------------------------------------------
    def infer(self, n: Node) -> T:
        if isinstance(n, Number):
            return NUM
        if isinstance(n, String):
            return Ty("text", numeric_text=parse_numeric_text(n.value) is not None)
        if isinstance(n, Bool):
            return BOOL
        if isinstance(n, ErrorLit):
            return ERR
        if isinstance(n, Missing):
            return EMPTY
        if isinstance(n, ArrayLit):
            t = EMPTY
            for x in n.children():
                t = join(t, scalar(self.infer(x)))
            return RangeTy(t, len(n.rows), len(n.rows[0]) if n.rows else 0)
        if isinstance(n, Percent):
            inner = scalar(self.infer(n.operand))
            return PCT if inner.base in ("num", "empty") else inner
        if isinstance(n, UnaryOp):
            inner = scalar(self.infer(n.operand))
            self._expect_numeric(inner, n.operand, f"unary {n.op}")
            return inner if inner.base == "num" else NUM
        if isinstance(n, (CellRef, RangeRef, NameRef)):
            return self._ref_type(n)
        if isinstance(n, BinOp):
            return self._binop(n)
        if isinstance(n, FuncCall):
            return self._call(n)
        return ANY

    def _ref_type(self, n: Node) -> T:
        if isinstance(n, NameRef):
            sym = self.symbols.lookup(n.name, self.cell.sheet)
            if sym is None:
                return ERR
            if sym.kind == "constant":
                return self.infer(sym.target)
        ref = self.symbols.resolve(n, self.cell.sheet)
        if ref is None:
            return ERR
        if isinstance(ref, CellRef):
            return self.cell_type(make_key(ref.sheet, ref.col, ref.row))
        elem, units, texts = EMPTY, set(), []
        for key in self.graph.expand(ref):
            t = self.cell_type(key)
            if t.base == "num":
                units.add(t.unit or "plain")
            if t.numeric_text:
                texts.append(key)
            if t.base != "text":              # headers / labels don't decide the element type
                elem = join(elem, t)
        rows, cols = ref.shape
        return RangeTy(elem, rows, cols, frozenset(units), tuple(texts), ref.sheet)

    def _expect_numeric(self, t: Ty, node: Node, context: str) -> None:
        if t.base == "text" and not t.numeric_text and isinstance(node, String):
            self.diag("SG301", "error", f'text "{node.value}" used in {context}',
                      explanation="Arithmetic on text that is not a number produces #VALUE!.",
                      fix="Use a number, or a reference to a numeric cell.")
        elif t.base == "text" and not t.numeric_text:
            self.diag("SG301", "warning", f"{node.unparse()} holds text but is used in {context}",
                      explanation="The referenced cell contains text, so this evaluates to "
                                  "#VALUE! (or silently to 0 inside some functions).",
                      fix="Point the formula at a numeric cell.")

    # -- operators ---------------------------------------------------------
    def _binop(self, n: BinOp) -> T:
        lt, rt = self.infer(n.left), self.infer(n.right)
        a, b = scalar(lt), scalar(rt)
        op = n.op

        if op == "&":
            return TEXT
        if op in ("=", "<>", "<", ">", "<=", ">="):
            if {a.base, b.base} == {"num", "text"} and not (a.numeric_text or b.numeric_text):
                self.diag("SG301", "warning", f"comparing a number with text in "
                          f"{n.unparse()}",
                          explanation="In spreadsheets a number is never equal to text "
                                      '(5 = "5" is FALSE), so this comparison is constant.',
                          fix="Compare like with like - convert with VALUE() or TEXT().")
            return BOOL

        self._expect_numeric(a, n.left, f"'{op}'")
        self._expect_numeric(b, n.right, f"'{op}'")
        if op == "/":
            self._check_div_zero(n.right, b)

        if a.base == "date" or b.base == "date":
            return self._date_arith(n, a, b)
        if a.base not in ("num", "empty", "bool") or b.base not in ("num", "empty", "bool"):
            return NUM if a.base != "error" and b.base != "error" else ERR

        ua, ub = a.unit if a.base == "num" else "", b.unit if b.base == "num" else ""
        if op in ("+", "-"):
            if ua and ub and ua != ub and "mixed" not in (ua, ub):
                money, rate = (n.left, n.right) if ua == "currency" else (n.right, n.left)
                suggestion = None
                if {ua, ub} == {"currency", "percent"} and op == "+":
                    fixed = BinOp("*", clone(money), BinOp("+", Number(1, "1"), clone(rate)))
                    suggestion = self._rewrite(n, fixed)
                self.diag("SG302", "error",
                          f"adding {ua} to {ub}: {n.unparse()}",
                          explanation=f"{n.left.unparse()} is {_article(ua)} and "
                                      f"{n.right.unparse()} is {_article(ub)}. Adding them is like "
                                      f"adding metres to seconds — e.g. $500 + 10% gives $500.10, "
                                      f"not $550.",
                          fix=(f"To grow by the percentage use {money.unparse()}*(1+"
                               f"{rate.unparse()})." if suggestion else
                               "Convert both operands to the same unit first."),
                          suggested=suggestion)
                return Ty("num", "mixed")
            return Ty("num", ua or ub)
        if op == "*":
            if ua == ub == "currency":
                self.diag("SG302", "warning", f"multiplying two currency amounts: {n.unparse()}",
                          explanation="Money × money gives 'currency squared', which has no "
                                      "meaning. One operand is probably meant to be a quantity "
                                      "or a rate.",
                          fix="Check which operand should be a count or a percentage.")
                return Ty("num", "mixed")
            if "currency" in (ua, ub):
                return CUR                     # price × qty, price × rate
            if ua == ub == "percent":
                return PCT                     # 10% × 50% = 5%
            return NUM                         # qty × growth% = qty
        if op == "/":
            if ua and ua == ub:
                return NUM                     # a ratio is unit-less
            if ub == "currency" and ua != "currency":
                return NUM
            return Ty("num", ua)
        if op == "^":
            return NUM
        return NUM

    def _date_arith(self, n: BinOp, a: Ty, b: Ty) -> Ty:
        if n.op == "-" and a.base == b.base == "date":
            return NUM                         # difference of dates = days
        if n.op in ("+", "-") and (a.base == "date") != (b.base == "date"):
            other = b if a.base == "date" else a
            if other.base == "num" and other.unit:
                self.diag("SG302", "warning", f"adding {other.unit} to a date: {n.unparse()}",
                          explanation="Dates can only be shifted by a plain number of days.",
                          fix="Add a number of days, or use EDATE() for months.")
            return DATE
        self.diag("SG302", "warning", f"'{n.op}' is not meaningful on dates: {n.unparse()}",
                  explanation="Dates are stored as day numbers, so this computes a value, "
                              "but the result has no real-world meaning.",
                  fix="Subtract dates to get days, or add days to a date.")
        return NUM

    def _check_div_zero(self, node: Node, t: Ty) -> None:
        if self._guarded:
            return
        if isinstance(node, Number) and node.value == 0:
            self.diag("SG304", "error", "division by the constant 0",
                      explanation="This always evaluates to #DIV/0!.",
                      fix="Divide by a non-zero value, or wrap in IFERROR().")
        elif t.base == "empty" and isinstance(node, (CellRef, NameRef)):
            ref = node.unparse()
            self.diag("SG304", "error", f"divides by {ref}, which is empty",
                      explanation=f"Empty cells count as 0, so this formula shows #DIV/0! until "
                                  f"someone fills in {ref}.",
                      fix=f"Point the divisor at the right cell, or guard it: "
                          f"=IF({ref}=0,0,…) / IFERROR(…).")

    # -- function calls ------------------------------------------------------
    def _call(self, n: FuncCall) -> T:
        sig = functions.lookup(n.name)
        if n.name in ("IFERROR", "IFNA", "ISERROR", "ISERR", "ISNA") and n.args:
            self._guarded += 1                 # errors here are handled by the author
            try:
                first = self.infer(n.args[0])
            finally:
                self._guarded -= 1
            arg_types = [first] + [self.infer(a) for a in n.args[1:]]
        else:
            arg_types = [self.infer(a) for a in n.args]
        if sig is None:
            return ERR
        name = sig.name

        # argument kinds
        for i, (arg, t) in enumerate(zip(n.args, arg_types)):
            kind = sig.param_kind(i)
            st = scalar(t)
            if kind == "num":
                self._expect_numeric(st, arg, f"{name}()")
                if isinstance(t, RangeTy) and t.text_cells and name in (
                        "SUM", "AVERAGE", "MIN", "MAX", "PRODUCT", "MEDIAN", "COUNT"):
                    cells = ", ".join(k.split("!")[1] for k in t.text_cells)
                    self.diag("SG305", "warning",
                              f"{name}() silently ignores numbers stored as text ({cells})",
                              explanation=f"{cells} contain(s) text that looks like a number. "
                                          f"{name} skips text without any error, so the result "
                                          f"is quietly wrong.",
                              fix="Convert those cells to real numbers.", related=list(t.text_cells))
            elif kind == "range" and isinstance(arg, (Number, String, Bool)) and name not in (
                    "SUMPRODUCT",):
                self.diag("SG301", "warning", f"{name}() expects a range for argument {i + 1}",
                          explanation="A literal was given where a cell range is expected.",
                          fix="Pass a range such as A2:A10.")

        self._check_dimensions(n, sig, arg_types)

        # result type
        if sig.returns == "unit":
            idx = {"SUMIF": [2, 0], "AVERAGEIF": [2, 0], "SUMIFS": [0], "MAXIFS": [0],
                   "MINIFS": [0]}.get(name)
            if idx is not None:
                src = next((arg_types[i] for i in idx if i < len(arg_types)
                            and not isinstance(n.args[i], Missing)), None)
                pool = [src] if src is not None else []
            elif sig.max_args is None:
                pool = arg_types
            else:
                pool = arg_types[:1]
            units = set()
            for t in pool:
                if isinstance(t, RangeTy):
                    units |= {u for u in t.units}
                else:
                    st = scalar(t)
                    if st.base == "num":
                        units.add(st.unit or "plain")
            real = units - {"plain"}
            if len(real) > 1 and "mixed" not in real:
                self.diag("SG302", "warning", f"{name}() mixes units: {', '.join(sorted(real))}",
                          explanation="Aggregating values with different units (e.g. dollars and "
                                      "percentages) produces a meaningless number.",
                          fix="Aggregate only cells of the same unit.")
                return Ty("num", "mixed")
            if any(scalar(t).base == "date" for t in pool):
                return DATE if name in ("MIN", "MAX", "MEDIAN") else NUM
            return Ty("num", next(iter(real)) if len(real) == 1 else "")
        if name in ("IF", "IFERROR", "IFNA", "IFS", "SWITCH", "CHOOSE"):
            branches = {"IF": arg_types[1:3], "IFERROR": arg_types[:2], "IFNA": arg_types[:2],
                        "IFS": arg_types[1::2], "CHOOSE": arg_types[1:],
                        "SWITCH": arg_types[2::2] + ([arg_types[-1]] if arg_types and len(arg_types) % 2 == 0
                                                     else [])}[name]
            t = EMPTY
            for b in branches:
                t = join(t, scalar(b))
            return t if t.base != "empty" else ANY
        if name in ("VLOOKUP", "HLOOKUP", "INDEX", "XLOOKUP"):
            pos = {"XLOOKUP": 2, "INDEX": 0}.get(name, 1)
            table = arg_types[pos] if pos < len(arg_types) else ANY
            return scalar(table) if scalar(table).base != "empty" else ANY
        return {"num": NUM, "text": TEXT, "bool": BOOL, "date": DATE}.get(sig.returns, ANY)

    def _check_dimensions(self, n: FuncCall, sig, ts) -> None:
        name = sig.name

        def lit(i):
            a = n.args[i] if i < len(n.args) else None
            return int(a.value) if isinstance(a, Number) else None

        def shape(i):
            return (ts[i].rows, ts[i].cols) if i < len(ts) and isinstance(ts[i], RangeTy) \
                else None

        if name in ("VLOOKUP", "HLOOKUP") and len(n.args) >= 3:
            sh, k = shape(1), lit(2)
            if sh and k is not None:
                width = sh[1] if name == "VLOOKUP" else sh[0]
                what = "columns" if name == "VLOOKUP" else "rows"
                if width and (k > width or k < 1):
                    self.diag("SG303", "error",
                              f"{name} asks for {what[:-1]} {k} of a {width}-{what[:-1]} table",
                              explanation=f"The lookup table {n.args[1].unparse()} only has "
                                          f"{width} {what}, so this returns #REF!.",
                              fix=f"Use an index between 1 and {width}, or widen the table range.")
        if name == "INDEX" and len(n.args) >= 2:
            sh = shape(0)
            for i, dim, label in ((1, 0, "row"), (2, 1, "column")):
                k = lit(i)
                if sh and k is not None and sh[dim] and k > sh[dim]:
                    self.diag("SG303", "error", f"INDEX {label} {k} is outside a "
                              f"{sh[0]}×{sh[1]} range",
                              explanation="Indexing past the end of a range returns #REF!.",
                              fix=f"Use a {label} index ≤ {sh[dim]}.")
        groups = {"SUMPRODUCT": list(range(len(ts))),
                  "SUMIFS": [0] + list(range(1, len(ts), 2)),
                  "COUNTIFS": list(range(0, len(ts), 2)),
                  "AVERAGEIFS": [0] + list(range(1, len(ts), 2)),
                  "MAXIFS": [0] + list(range(1, len(ts), 2)),
                  "MINIFS": [0] + list(range(1, len(ts), 2)),
                  "SUMIF": [0, 2] if len(ts) == 3 else [],
                  "AVERAGEIF": [0, 2] if len(ts) == 3 else []}.get(name)
        if groups:
            shapes = {i: shape(i) for i in groups if shape(i)}
            if len(set(shapes.values())) > 1:
                desc = ", ".join(f"{n.args[i].unparse()} is {s[0]}×{s[1]}"
                                 for i, s in shapes.items())
                sev = "warning" if name in ("SUMIF", "AVERAGEIF") else "error"
                self.diag("SG303", sev, f"{name}() ranges have different shapes",
                          explanation=f"{desc}. The ranges must line up cell-for-cell; "
                                      + ("Sheets silently resizes the second one, so the wrong "
                                         "cells are summed." if sev == "warning"
                                         else "this returns #VALUE!."),
                          fix="Make all ranges cover the same rows and columns.")

    def _rewrite(self, old: Node, new: Node) -> Optional[str]:
        """Return the cell's formula with subtree `old` replaced by `new`."""
        from .transforms import map_tree
        root = clone(self.cell.ast)
        target_span = old.span

        def swap(x):
            if x.span == target_span and type(x) is type(old) and x.unparse() == old.unparse():
                return new
            return x
        return "=" + map_tree(root, swap).unparse()


def _article(unit: str) -> str:
    return {"currency": "a currency amount", "percent": "a percentage",
            "mixed": "a mixed-unit value"}.get(unit, f"a {unit or 'plain'} number")


def type_check(wb: Workbook, symbols: SymbolTable, graph: DependencyGraph) -> TypeChecker:
    return TypeChecker(wb, symbols, graph).run()
