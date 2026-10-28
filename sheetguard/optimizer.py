"""Phase 7 - Code optimization: classical passes over the AST and the DAG.

  1. Constant folding        12*25000*(1+10%)      ->  330000
  2. Algebraic simplification  x*1, x+0, x/1, --x  ->  x     (only when x is
                               proven numeric, so text/blank semantics are kept)
  3. Dead-branch elimination IF(TRUE, a, b)         ->  a
  4. Constant propagation    named constants (Rate = 0.07) are inlined
  5. Dead-cell elimination   inputs nothing reads (an 'Old FX rate' left behind)
  6. Program slicing         the minimal set of cells needed for chosen outputs
  (Common-subexpression elimination happens in the code generator, where the
   temporaries live.)

Every pass is semantics-preserving: volatile functions (RAND, NOW, TODAY...)
are never folded and an expression whose folding would produce an error is
left untouched so the error still surfaces at run time.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set

from . import functions
from . import runtime as rt
from .ast_nodes import (BinOp, Bool, CellRef, ErrorLit, FuncCall, NameRef, Node, Number,
                        Percent, RangeRef, String, UnaryOp)
from .diagnostics import Diagnostic
from .graph import DependencyGraph, _sort_key
from .symbols import SymbolTable
from .transforms import clone, map_tree
from .workbook import Workbook

LITERALS = (Number, String, Bool)


def _literal(v) -> Optional[Node]:
    if isinstance(v, bool):
        return Bool(v)
    if isinstance(v, (int, float)):
        v = float(v)
        if v != v or v in (float("inf"), float("-inf")):
            return None
        text = str(int(v)) if v.is_integer() and abs(v) < 1e15 else repr(round(v, 12))
        return Number(v, text)
    if isinstance(v, str):
        return String(v)
    return None


def _value_of(n: Node):
    return n.value


@dataclass
class CellOpt:
    key: str
    before: str
    after: str
    passes: List[str] = field(default_factory=list)
    nodes_before: int = 0
    nodes_after: int = 0

    def to_dict(self):
        return self.__dict__


@dataclass
class OptResult:
    asts: Dict[str, Node]
    changes: List[CellOpt]
    dead_cells: List[str]
    diagnostics: List[Diagnostic]
    stats: dict

    def to_dict(self):
        return {"changes": [c.to_dict() for c in self.changes], "dead_cells": self.dead_cells,
                "stats": self.stats}


class Optimizer:
    def __init__(self, wb: Workbook, symbols: SymbolTable, graph: DependencyGraph,
                 types: Optional[dict] = None):
        self.wb, self.symbols, self.graph = wb, symbols, graph
        self.types = types or {}
        self._interp = None

    # ------------------------------------------------------------ helpers
    def _const_eval(self, n: Node):
        from .evaluator import Interpreter
        if self._interp is None:
            self._interp = Interpreter.__new__(Interpreter)
            self._interp.symbols = self.symbols
            self._interp.values = {}
            self._interp.wb = self.wb
        return rt.finish(self._interp.eval(n, ""))

    def _numeric(self, n: Node, sheet: str) -> bool:
        """Is `n` statically guaranteed to be a number (never text / blank)?"""
        if isinstance(n, Number):
            return True
        if isinstance(n, (BinOp,)) and n.op in "+-*/^":
            return True
        if isinstance(n, (UnaryOp, Percent)):
            return True
        if isinstance(n, CellRef):
            ref = self.symbols.resolve(n, sheet)
            if ref is None:
                return False
            t = self.types.get(f"{ref.sheet}!{ref.address()}")
            c = self.wb.cell(f"{ref.sheet}!{ref.address()}")
            return bool(c and not c.is_formula and t is not None and
                        getattr(t, "base", "") == "num")
        return False

    # ------------------------------------------------------------ passes
    def optimize_ast(self, ast: Node, sheet: str, log: List[str]) -> Node:
        root = clone(ast)

        def visit(n: Node) -> Node:
            # 4. constant propagation of named constants
            if isinstance(n, NameRef):
                sym = self.symbols.lookup(n.name, sheet)
                if sym is not None and sym.kind == "constant" and isinstance(sym.target,
                                                                              LITERALS):
                    log.append(f"propagated constant {n.name} = {sym.target.unparse()}")
                    return clone(sym.target)
                return n
            # 3. dead-branch elimination
            if isinstance(n, FuncCall) and n.name == "IF" and len(n.args) in (2, 3) and \
                    isinstance(n.args[0], (Bool, Number)):
                cond = bool(n.args[0].value)
                kept = n.args[1] if cond else (n.args[2] if len(n.args) > 2 else Bool(False))
                log.append(f"dead branch eliminated in IF({n.args[0].unparse()}, …)")
                return kept
            # 1. constant folding
            kids = n.children()
            if isinstance(n, (BinOp, UnaryOp, Percent, FuncCall)) and \
                    all(isinstance(k, LITERALS) for k in kids):
                if isinstance(n, FuncCall):
                    sig = functions.lookup(n.name)
                    if sig is None or sig.volatile or n.name in rt.LAZY:
                        return n
                try:
                    v = self._const_eval(n)
                except rt.XLError:
                    return n
                lit = _literal(v)
                if lit is not None:
                    log.append(f"folded {n.unparse()} → {lit.unparse()}")
                    return lit
            if isinstance(n, Percent) and isinstance(n.operand, Number):
                v = n.operand.value / 100
                lit = _literal(v)
                log.append(f"folded {n.unparse()} → {lit.unparse()}")
                return lit
            # 2. algebraic simplification (identities)
            if isinstance(n, BinOp):
                L, R = n.left, n.right
                lv = L.value if isinstance(L, Number) else None
                rv = R.value if isinstance(R, Number) else None
                rule = None
                if n.op == "*" and rv == 1 and self._numeric(L, sheet):
                    rule = L
                elif n.op == "*" and lv == 1 and self._numeric(R, sheet):
                    rule = R
                elif n.op in "+-" and rv == 0 and self._numeric(L, sheet):
                    rule = L
                elif n.op == "+" and lv == 0 and self._numeric(R, sheet):
                    rule = R
                elif n.op == "/" and rv == 1 and self._numeric(L, sheet):
                    rule = L
                elif n.op == "^" and rv == 1 and self._numeric(L, sheet):
                    rule = L
                if rule is not None:
                    log.append(f"simplified {n.unparse()} → {rule.unparse()}")
                    return rule
            if isinstance(n, UnaryOp) and n.op == "-" and isinstance(n.operand, UnaryOp) and \
                    n.operand.op == "-" and self._numeric(n.operand.operand, sheet):
                log.append(f"simplified {n.unparse()} → {n.operand.operand.unparse()}")
                return n.operand.operand
            if isinstance(n, UnaryOp) and n.op == "+" and self._numeric(n.operand, sheet):
                return n.operand
            return n

        return map_tree(root, visit)

    def dead_cells(self) -> List[str]:
        """Numeric inputs that nothing reads, sitting in a column of inputs that *are* read."""
        dead = []
        for sh in self.wb.sheets.values():
            for c in sh:
                if c.is_formula or isinstance(c.raw, bool) or not isinstance(c.raw, (int, float)):
                    continue
                if self.graph.succs.get(c.key):
                    continue
                column = [x for x in sh if x.col == c.col and x.key != c.key and not x.is_formula
                          and isinstance(x.raw, (int, float))]
                if column and any(self.graph.succs.get(x.key) for x in column):
                    dead.append(c.key)
        return sorted(dead, key=_sort_key)

    def _who_should_read(self, c):
        """A dead data cell is often the *symptom* of a bug elsewhere: find the formula in
        the same row that reads a neighbouring row of this column instead."""
        sh = self.wb.sheets[c.sheet]
        for f in sorted((x for x in sh if x.row == c.row and x.is_formula and x.ast is not None),
                        key=lambda x: x.col):
            for p in self.graph.preds.get(f.key, ()):
                ps, _, pref = p.rpartition("!")
                from .refs import parse_cell
                pc, pr, _, _ = parse_cell(pref)
                if ps == c.sheet and pc == c.col and pr != c.row:
                    return (f"{f.address} ({f.raw}) reads {pref} from another row instead — "
                            f"this unused value is a symptom of that bug.",
                            f"Check {f.address}: it probably should reference {c.address}.",
                            [f.key])
        for x in sh:
            if x.row == c.row and not x.is_formula and isinstance(x.raw, (int, float)) and \
                    x.col > c.col:
                up, down = sh.get(x.col, c.row - 1), sh.get(x.col, c.row + 1)
                if (up is not None and up.is_formula and not up.raw.upper().startswith("=SUM")) \
                        or (down is not None and down.is_formula and up is not None
                            and up.is_formula):
                        return (f"{x.address} holds a typed value where the column has formulas, "
                                f"so the inputs in this row are no longer used.",
                                f"Restore the formula in {x.address}.", [x.key])
        return None

    def slice_for(self, targets: List[str]) -> List[str]:
        """Program slicing: every cell needed to compute `targets`."""
        need: Set[str] = set(targets)
        for t in targets:
            need.update(self.graph.precedents(t))
        return sorted(need, key=_sort_key)

    def run(self) -> OptResult:
        asts, changes, diags = {}, [], []
        total_before = total_after = 0
        counters = {"folded": 0, "simplified": 0, "branches": 0, "propagated": 0}
        for c in self.wb.formula_cells():
            if c.ast is None:
                continue
            log: List[str] = []
            new = self.optimize_ast(c.ast, c.sheet, log)
            asts[c.key] = new
            nb, na = sum(1 for _ in c.ast.walk()), sum(1 for _ in new.walk())
            total_before += nb
            total_after += na
            if log:
                for entry in log:
                    for k, word in (("folded", "folded"), ("simplified", "simplified"),
                                    ("branches", "dead branch"), ("propagated", "propagated")):
                        if entry.startswith(word):
                            counters[k] += 1
                changes.append(CellOpt(c.key, c.raw, "=" + new.unparse(), log, nb, na))
                if isinstance(new, LITERALS) and any(isinstance(x, (BinOp, FuncCall, Percent))
                                                     for x in c.ast.walk()):
                    diags.append(Diagnostic(
                        "SG501", "info", c.key,
                        f"constant expression: always equals {new.unparse()}",
                        explanation=f"{c.raw} has no inputs, so the optimizer folds it to the "
                                    f"constant {new.unparse()} at compile time. A hidden "
                                    f"assumption like this is easy to forget when things change.",
                        fix="Move the assumption(s) into labelled input cells and reference "
                            "them.", phase="optimizer"))
        dead = self.dead_cells()
        for key in dead:
            c = self.wb.cell(key)
            label = next((x.raw for x in self.wb.sheets[c.sheet] if x.row == c.row and
                          x.col < c.col and isinstance(x.raw, str)), "")
            hint = self._who_should_read(c)
            diags.append(Diagnostic(
                "SG502", "warning" if hint else "info", key,
                f"dead cell: {('“' + label + '” ') if label else ''}{c.raw} is never used",
                explanation=("No formula reads this input, unlike the inputs around it. " +
                             (hint[0] if hint else "It is dead code: either a stale assumption "
                              "that should be deleted, or a value someone *meant* to use.")),
                fix=hint[1] if hint else "Delete it, or reference it where it was intended.",
                related=hint[2] if hint else [], phase="optimizer"))
        stats = {**counters, "cells_changed": len(changes), "ast_nodes_before": total_before,
                 "ast_nodes_after": total_after, "dead_cells": len(dead)}
        return OptResult(asts, changes, dead, diags, stats)


def optimize(wb, symbols, graph, types=None) -> OptResult:
    return Optimizer(wb, symbols, graph, types).run()
