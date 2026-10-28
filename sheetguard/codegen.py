"""Phase 8 - Code generation: compile the whole workbook into one Python function.

The generated code is *straight-line*: every formula cell becomes one
assignment to a local variable, emitted in topological order, so a full
recalculation is a single pass with no dictionary lookups, no AST walking and
no dependency bookkeeping at run time.

Three classical back-end optimizations are applied:

* Type-directed specialization - where the type checker proved both operands
  are numbers, ``B2*C2`` compiles to the native float multiply ``(B2 * C2)``
  instead of the spreadsheet-semantics helper ``mul(B2, C2)``. The fast path is
  *speculative*: if a value turns out to be an error or a blank at run time, the
  cell falls back ("deoptimizes") to the always-correct slow path.
* Common-subexpression elimination - an expression such as ``(1 - Discount)``
  that occurs in many cells is computed once into a temporary.
* Dead-input elimination / slicing - inputs that no compiled formula reads are
  never loaded; with ``targets=`` only the backward slice of those cells is
  compiled at all.

Example output::

    def recalc(I):
        Inputs_B3 = I.get('Inputs!B3')
        ...
        # Sales!E2  =D2*(1-Discount)   :: Number[currency]
        try:
            Sales_E2 = (Sales_D2 * _t0)
        except Exception:
            try:
                Sales_E2 = _fin(mul(_v(Sales_D2), sub(1.0, _v(Inputs_B3))))
            except XLError as _e:
                Sales_E2 = _e
"""
from __future__ import annotations

import math
import re
import time
from collections import Counter
from typing import Dict, List, Optional, Tuple

from . import functions
from .functions import arity_ok
from . import runtime as rt
from .ast_nodes import (ArrayLit, BinOp, Bool, CellRef, ErrorLit, FuncCall, Missing, NameRef,
                        Node, Number, Percent, RangeRef, String, UnaryOp)
from .evaluator import evaluation_order, input_value, opaque_cells
from .graph import DependencyGraph, _sort_key
from .refs import make_key
from .symbols import SymbolTable
from .workbook import Workbook

_HELPER = {"+": "add", "-": "sub", "*": "mul", "/": "div", "^": "power", "&": "concat"}
_FAST_FUNCS = {"SUM": "_xsum", "MIN": "min", "MAX": "max", "ABS": "abs"}
_NUMERIC_ROOT_FUNCS = {n for n, s in functions.FUNCTIONS.items()
                       if s.returns in ("num", "unit") and n not in rt.LAZY}


def _ok(x):
    """Fast-path result check: inf / NaN (overflow) take the slow path, which yields #NUM!."""
    if math.isfinite(x):
        return x
    raise OverflowError


def _try(thunk):
    try:
        return thunk()
    except rt.XLError as e:
        return e


def _v(x):
    if isinstance(x, rt.XLError):
        raise x
    return x


def _r(code):
    raise rt.ERRORS.get(code, rt.VALUE)


class _Bad:
    """Sentinel for a CSE temporary whose evaluation failed; any arithmetic on it
    raises TypeError, which sends the using cell down its slow path."""
    __slots__ = ()


_BAD = _Bad()


def _mean(xs):
    return rt.xsum(xs) / len(xs)


RUNTIME_NAMESPACE = {
    "XLError": rt.XLError, "Range": rt.Range, "add": rt.add, "sub": rt.sub, "mul": rt.mul,
    "div": rt.div, "power": rt.power, "concat": rt.concat, "neg": rt.neg, "pos": rt.pos,
    "pct": rt.pct, "compare": rt.compare, "truthy": rt.truthy, "scalar": rt.scalar,
    "call": rt.call, "call_lazy": rt.call_lazy, "_fin": rt.finish, "_v": _v, "_r": _r,
    "_BAD": _BAD, "_try": _try, "_ok": _ok, "_mean": _mean, "_xsum": rt.xsum, "CIRC": rt.CIRC,
}


class CodeGenerator:
    def __init__(self, wb: Workbook, symbols: SymbolTable, graph: DependencyGraph,
                 types: Optional[dict] = None, asts: Optional[Dict[str, Node]] = None,
                 specialize: bool = True, cse: bool = True, targets: Optional[List[str]] = None):
        self.wb, self.symbols, self.graph = wb, symbols, graph
        self.types = types or {}
        self.asts = asts or {}
        self.opaque = opaque_cells(wb)
        self.specialize, self.cse = specialize, cse
        self.targets = targets
        self.names: Dict[str, str] = {}
        self._used = set(RUNTIME_NAMESPACE) | {"I", "np"}
        self.stats = Counter()
        self.source = ""
        self._hoisted: List[str] = []
        self._depth = 0
        self._proven = None

    # ------------------------------------------------------------ naming
    def var(self, key: str) -> str:
        if key not in self.names:
            base = re.sub(r"\W", "_", key.replace("!", "_"))
            if base[0].isdigit():
                base = "_" + base
            name, i = base, 2
            while name in self._used:
                name, i = f"{base}_{i}", i + 1
            self.names[key] = name
            self._used.add(name)
        return self.names[key]

    def ast_of(self, key: str) -> Optional[Node]:
        if key in self.opaque:
            return self.opaque[key]
        if key in self.asts:
            return self.asts[key]
        c = self.wb.cell(key)
        return c.ast if c else None

    # ------------------------------------------------------------ proofs
    def proven_numeric(self, key: str) -> bool:
        """Cells whose value is always a float (or, for formulas, an error)."""
        if self._proven is None:
            self._proven = set()
            for c in self.wb.cells():
                t = self.types.get(c.key)
                if getattr(t, "base", None) != "num":
                    continue
                if not c.is_formula:
                    if isinstance(c.raw, (int, float)) and not isinstance(c.raw, bool):
                        self._proven.add(c.key)
                    continue
                root = self.ast_of(c.key)
                if isinstance(root, (BinOp, UnaryOp, Percent, Number)) and \
                        getattr(root, "op", "+") not in ("&", "=", "<>", "<", ">", "<=", ">="):
                    self._proven.add(c.key)
                elif isinstance(root, FuncCall) and root.name in _NUMERIC_ROOT_FUNCS:
                    self._proven.add(c.key)
        return key in self._proven

    # ------------------------------------------------------------ references
    def _resolve_cell(self, n: Node, sheet: str):
        if isinstance(n, NameRef):
            sym = self.symbols.lookup(n.name, sheet)
            if sym is None:
                return "name", None
            if sym.kind == "constant":
                return "const", sym.target
        r = self.symbols.resolve(n, sheet)
        if r is None:
            return "ref", None
        if isinstance(r, CellRef):
            return "cell", make_key(r.sheet, r.col, r.row)
        return "range", r

    def _range_keys(self, r: RangeRef) -> List[List[str]]:
        c1, c2, r1, r2 = self.wb.sheets[r.sheet].bounds(r)
        return [[make_key(r.sheet, c, row) for c in range(c1, c2 + 1)]
                for row in range(r1, r2 + 1)]

    def _cell_expr(self, key: str) -> str:
        return self.var(key) if self.wb.cell(key) is not None else "None"

    # ------------------------------------------------------------ slow path
    HOIST_EVERY = 40        # Python refuses expressions nested ~200 parentheses deep

    def safe(self, n: Node, sheet: str) -> str:
        """Generic (always-correct) code for n. Very deep trees (=A1+A1+...+A1 with 2,000
        terms) are cut into hoisted temporaries every HOIST_EVERY levels. Temporaries store an
        error instead of raising it, and _v() re-raises it where the value is used, so the
        semantics (including IF / IFERROR laziness) are unchanged."""
        self._depth = getattr(self, "_depth", 0) + 1
        try:
            code = self._safe_node(n, sheet)
        finally:
            self._depth -= 1
        if self._depth and self._depth % self.HOIST_EVERY == 0 and \
                not isinstance(n, (Number, String, Bool, Missing)):
            name = f"_h{len(self._hoisted)}"
            self._used.add(name)
            self._hoisted.append(f"    try:\n        {name} = {code}\n    except XLError as _e:\n"
                                 f"        {name} = _e")
            return f"_v({name})"
        return code

    def _safe_node(self, n: Node, sheet: str) -> str:
        t = type(n)
        if t is Number:
            return repr(float(n.value))
        if t is String:
            return repr(n.value)
        if t is Bool:
            return "True" if n.value else "False"
        if t is ErrorLit:
            return f"_r({n.value!r})"
        if t is Missing:
            return "None"
        if t is ArrayLit:
            return "Range([" + ", ".join("[" + ", ".join(self.safe(x, sheet) for x in row) + "]"
                                         for row in n.rows) + "])"
        if t in (CellRef, RangeRef, NameRef):
            kind, x = self._resolve_cell(n, sheet)
            if kind == "name":
                return "_r('#NAME?')"
            if kind == "ref":
                return "_r('#REF!')"
            if kind == "const":
                return self.safe(x, sheet)
            if kind == "cell":
                return f"_v({self._cell_expr(x)})" if self.wb.cell(x) is not None else "None"
            rows = self._range_keys(x)
            return "Range([" + ", ".join("[" + ", ".join(self._cell_expr(k) for k in row) + "]"
                                         for row in rows) + "])"
        if t is BinOp:
            a, b = self.safe(n.left, sheet), self.safe(n.right, sheet)
            if n.op in _HELPER:
                return f"{_HELPER[n.op]}({a}, {b})"
            return f"compare({n.op!r}, {a}, {b})"
        if t is UnaryOp:
            return f"{'neg' if n.op == '-' else 'pos'}({self.safe(n.operand, sheet)})"
        if t is Percent:
            return f"pct({self.safe(n.operand, sheet)})"
        if t is FuncCall:
            if not arity_ok(n.name, len(n.args)):
                return "_r('#N/A')"
            args = [self.safe(a, sheet) for a in n.args]
            if n.name == "IF":
                yes = args[1] if len(args) > 1 else "False"
                no = args[2] if len(args) > 2 else "False"
                return f"({yes} if truthy(scalar({args[0]})) else {no})"
            if n.name in rt.LAZY:
                return f"call_lazy({n.name!r}, [" + ", ".join(f"lambda: {a}" for a in args) + "])"
            if n.name not in rt.FUNCS:
                return "_r('#NAME?')"
            if n.name in rt.ERROR_TOLERANT:
                args = [f"_try(lambda: {s})" for s in args]
            if n.name in rt.REF_AGG:
                args = [f"Range([[{s}]])" if type(a) in (CellRef, NameRef) and
                        self._resolve_cell(a, sheet)[0] == "cell" else s
                        for a, s in zip(n.args, args)]
            return f"call({n.name!r}, [{', '.join(args)}])"
        return "_r('#VALUE!')"

    # ------------------------------------------------------------ fast path
    def fast(self, n: Node, sheet: str) -> Optional[Tuple[str, str]]:
        self._fdepth = getattr(self, "_fdepth", 0) + 1
        try:
            return None if self._fdepth > self.HOIST_EVERY else self._fast_node(n, sheet)
        finally:
            self._fdepth -= 1

    def _fast_node(self, n: Node, sheet: str) -> Optional[Tuple[str, str]]:
        """(canonical code, code-with-temporaries) or None if not specializable."""
        t = type(n)
        if t is Number:
            s = repr(float(n.value))
            return s, s
        if t in (CellRef, NameRef):
            kind, x = self._resolve_cell(n, sheet)
            if kind == "const":
                return self.fast(x, sheet)
            if kind == "cell" and self.proven_numeric(x):
                v = self.var(x)
                return v, v
            return None
        if t is BinOp and n.op in "+-*/^":
            a, b = self.fast(n.left, sheet), self.fast(n.right, sheet)
            if a is None or b is None:
                return None
            if n.op == "^":                    # never Python's **: (-8)**(1/3) is complex
                return self._temp(f"power({a[0]}, {b[0]})", f"power({a[1]}, {b[1]})")
            return self._temp(f"({a[0]} {n.op} {b[0]})", f"({a[1]} {n.op} {b[1]})")
        if t is UnaryOp:
            a = self.fast(n.operand, sheet)
            if a is None:
                return None
            return (f"(-{a[0]})", f"(-{a[1]})") if n.op == "-" else a
        if t is Percent:
            a = self.fast(n.operand, sheet)
            return None if a is None else self._temp(f"({a[0]} / 100.0)", f"({a[1]} / 100.0)")
        if t is FuncCall and n.name in _FAST_FUNCS and n.args:
            parts = []
            for arg in n.args:
                if isinstance(arg, RangeRef) or (isinstance(arg, NameRef) and
                                                 self._resolve_cell(arg, sheet)[0] == "range"):
                    kind, r = self._resolve_cell(arg, sheet)
                    if kind != "range":
                        return None
                    keys = [k for row in self._range_keys(r) for k in row]
                    if not keys or not all(self.proven_numeric(k) for k in keys):
                        return None
                    parts.extend((self.var(k), self.var(k)) for k in keys)
                else:
                    a = self.fast(arg, sheet)
                    if a is None:
                        return None
                    parts.append(a)
            if not parts:
                return None
            fn = _FAST_FUNCS[n.name]
            if fn == "abs":
                if len(parts) != 1:
                    return None
                return self._temp(f"abs({parts[0][0]})", f"abs({parts[0][1]})")
            raw = f"{fn}(({', '.join(p[0] for p in parts)},))"
            sub = f"{fn}(({', '.join(p[1] for p in parts)},))"
            return self._temp(raw, sub)
        return None

    def _temp(self, raw: str, sub: str) -> Tuple[str, str]:
        if self._counting:
            self._counts[raw] += 1
            return raw, sub
        if self.cse and self._counts.get(raw, 0) >= 2:
            if raw not in self._temps:
                name = f"_t{len(self._temps)}"
                self._temps[raw] = name
                self._pending.append(
                    f"    try:\n        {name} = {sub}\n    except Exception:\n"
                    f"        {name} = _BAD")
                self.stats["cse_temps"] += 1
            else:
                self.stats["cse_reuses"] += 1
            return raw, self._temps[raw]
        return raw, sub

    # ------------------------------------------------------------ driver
    def generate(self) -> str:
        order = evaluation_order(self.graph)
        cycle = {k for c in self.graph.cycles for k in c}
        if self.targets:
            keep = set(self.targets)
            for t in self.targets:
                keep.update(self.graph.precedents(t))
            order = [k for k in order if k in keep]
        formula_keys = [k for k in order if (c := self.wb.cell(k)) is not None and c.is_formula]

        # pass 1: count canonical subexpressions (for CSE)
        self._counting, self._counts, self._temps = True, Counter(), {}
        if self.specialize and self.cse:
            for k in formula_keys:
                ast = self.ast_of(k)
                if ast is not None and k not in cycle:
                    self.fast(ast, self.wb.cell(k).sheet)
        self._counting = False
        # only non-trivial expressions are worth a temporary
        self._counts = Counter({r: n for r, n in self._counts.items() if n >= 2 and
                                any(op in r for op in (" + ", " - ", " * ", " / ", " ** ", "("))
                                and not re.fullmatch(r"[\w.]+", r)})

        # which inputs are actually read?
        used_inputs = set()
        for k in formula_keys:
            used_inputs.update(p for p in self.graph.preds.get(k, ())
                               if (c := self.wb.cell(p)) is not None and not c.is_formula)
        numeric_inputs = [c.key for c in self.wb.cells() if not c.is_formula and
                          isinstance(c.raw, (int, float)) and not isinstance(c.raw, bool)]
        self.stats["inputs_loaded"] = len(used_inputs)
        self.stats["inputs_skipped"] = len([k for k in numeric_inputs if k not in used_inputs])

        lines = ["def recalc(I):",
                 f"    # generated by SheetGuard codegen from '{self.wb.name}'",
                 f"    # {len(formula_keys)} formula cells, {len(used_inputs)} inputs "
                 f"({self.stats['inputs_skipped']} unused numeric inputs eliminated)"]
        for k in sorted(used_inputs, key=_sort_key):
            lines.append(f"    {self.var(k)} = I.get({k!r})")
        lines.append("")
        level_of = {k: i for i, lvl in enumerate(self.graph.levels) for k in lvl}

        for k in formula_keys:
            c = self.wb.cell(k)
            v = self.var(k)
            ty = self.types.get(k, "")
            lv = level_of.get(k)
            lines.append(f"    # {'[L%d] ' % lv if lv is not None else ''}{k}  {c.raw}"
                         + (f"   :: {ty}" if ty else ""))
            if k in cycle:
                lines.append(f"    {v} = CIRC                     # part of a circular reference")
                self.stats["cycle_cells"] += 1
                continue
            ast = self.ast_of(k)
            if ast is None:
                lines.append(f"    {v} = _r('#VALUE!') if False else XLError('#ERROR!')")
                continue
            if isinstance(ast, Number):
                lines.append(f"    {v} = {float(ast.value)!r}")
                self.stats["constant_cells"] += 1
                continue
            self._hoisted = []
            safe = self.safe(ast, c.sheet)
            lines.extend(self._hoisted)
            self._pending = []
            fast = self.fast(ast, c.sheet) if self.specialize and not self._hoisted else None
            lines.extend(self._pending)
            if fast is not None and fast[1] != safe:
                self.stats["fast_cells"] += 1
                lines.append(f"    try:\n        {v} = {fast[1]}\n"
                             f"        if not -1.8e308 < {v} < 1.8e308: raise OverflowError\n"
                             f"    except Exception:\n"
                             f"        try:\n            {v} = _fin({safe})\n"
                             f"        except XLError as _e:\n            {v} = _e")
            else:
                self.stats["slow_cells"] += 1
                lines.append(f"    try:\n        {v} = _fin({safe})\n"
                             f"    except XLError as _e:\n        {v} = _e")
        out_keys = sorted(used_inputs, key=_sort_key) + formula_keys
        lines.append("")
        lines.append("    return {" + ", ".join(f"{k!r}: {self.var(k)}" for k in out_keys) + "}")
        self.stats["formula_cells"] = len(formula_keys)
        self.stats["lines"] = sum(x.count("\n") + 1 for x in lines)
        self.source = "\n".join(lines) + "\n"
        return self.source

    def compile(self):
        if not self.source:
            self.generate()
        t = time.perf_counter()
        ns = dict(RUNTIME_NAMESPACE)
        code = compile(self.source, f"<sheetguard:{self.wb.name}>", "exec")
        exec(code, ns)
        self.stats["compile_ms"] = round((time.perf_counter() - t) * 1000, 2)
        return ns["recalc"]


def prepare_inputs(wb: Workbook) -> Dict[str, object]:
    return {c.key: input_value(c.raw) for c in wb.cells() if not c.is_formula}


def compile_workbook(wb, symbols, graph, types=None, asts=None, **kw):
    g = CodeGenerator(wb, symbols, graph, types, asts, **kw)
    g.generate()
    return g, g.compile()
