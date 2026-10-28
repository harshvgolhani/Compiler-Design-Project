"""Dynamic analysis: type-guided fuzz testing of a workbook.

Static analysis finds bugs that are visible *now*. Some bugs only appear for
certain inputs: =F10/E10 is fine until the discount is 100 %, and then the
report shows #DIV/0!. The fuzzer searches for such inputs automatically.

  1. Generate inputs from the *inferred types* (the type checker's output):
       percent   0 %, 100 %, 99.9 %            (plausible)    -50 %, 250 %   (adversarial)
       currency  0, 0.01, 10 000 000           (plausible)    -100           (adversarial)
       number    0, 1, 1 000 000               (plausible)    -1             (adversarial)
       any       blank, "n/a"                                  (adversarial: missing / text)
     plus column strategies: every value in a data column set to 0 or left blank
     ("nothing sold this quarter").
  2. Execute each mutated workbook with the *compiled* code from codegen.py,
     which is fast enough for hundreds of runs per second.
  3. Report every formula that was fine at baseline but turns into an error,
     with the smallest witness (the input and value that triggers it) and a
     suggested guard.
"""
from __future__ import annotations

import time
from collections import defaultdict
from typing import Dict, List, Tuple

from . import runtime as rt
from .ast_nodes import BinOp, Node
from .codegen import CodeGenerator, prepare_inputs
from .diagnostics import Diagnostic
from .graph import _sort_key
from .refs import split_key
from .summary import _label

PLAUSIBLE = {"percent": [0.0, 1.0, 0.999], "currency": [0.0, 0.01, 1e7], "": [0.0, 1.0, 1e6],
             "date": [0.0, 1.0]}
ADVERSARIAL = {"percent": [-0.5, 2.5], "currency": [-100.0], "": [-1.0], "date": [-1.0]}


def _unit(t) -> str:
    base = getattr(t, "base", "")
    if base == "date":
        return "date"
    u = getattr(t, "unit", "")
    return u if u in ("percent", "currency") else ""


def _fmt(v, unit: str) -> str:
    if v is None:
        return "blank"
    if isinstance(v, str):
        return f'"{v}"'
    if unit == "percent":
        return f"{v * 100:g}%"
    if unit == "currency":
        return f"${v:,.2f}"
    return f"{v:g}"


class Fuzzer:
    def __init__(self, wb, rep, max_runs: int = 2000):
        self.wb, self.rep = wb, rep
        self.max_runs = max_runs
        asts = rep.optimization.asts if rep.optimization else None
        self.cg = CodeGenerator(wb, rep.symbols, rep.graph, rep.types, asts)
        self.fn = self.cg.compile()
        self.base_inputs = prepare_inputs(wb)
        self.baseline = self.fn(self.base_inputs)

    # ------------------------------------------------------------------ input space
    def input_cells(self) -> List[str]:
        out = []
        for c in self.wb.cells():
            if c.is_formula or isinstance(c.raw, bool) or not isinstance(c.raw, (int, float)):
                continue
            if self.rep.graph.succs.get(c.key):
                out.append(c.key)
        return sorted(out, key=_sort_key)

    def mutations(self) -> List[dict]:
        muts = []
        inputs = self.input_cells()
        for k in inputs:
            unit = _unit(self.rep.types.get(k))
            for v in PLAUSIBLE.get(unit, PLAUSIBLE[""]):
                muts.append({"cells": [k], "value": v, "plausible": True, "unit": unit})
            for v in ADVERSARIAL.get(unit, ADVERSARIAL[""]):
                muts.append({"cells": [k], "value": v, "plausible": False, "unit": unit})
            muts.append({"cells": [k], "value": None, "plausible": False, "unit": unit})
            muts.append({"cells": [k], "value": "n/a", "plausible": False, "unit": unit})
        # column strategies: a whole block of data inputs at once
        cols: Dict[Tuple[str, int], List[str]] = defaultdict(list)
        for k in inputs:
            s, c, r = split_key(k)
            cols[(s, c)].append(k)
        for (s, c), keys in cols.items():
            if len(keys) >= 3:
                unit = _unit(self.rep.types.get(keys[0]))
                muts.append({"cells": keys, "value": 0.0, "plausible": True, "unit": unit,
                             "column": True})
                muts.append({"cells": keys, "value": None, "plausible": False, "unit": unit,
                             "column": True})
        return muts[: self.max_runs]

    # ------------------------------------------------------------------ run
    def run(self) -> Dict:
        t0 = time.perf_counter()
        muts = self.mutations()
        base = self.baseline
        formula_keys = [c.key for c in self.wb.formula_cells()]
        healthy = {k for k in formula_keys if not isinstance(base.get(k), rt.XLError)}
        findings: Dict[Tuple[str, str], dict] = {}
        sensitive = set()
        runs = 0
        for m in muts:
            I = dict(self.base_inputs)
            for k in m["cells"]:
                I[k] = m["value"]
            out = self.fn(I)
            runs += 1
            for k in formula_keys:
                v, b = out.get(k), base.get(k)
                if not _same(v, b):
                    sensitive.add(k)
                if k in healthy and isinstance(v, rt.XLError):
                    key = (k, v.code)
                    f = findings.get(key)
                    if f is None:
                        f = findings[key] = {"cell": k, "error": v.code, "witness": None,
                                             "triggers": 0, "plausible_triggers": 0}
                    f["triggers"] += 1
                    if m["plausible"]:
                        f["plausible_triggers"] += 1
                    better = f["witness"] is None or (m["plausible"] and not f["witness"]["plausible"]) \
                        or (m["plausible"] == f["witness"]["plausible"]
                            and len(m["cells"]) < len(f["witness"]["cells"]))
                    if better:
                        f["witness"] = m
        elapsed = time.perf_counter() - t0
        # root-cause findings only: drop cells whose error merely propagates from another finding
        result = []
        found_cells = {k for k, _ in findings}
        for (k, code), f in sorted(findings.items(), key=lambda x: _sort_key(x[0][0])):
            upstream = found_cells & set(self.rep.graph.precedents(k))
            if upstream and any(findings.get((u, code)) for u in upstream):
                continue
            result.append(self._describe(f))
        result.sort(key=lambda f: (not f["plausible"], _sort_key(f["cell"])))
        groups: Dict[str, dict] = {}
        for f in result:
            if f["plausible"]:
                continue
            g = groups.setdefault(f["error"], {"error": f["error"], "count": 0, "example": f})
            g["count"] += 1
        return {
            "plausible": [f for f in result if f["plausible"]],
            "adversarial_groups": list(groups.values()),
            "runs": runs, "inputs": len(self.input_cells()), "formulas": len(formula_keys),
            "ms": round(elapsed * 1000, 1),
            "runs_per_second": round(runs / elapsed) if elapsed else None,
            "sensitivity_coverage": round(100 * len(sensitive) / max(1, len(healthy)), 1),
            "findings": result,
            "propagated": len(findings) - len(result),
        }

    def _describe(self, f: dict) -> dict:
        m = f["witness"]
        cell = self.wb.cell(f["cell"])
        if m.get("column"):
            from .refs import index_to_col
            s, c, _ = split_key(m["cells"][0])
            what = f"every value in column {s}!{index_to_col(c)} ({len(m['cells'])} cells)"
            label = _label(self.wb, m["cells"][0]).split(" — ")[-1]
            witness = f"{what} — {label} — set to {_fmt(m['value'], m['unit'])}"
        else:
            k = m["cells"][0]
            witness = f"{k} ({_label(self.wb, k)}) = {_fmt(m['value'], m['unit'])}"
        fix = self._guard(cell)
        return {
            "cell": f["cell"], "label": _label(self.wb, f["cell"]), "formula": cell.raw,
            "error": f["error"], "witness": witness, "plausible": m["plausible"],
            "triggers": f["triggers"], "suggested_formula": fix,
            "diagnostic": Diagnostic(
                "SG601", "warning" if m["plausible"] else "info", f["cell"],
                f"becomes {f['error']} when {witness}",
                explanation=(f"{cell.raw} works with today's numbers, but the fuzzer found an "
                             f"{'ordinary' if m['plausible'] else 'unusual'} input that breaks it. "
                             f"{f['triggers']} of the generated test inputs trigger {f['error']}."),
                fix=("Guard the division so the report stays readable: " + fix) if fix else
                "Validate the input or wrap the formula in IFERROR().",
                suggested_formula=fix, phase="fuzz").to_dict(),
        }

    def _guard(self, cell) -> str:
        ast: Node = cell.ast
        if ast is None:
            return ""
        if isinstance(ast, BinOp) and ast.op == "/":
            return f"=IF({ast.right.unparse()}=0,0,{ast.unparse()})"
        return f"=IFERROR({ast.unparse()},0)"


def _same(a, b) -> bool:
    if isinstance(a, float) and isinstance(b, float):
        return abs(a - b) <= 1e-9 * max(1.0, abs(a), abs(b))
    if isinstance(a, rt.XLError) or isinstance(b, rt.XLError):
        return str(a) == str(b)
    return a == b


def fuzz_workbook(wb, rep, max_runs: int = 2000) -> Dict:
    return Fuzzer(wb, rep, max_runs).run()
