"""Execution engine #1: a tree-walking interpreter over the AST.

Cells are evaluated in the topological order computed by the dependency
graph, so every precedent is ready before its dependents - no recursion,
no re-evaluation. Cells inside a cycle get #CIRC!, and the error propagates
to everything downstream, exactly like a real spreadsheet.

Two modes are provided for benchmarking:
  * Interpreter(reparse=False)   parse once, walk cached ASTs
  * Interpreter(reparse=True)    'naive' - re-lex and re-parse every formula on
                                 every recalculation (what an engine without a
                                 compilation step effectively does)
"""
from __future__ import annotations

from typing import Dict, Iterable, List, Optional

from . import runtime as rt
from .functions import arity_ok, lookup
from .ast_nodes import (ArrayLit, BinOp, Bool, CellRef, ErrorLit, FuncCall, Missing, NameRef,
                        Node, Number, Percent, RangeRef, String, UnaryOp)
from .graph import DependencyGraph, _sort_key
from .parser import Parser
from .refs import make_key
from .symbols import SymbolTable
from .workbook import Workbook


def input_value(raw):
    """The runtime value of a constant cell."""
    if raw is None or raw == "":
        return None
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, int):
        return float(raw)
    if isinstance(raw, float):
        return raw
    if isinstance(raw, str):
        return rt.ERRORS.get(raw.upper(), raw) if raw.startswith("#") else raw
    if hasattr(raw, "year"):
        return rt.date_to_serial(raw)
    return raw


def unevaluated_function(ast: Optional[Node]) -> Optional[str]:
    """Name of a function SheetGuard recognises (no 'unknown function' error) but cannot
    evaluate, e.g. FILTER, QUERY, IMPORTRANGE - or None."""
    if ast is None:
        return None
    for n in ast.walk():
        if isinstance(n, FuncCall) and n.name not in rt.FUNCS and n.name not in rt.LAZY \
                and lookup(n.name) is not None:
            return n.name
    return None


def is_cell_name(symbols, n: NameRef, sheet: str) -> bool:
    sym = symbols.lookup(n.name, sheet)
    return sym is not None and sym.kind != "constant"


def literal(v) -> Node:
    if isinstance(v, rt.XLError):
        return ErrorLit(v.code)
    if isinstance(v, bool):
        return Bool(v)
    if isinstance(v, (int, float)):
        return Number(float(v))
    if v is None:
        return Missing()
    return String(str(v))


def opaque_cells(wb: Workbook) -> Dict[str, Node]:
    """Formula cells SheetGuard cannot evaluate but whose value the spreadsheet already
    computed (cached in the file). Every back-end treats them as constants with that value,
    so the rest of the workbook still computes correctly."""
    out = {}
    for c in wb.formula_cells():
        if c.value is not None and c.value != "" and unevaluated_function(c.ast):
            out[c.key] = literal(input_value(c.value))
    return out


def evaluation_order(graph: DependencyGraph) -> List[str]:
    """Topological order, then cycle members, then cells downstream of cycles
    (themselves topologically sorted)."""
    order = list(graph.order)
    in_cycle = sorted({k for c in graph.cycles for k in c}, key=_sort_key)
    blocked = set(graph.blocked)
    indeg = {k: sum(1 for p in graph.preds[k] if p in blocked) for k in blocked}
    frontier = sorted([k for k, d in indeg.items() if d == 0], key=_sort_key)
    tail = []
    while frontier:
        k = frontier.pop(0)
        tail.append(k)
        for d in graph.succs[k]:
            if d in indeg:
                indeg[d] -= 1
                if indeg[d] == 0:
                    frontier.append(d)
    return order + in_cycle + tail


class Interpreter:
    def __init__(self, wb: Workbook, symbols: SymbolTable, graph: DependencyGraph,
                 reparse: bool = False):
        self.wb, self.symbols, self.graph = wb, symbols, graph
        self.reparse = reparse
        self.values: Dict[str, object] = {}
        self.order = evaluation_order(graph)
        self.cycle_cells = {k for c in graph.cycles for k in c}
        self.evaluations = 0
        self.opaque = {k: input_value(wb.cell(k).value) for k in opaque_cells(wb)}

    # ------------------------------------------------------------ public API
    def load_inputs(self) -> None:
        self.values = {c.key: input_value(c.raw) for c in self.wb.cells() if not c.is_formula}

    def run(self) -> Dict[str, object]:
        self.load_inputs()
        self.evaluations = 0
        for key in self.order:
            self._eval_cell(key)
        return self.values

    def recalc(self, changes: Dict[str, object]) -> List[str]:
        """Incremental recalculation: apply input changes and recompute only the
        cells reachable from them in the DAG. Returns the recomputed cells."""
        dirty = set()
        for key, raw in changes.items():
            self.values[key] = input_value(raw)
            dirty.update(self.graph.dependents(key))
        todo = [k for k in self.order if k in dirty]
        for key in todo:
            self._eval_cell(key)
        return todo

    # ------------------------------------------------------------ cells
    def _eval_cell(self, key: str) -> None:
        c = self.wb.cell(key)
        if c is None or not c.is_formula:
            return
        self.evaluations += 1
        if key in self.cycle_cells:
            self.values[key] = rt.CIRC
            return
        if key in self.opaque:
            self.values[key] = self.opaque[key]
            return
        if self.reparse:
            try:
                ast = Parser(c.raw).parse()
            except Exception:
                self.values[key] = rt.PARSE
                return
        else:
            ast = c.ast
            if ast is None:
                self.values[key] = rt.PARSE
                return
        try:
            self.values[key] = rt.finish(self.eval(ast, c.sheet))
        except rt.XLError as e:
            self.values[key] = e
        except RecursionError:
            self.values[key] = rt.NUM

    # ------------------------------------------------------------ expressions
    def eval(self, n: Node, sheet: str):
        t = type(n)
        if t is Number:
            return n.value
        if t is BinOp:
            if n.op in ("=", "<>", "<", ">", "<=", ">="):
                return rt.compare(n.op, self.eval(n.left, sheet), self.eval(n.right, sheet))
            return rt.BINOPS[n.op](self.eval(n.left, sheet), self.eval(n.right, sheet))
        if t is CellRef or t is RangeRef or t is NameRef:
            return self.ref(n, sheet)
        if t is FuncCall:
            return self.call(n, sheet)
        if t is String:
            return n.value
        if t is Bool:
            return n.value
        if t is UnaryOp:
            v = self.eval(n.operand, sheet)
            return rt.neg(v) if n.op == "-" else rt.pos(v)
        if t is Percent:
            return rt.pct(self.eval(n.operand, sheet))
        if t is ErrorLit:
            raise rt.ERRORS.get(n.value, rt.VALUE)
        if t is Missing:
            return None
        if t is ArrayLit:
            return rt.Range([[self.eval(x, sheet) for x in row] for row in n.rows])
        raise rt.VALUE

    def ref(self, n: Node, sheet: str):
        if isinstance(n, NameRef):
            sym = self.symbols.lookup(n.name, sheet)
            if sym is None:
                raise rt.NAME
            if sym.kind == "constant":
                return self.eval(sym.target, sheet)
        r = self.symbols.resolve(n, sheet)
        if r is None:
            raise rt.REF
        if isinstance(r, CellRef):
            v = self.values.get(make_key(r.sheet, r.col, r.row))
            if isinstance(v, rt.XLError):
                raise v
            return v
        return self.range_value(r)

    def range_value(self, r: RangeRef) -> rt.Range:
        c1, c2, r1, r2 = self.wb.sheets[r.sheet].bounds(r)
        vals = self.values
        s = r.sheet
        return rt.Range([[vals.get(make_key(s, c, row)) for c in range(c1, c2 + 1)]
                         for row in range(r1, r2 + 1)])

    def call(self, n: FuncCall, sheet: str):
        if not arity_ok(n.name, len(n.args)):
            raise rt.NA                 # like Sheets: a malformed call is #N/A, args are not run
        if n.name not in rt.FUNCS and n.name not in rt.LAZY:
            raise rt.NAME               # unknown / unimplemented function: #NAME? before args
        if n.name in rt.LAZY:
            return rt.call_lazy(n.name, [(lambda a=a: self.eval(a, sheet)) for a in n.args])
        if n.name in rt.REF_AGG:
            return rt.call(n.name, [self._agg_arg(a, sheet) for a in n.args])
        if n.name in rt.ERROR_TOLERANT:
            return rt.call(n.name, [self._tolerant(a, sheet) for a in n.args])
        return rt.call(n.name, [self.eval(a, sheet) for a in n.args])

    def _tolerant(self, a: Node, sheet: str):
        try:
            return self.eval(a, sheet)
        except rt.XLError as e:
            return e

    def _agg_arg(self, a: Node, sheet: str):
        v = self.eval(a, sheet)
        if isinstance(a, CellRef) or (isinstance(a, NameRef) and not isinstance(v, rt.Range)
                                      and is_cell_name(self.symbols, a, sheet)):
            return rt.Range([[v]])
        return v


def evaluate(wb: Workbook, symbols: SymbolTable, graph: DependencyGraph) -> Dict[str, object]:
    return Interpreter(wb, symbols, graph).run()
