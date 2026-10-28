"""The compiler driver: runs every phase in order and collects the results.

    source workbook
        │  Phase 1  lexer            (lexer.py)
        │  Phase 2  parser -> AST    (parser.py)
        │  Phase 3  symbol table     (symbols.py)
        │  Phase 4  dependency DAG   (graph.py)
        │  Phase 5  type/unit check  (typecheck.py)
        │  Phase 6  smell detection  (smells.py)
        │  Phase 7  optimizer        (optimizer.py)   -> codegen.py / vectorize.py on demand
        │  then     auto-fix plan    (fixes.py)
        ▼
    Report  ->  JSON for the sidebar / web UI, or text for the CLI
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .diagnostics import Diagnostic, sort_diagnostics
from .fixes import Fix, merge_diagnostics, plan_fixes
from .graph import DependencyGraph, build_graph
from .optimizer import OptResult, optimize
from .printer import ast_stats
from .smells import detect_smells
from .symbols import SymbolTable, build_symbol_table
from .typecheck import type_check
from .workbook import Workbook


@dataclass
class Report:
    workbook: Workbook
    symbols: SymbolTable
    graph: DependencyGraph
    diagnostics: List[Diagnostic]
    types: Dict[str, object] = field(default_factory=dict)
    fixes: List[Fix] = field(default_factory=list)
    optimization: Optional[OptResult] = None
    timings: Dict[str, float] = field(default_factory=dict)

    def counts(self) -> dict:
        out = {"error": 0, "warning": 0, "info": 0}
        for d in self.diagnostics:
            out[d.severity] = out.get(d.severity, 0) + 1
        return out

    def cell_info(self, key: str) -> dict:
        """Everything the Structure View needs about one cell."""
        c = self.workbook.cell(key)
        if c is None:
            return {"key": key, "empty": True}
        impact, count = self.graph.dependents_limited(key, 200)
        info = {"key": key, "raw": c.raw, "value": c.value, "fmt": c.fmt,
                "is_formula": c.is_formula, "type": str(self.types.get(key, "")),
                "precedents": self.graph.precedents(key, transitive=False),
                "dependents": self.graph.dependents(key, transitive=False),
                "impact": impact, "impact_count": count,
                "diagnostics": [d.to_dict() for d in self.diagnostics if d.cell == key]}
        if c.tokens:
            info["tokens"] = [t.to_dict() for t in c.tokens]
        if c.ast is not None:
            info["ast"] = c.ast.to_dict()
            info["ast_stats"] = ast_stats(c.ast)
        if c.error is not None:
            info["error"] = c.error.to_dict()
        return info

    def to_dict(self, include_cells: bool = True) -> dict:
        d = {
            "workbook": {"name": self.workbook.name, **self.workbook.stats()},
            "counts": self.counts(),
            "diagnostics": [x.to_dict() for x in self.diagnostics],
            "symbols": self.symbols.to_dict(),
            "graph": self.graph.to_dict(),
            "types": {k: str(v) for k, v in self.types.items()},
            "fixes": [f.to_dict() for f in self.fixes],
            "optimizer": self.optimization.to_dict() if self.optimization else None,
            "timings_ms": {k: round(v * 1000, 3) for k, v in self.timings.items()},
        }
        if include_cells:
            d["cells"] = {c.key: self.cell_info(c.key) for c in self.workbook.formula_cells()}
        return d


def _unevaluated_notes(wb: Workbook) -> List[Diagnostic]:
    """One note per function that SheetGuard recognises but does not execute."""
    from .evaluator import opaque_cells, unevaluated_function
    opaque = opaque_cells(wb)
    by_fn: Dict[str, List[str]] = {}
    for c in wb.formula_cells():
        fn = unevaluated_function(c.ast)
        if fn:
            by_fn.setdefault(fn, []).append(c.key)
    out = []
    for fn, keys in sorted(by_fn.items()):
        cached = sum(1 for k in keys if k in opaque)
        more = f" (and {len(keys) - 1} other cell(s))" if len(keys) > 1 else ""
        out.append(Diagnostic(
            "SG106", "info", keys[0], f"{fn}() is checked but not executed by SheetGuard{more}",
            explanation=(f"{fn} is a valid function, so it is parsed, name-checked and part of the "
                         f"dependency graph, but SheetGuard's execution engines do not implement "
                         f"it. " + (f"For {cached} of {len(keys)} cell(s) the value computed by the "
                                    f"spreadsheet is used, so everything downstream stays correct."
                                    if cached else "No cached value is available, so it shows "
                                    "#NAME? in SheetGuard's own recalculation.")),
            fix="Nothing to fix in the workbook; this is a limitation of SheetGuard's evaluator.",
            related=keys[1:20], phase="symbols"))
    return out


VERIFY_FIXES_UP_TO = 20_000      # formula cells; beyond that fixes are not re-verified


def _verify_fixes(wb: Workbook, fixes: List[Fix], diags: List[Diagnostic]) -> List[Fix]:
    """Translation validation for the auto-fixer: compile the corrected workbook and drop
    any fix whose cell would get an error it did not have before (e.g. a copy-paste fix
    that copies a pattern which itself divides by an empty cell)."""
    from .fixes import apply_fixes
    before = {d.cell for d in diags if d.severity == "error"}
    after = analyze(apply_fixes(wb, fixes), verify=False)
    new_errors = {d.cell: d for d in after.diagnostics
                  if d.severity == "error" and d.cell not in before}
    kept = [f for f in fixes if f.cell not in new_errors]
    for f in fixes:
        if f.cell in new_errors:
            for d in diags:
                if d.cell == f.cell and d.suggested_formula == f.after:
                    d.fix += (f" (Not applied automatically: {f.after} would itself raise "
                              f"{new_errors[f.cell].code} – {new_errors[f.cell].message}.)")
    return kept


def analyze(wb: Workbook, verify: bool = True) -> Report:
    timings = {}

    t = time.perf_counter()
    diags = wb.compile()                       # phases 1 + 2
    timings["lex+parse"] = time.perf_counter() - t

    t = time.perf_counter()
    symbols = build_symbol_table(wb)           # phase 3
    timings["symbols"] = time.perf_counter() - t
    diags += symbols.diagnostics
    diags += _unevaluated_notes(wb)

    t = time.perf_counter()
    graph = build_graph(wb, symbols)           # phase 4
    timings["graph"] = time.perf_counter() - t
    diags += graph.diagnostics

    t = time.perf_counter()
    tc = type_check(wb, symbols, graph)        # phase 5
    timings["types"] = time.perf_counter() - t
    diags += tc.diagnostics

    t = time.perf_counter()
    sm = detect_smells(wb, symbols, graph, diags)   # phase 6
    timings["smells"] = time.perf_counter() - t
    diags += sm.diagnostics

    t = time.perf_counter()
    opt = optimize(wb, symbols, graph, tc.types)    # phase 7
    timings["optimizer"] = time.perf_counter() - t
    diags += opt.diagnostics

    diags = sort_diagnostics(merge_diagnostics(diags))
    fixes = plan_fixes(wb, diags)
    if verify and fixes and sum(1 for _ in wb.formula_cells()) <= VERIFY_FIXES_UP_TO:
        t = time.perf_counter()
        fixes = _verify_fixes(wb, fixes, diags)
        timings["fix check"] = time.perf_counter() - t
    return Report(wb, symbols, graph, diags, tc.types, fixes, opt, timings)
