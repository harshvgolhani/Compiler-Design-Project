"""Benchmark: the same workbook recalculated by five execution engines.

    naive        re-lex + re-parse every formula, then interpret     (no compiler)
    interpreter  parse once, walk cached ASTs in topological order
    compiled     generated straight-line Python, no optimizations
    optimized    + type specialization + CSE + constant folding
    numpy        + loop vectorization of formula columns

Plus incremental recalculation: change one input and recompute only the
cells the DAG says can be affected.
"""
from __future__ import annotations

import random
import time
from typing import Dict, List

from .codegen import CodeGenerator, prepare_inputs
from .evaluator import Interpreter
from .optimizer import optimize
from .pipeline import analyze
from .runtime import XLError
from .vectorize import VectorCompiler
from .workbook import Workbook

USD = '"$"#,##0.00'
PCT = "0.0%"


def make_sales_workbook(n_rows: int = 2000, seed: int = 7) -> Workbook:
    """A clean, realistic sales model with `n_rows` regions/products."""
    rnd = random.Random(seed)
    wb = Workbook(f"Synthetic sales model ({n_rows:,} rows)")
    inp = wb.add_sheet("Inputs")
    for r, (a, b, f) in enumerate([("Parameter", "Value", ""), ("Tax Rate", 0.18, PCT),
                                   ("Discount", 0.05, PCT), ("Commission Rate", 0.10, PCT),
                                   ("USD→INR", 83.2, "")], start=1):
        inp.set(1, r, a)
        inp.set(2, r, b, fmt=f)
    for n, ref in (("TaxRate", "Inputs!$B$2"), ("Discount", "Inputs!$B$3"),
                   ("CommissionRate", "Inputs!$B$4"), ("FX", "Inputs!$B$5")):
        wb.define_name(n, ref)
    s = wb.add_sheet("Sales")
    for c, h in enumerate(["Item", "Units", "Unit Price", "Revenue", "Discounted", "Tax",
                           "Total", "Growth %", "Projected"], start=1):
        s.set(c, 1, h)
    last = n_rows + 1
    for r in range(2, last + 1):
        s.set(1, r, f"Item {r - 1}")
        s.set(2, r, rnd.randint(10, 2000))
        s.set(3, r, round(rnd.uniform(5, 120), 2), fmt=USD)
        s.set(4, r, f"=B{r}*C{r}", fmt=USD)
        s.set(5, r, f"=D{r}*(1-Discount)", fmt=USD)
        s.set(6, r, f"=E{r}*TaxRate", fmt=USD)
        s.set(7, r, f"=E{r}+F{r}", fmt=USD)
        s.set(8, r, round(rnd.uniform(-0.05, 0.2), 3), fmt=PCT)
        s.set(9, r, f"=G{r}*(1+H{r})", fmt=USD)
    t = last + 1
    s.set(1, t, "TOTAL")
    for c, col in ((2, "B"), (4, "D"), (5, "E"), (6, "F"), (7, "G"), (9, "I")):
        s.set(c, t, f"=SUM({col}2:{col}{last})")
    m = wb.add_sheet("Summary")
    for r, (a, b) in enumerate([("Metric", "Value"), ("Total revenue", f"=Sales!G{t}"),
                                ("Commission", "=B2*CommissionRate"),
                                ("Profit", "=B2-B3"), ("Revenue in INR", "=B2*FX"),
                                ("Projected growth", f"=Sales!I{t}/Sales!G{t}-1"),
                                ("Best item revenue", f"=MAX(Sales!D2:D{last})")], start=1):
        m.set(1, r, a)
        m.set(2, r, b)
    return wb


def _best(fn, repeat: int) -> float:
    best = float("inf")
    for _ in range(repeat):
        t = time.perf_counter()
        fn()
        best = min(best, time.perf_counter() - t)
    return best


def _same(a, b) -> bool:
    if isinstance(a, float) and isinstance(b, float):
        return abs(a - b) <= 1e-9 * max(1.0, abs(a), abs(b))
    if isinstance(a, XLError) or isinstance(b, XLError):
        return str(a) == str(b)
    return a == b


def run_benchmark(wb: Workbook = None, n_rows: int = 2000, repeat: int = 3,
                  include_naive: bool = True) -> Dict:
    wb = wb or make_sales_workbook(n_rows)
    rep = analyze(wb)
    opt = optimize(wb, rep.symbols, rep.graph, rep.types)
    inputs = prepare_inputs(wb)
    n_formulas = sum(1 for _ in wb.formula_cells())
    results: List[Dict] = []

    interp = Interpreter(wb, rep.symbols, rep.graph)
    ref = dict(interp.run())

    def record(name, desc, seconds, values=None, extra=None):
        ok = None
        if values is not None:
            ok = all(_same(values.get(k), ref.get(k)) for k in ref
                     if wb.cell(k) is not None and wb.cell(k).is_formula)
        results.append({"engine": name, "description": desc, "ms": round(seconds * 1000, 3),
                        "verified": ok, **(extra or {})})

    if include_naive:
        naive = Interpreter(wb, rep.symbols, rep.graph, reparse=True)
        record("naive", "re-parse + interpret every formula", _best(naive.run, repeat),
               naive.run())
    record("interpreter", "AST interpreter (parse once)", _best(interp.run, repeat), interp.run())

    plain = CodeGenerator(wb, rep.symbols, rep.graph, rep.types, specialize=False, cse=False)
    plain.generate()
    f_plain = plain.compile()
    record("compiled", "generated Python, no optimizations",
           _best(lambda: f_plain(inputs), repeat), f_plain(inputs),
           {"compile_ms": plain.stats["compile_ms"]})

    cg = CodeGenerator(wb, rep.symbols, rep.graph, rep.types, opt.asts)
    cg.generate()
    f_opt = cg.compile()
    record("optimized", "+ type specialization, CSE, folding",
           _best(lambda: f_opt(inputs), repeat), f_opt(inputs),
           {"compile_ms": cg.stats["compile_ms"], "fast_cells": cg.stats["fast_cells"],
            "cse_temps": cg.stats["cse_temps"], "cse_reuses": cg.stats["cse_reuses"]})

    vec_info = {}
    try:
        import numpy  # noqa: F401
        vc = VectorCompiler(wb, rep.symbols, rep.graph, rep.types, opt.asts)
        vc.generate()
        f_vec = vc.compile(fallback=f_opt)
        out = f_vec(inputs)
        record("numpy", "+ loop vectorization (NumPy)", _best(lambda: f_vec(inputs), repeat),
               f_vec.materialize(out),
               {"compile_ms": vc.stats["compile_ms"], "vector_groups": vc.stats["vector_groups"],
                "vector_cells": vc.stats["vector_cells"], "deopts": vc.stats["deopts"]})
        vec_info = dict(vc.stats)
    except ImportError:
        pass

    base = results[0]["ms"]
    for r in results:
        r["speedup"] = round(base / r["ms"], 1) if r["ms"] else None

    # incremental recalculation after editing one input
    inc = Interpreter(wb, rep.symbols, rep.graph)
    inc.run()
    t = time.perf_counter()
    touched = inc.recalc({"Inputs!B2": 0.2})
    inc_ms = (time.perf_counter() - t) * 1000
    t = time.perf_counter()
    touched2 = inc.recalc({"Sales!B2": 999})
    inc2_ms = (time.perf_counter() - t) * 1000

    return {
        "workbook": wb.name, "formulas": n_formulas, "cells": wb.stats()["cells"],
        "results": results, "vector": vec_info,
        "incremental": [
            {"change": "Inputs!B2 (TaxRate)", "recomputed": len(touched),
             "ms": round(inc_ms, 3)},
            {"change": "Sales!B2 (one row's units)", "recomputed": len(touched2),
             "ms": round(inc2_ms, 3)},
        ],
        "optimizer": opt.stats,
    }


def format_benchmark(b: Dict) -> str:
    lines = [f"{b['workbook']}: {b['formulas']:,} formulas, {b['cells']:,} cells", "",
             f"{'engine':<12} {'time (ms)':>11} {'speed-up':>9}  {'check':<6} description",
             "-" * 78]
    for r in b["results"]:
        ok = {True: "✓ same", False: "✗ DIFF", None: ""}[r["verified"]]
        lines.append(f"{r['engine']:<12} {r['ms']:>11,.2f} {r['speedup']:>8}×  {ok:<6} "
                     f"{r['description']}")
    lines.append("")
    lines.append("incremental recalculation (DAG-driven, only affected cells):")
    for i in b["incremental"]:
        lines.append(f"  change {i['change']:<28} → {i['recomputed']:>6,} of {b['formulas']:,} "
                     f"formulas recomputed in {i['ms']:.2f} ms")
    return "\n".join(lines)
