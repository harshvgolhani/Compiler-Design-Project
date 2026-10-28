"""JSON API: the boundary between the compiler and its front-ends.

Both front-ends call these functions: the Google Sheets sidebar (through the
HTTP server, via Apps Script's UrlFetchApp) and the standalone web UI. Every
function takes and returns plain JSON-able dicts.
"""
from __future__ import annotations

import base64
import io
import math
import os
import tempfile
import time
from typing import Any, Dict

from . import __version__
from . import runtime as rt
from .codegen import CodeGenerator
from .demo import build_demo_workbook
from .errors import SheetGuardError
from .fixes import apply_fixes, export_xlsx
from .lexer import tokenize
from .parser import parse
from .pipeline import analyze
from .printer import ast_tree
from .refs import a1
from .summary import compute_values, health_score, impact_report, summarize
from .transforms import r1c1
from .workbook import Workbook, WorkbookLoadError

MAX_GRAPH_NODES = 900
MAX_CELL_INFO = 3000
GRID_ROWS, GRID_COLS = 400, 60


def _int(payload: Dict, key: str, default: int, lo: int, hi: int) -> int:
    """An integer request parameter, clamped to [lo, hi]; a clear error if it is not a number."""
    v = payload.get(key, default)
    try:
        v = int(float(v))
    except (TypeError, ValueError):
        raise ValueError(f"'{key}' must be a number, got {v!r}") from None
    return max(lo, min(v, hi))


def jsonable(v: Any) -> Any:
    if isinstance(v, rt.XLError):
        return v.code
    if isinstance(v, float):
        if math.isnan(v) or math.isinf(v):
            return str(v)
        return v
    if isinstance(v, dict):
        return {str(k): jsonable(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [jsonable(x) for x in v]
    if hasattr(v, "isoformat"):
        return v.isoformat()
    return v


def load_workbook(payload: Dict) -> Workbook:
    if payload.get("demo"):
        return build_demo_workbook()
    if payload.get("xlsx_base64"):
        try:
            data = base64.b64decode(payload["xlsx_base64"], validate=False)
        except ValueError:
            raise WorkbookLoadError("the uploaded file could not be decoded (bad base64)") from None
        if not data.startswith(b"PK"):
            raise WorkbookLoadError(f"{payload.get('name') or 'the uploaded file'} is not an .xlsx "
                                    f"file (old .xls files must be re-saved as .xlsx)")
        with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as fh:
            fh.write(data)
            path = fh.name
        try:
            try:
                wb = Workbook.from_xlsx(path)
            except WorkbookLoadError:
                raise WorkbookLoadError(f"{payload.get('name') or 'the uploaded file'} is not a "
                                        f"readable .xlsx file. Re-save it from Excel or Sheets "
                                        f"(File > Download > .xlsx) and try again.") from None
            if payload.get("name"):
                wb.name = os.path.splitext(payload["name"])[0]
            return wb
        finally:
            os.unlink(path)
    if payload.get("workbook"):
        return Workbook.from_json(payload["workbook"])
    if isinstance(payload.get("sheets"), list):            # a bare workbook object
        return Workbook.from_json(payload)
    raise ValueError("payload needs one of: demo, workbook, xlsx_base64")


def _grid(wb: Workbook, values: Dict, rep) -> list:
    sev_of: Dict[str, str] = {}
    order = {"error": 0, "warning": 1, "info": 2}
    for d in rep.diagnostics:
        if d.cell and (d.cell not in sev_of or order[d.severity] < order[sev_of[d.cell]]):
            sev_of[d.cell] = d.severity
    out = []
    for sh in wb.sheets.values():
        cells = {}
        omitted = 0
        for c in sh:
            if (c.row > GRID_ROWS or c.col > GRID_COLS) and c.key not in sev_of:
                omitted += 1                     # the UI shows the top-left 400 x 60 cells
                continue
            v = values.get(c.key, c.raw)
            cells[c.address] = {
                "raw": jsonable(c.raw), "display": rt.display(v) if not isinstance(v, str)
                or c.is_formula else v, "fmt": c.fmt, "f": c.is_formula,
                "value": jsonable(v) if not isinstance(v, (str,)) else v,
                "sev": sev_of.get(c.key), "type": str(rep.types.get(c.key, "")),
            }
        out.append({"name": sh.name, "rows": sh.max_row, "cols": sh.max_col, "cells": cells,
                    "omitted": omitted})
    return out


def analyze_payload(payload: Dict) -> Dict:
    t0 = time.perf_counter()
    wb = load_workbook(payload)
    rep = analyze(wb)
    values = compute_values(wb, rep)
    impact = impact_report(wb, rep, values) if payload.get("impact", True) else None
    summary = summarize(wb, rep, impact)
    d = rep.to_dict(include_cells=False)

    # per-cell compiler internals for the Structure View
    cells = {}
    for c in list(wb.formula_cells())[:MAX_CELL_INFO]:
        info = rep.cell_info(c.key)
        info["value"] = jsonable(values.get(c.key))
        info["display"] = rt.display(values.get(c.key))
        if c.ast is not None:
            info["tree"] = ast_tree(c.ast)
            info["r1c1"] = r1c1(c.ast, c.col, c.row)
        cells[c.key] = info
    d["cells"] = cells

    graph = d["graph"]
    if len(graph["nodes"]) > MAX_GRAPH_NODES:
        keep = {n["id"] for n in graph["nodes"] if n["is_formula"] or n["in_cycle"]}
        keep = set(list(sorted(keep))[:MAX_GRAPH_NODES])
        graph["nodes"] = [n for n in graph["nodes"] if n["id"] in keep]
        graph["edges"] = [e for e in graph["edges"] if e["from"] in keep and e["to"] in keep]
        graph["truncated"] = True

    d["grid"] = _grid(wb, values, rep)
    d["health"] = health_score(rep.counts())
    d["summary"] = summary
    d["impact"] = impact
    d["version"] = __version__
    d["elapsed_ms"] = round((time.perf_counter() - t0) * 1000, 1)
    d["source_workbook"] = wb.to_json() if payload.get("return_workbook") else None
    return jsonable(d)


def parse_payload(payload: Dict) -> Dict:
    formula = str(payload.get("formula") or "").strip()
    if not formula.startswith("="):
        formula = "=" + formula
    out: Dict[str, Any] = {"formula": formula}
    try:
        toks = tokenize(formula)
        out["tokens"] = [t.to_dict() for t in toks]
        tree = parse(formula)
        out["ast"] = tree.to_dict()
        out["tree"] = ast_tree(tree)
        out["unparsed"] = "=" + tree.unparse()
        # evaluate inside the current workbook (so Sales!B2 or TaxRate resolve), in a free cell
        ctx = {k: payload[k] for k in ("demo", "workbook", "xlsx_base64") if payload.get(k)}
        wb = load_workbook(ctx) if ctx else Workbook("Playground")
        sheet = payload.get("sheet") if payload.get("sheet") in wb.sheets else \
            (next(iter(wb.sheets)) if wb.sheets else "Sheet1")
        sh = wb.add_sheet(sheet)
        col, row = 1, sh.max_row + 3
        sh.set(col, row, formula)
        key = f"{sheet}!{a1(col, row)}"
        out["r1c1"] = r1c1(tree, col, row)
        out["context"] = {"workbook": wb.name, "cell": key}
        rep = analyze(wb)
        out["diagnostics"] = [d.to_dict() for d in rep.diagnostics
                              if d.cell == key and d.code not in ("SG105",)]
        out["type"] = str(rep.types.get(key, ""))
        ch = next((c for c in (rep.optimization.changes if rep.optimization else [])
                   if c.key == key), None)
        if ch:
            out["optimized"] = ch.after
            out["passes"] = ch.passes
        vals = compute_values(wb, rep)
        out["value"] = rt.display(vals.get(key))
    except SheetGuardError as e:
        out["error"] = e.to_dict()
        from .parser import repair
        out["repair"] = repair(formula, e)
    return jsonable(out)


def fix_payload(payload: Dict) -> Dict:
    wb = load_workbook(payload)
    rep = analyze(wb)
    fixed = apply_fixes(wb, rep.fixes)
    return jsonable({"fixes": [f.to_dict() for f in rep.fixes],
                     "workbook": fixed.to_json()})


def export_payload(payload: Dict) -> Dict:
    wb = load_workbook(payload)
    rep = analyze(wb)
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "out.xlsx")
        export_xlsx(wb, path, rep.diagnostics, rep.fixes if payload.get("fix", True) else [])
        with open(path, "rb") as fh:
            data = base64.b64encode(fh.read()).decode()
    name = f"{wb.name} - SheetGuard.xlsx".replace("/", "-")
    return {"filename": name, "xlsx_base64": data, "fixes": len(rep.fixes)}


def compile_payload(payload: Dict) -> Dict:
    wb = load_workbook(payload)
    rep = analyze(wb)
    asts = rep.optimization.asts if rep.optimization else None
    if payload.get("numpy"):
        try:
            from .vectorize import VectorCompiler, pretty_source
        except ImportError:
            return {"error": "numpy is not installed on the server"}
        vc = VectorCompiler(wb, rep.symbols, rep.graph, rep.types, asts)
        src = vc.generate()
        return jsonable({"source": pretty_source(src), "stats": dict(vc.stats), "kind": "numpy"})
    target = payload.get("target") or None
    if target is not None:
        c = wb.cell(str(target))
        if c is None or not c.is_formula:
            raise ValueError(f"target {target!r} is not a formula cell (use e.g. Summary!B2)")
        target = c.key
    cg = CodeGenerator(wb, rep.symbols, rep.graph, rep.types, asts,
                       targets=[target] if target else None)
    src = cg.generate()
    cg.compile()
    return jsonable({"source": src, "stats": dict(cg.stats), "kind": "python",
                     "target": target})


def bench_payload(payload: Dict) -> Dict:
    from .bench import run_benchmark
    rows = _int(payload, "rows", 1000, 10, 20000)
    return jsonable(run_benchmark(n_rows=rows, repeat=_int(payload, "repeat", 3, 1, 10),
                                  include_naive=rows <= 5000))


def demo_workbook_payload(_: Dict = None) -> Dict:
    wb = build_demo_workbook()
    return jsonable({"workbook": wb.to_json()})


def health_payload(_: Dict = None) -> Dict:
    try:
        import numpy  # noqa: F401
        has_np = True
    except ImportError:
        has_np = False
    try:
        import openpyxl  # noqa: F401
        has_xl = True
    except ImportError:
        has_xl = False
    return {"ok": True, "service": "SheetGuard", "version": __version__, "numpy": has_np,
            "openpyxl": has_xl}


_GRAMMAR_CACHE: Dict = {}


def grammar_payload(_: Dict = None) -> Dict:
    """Formal grammar, FIRST/FOLLOW sets, LL(1) table, SLR(1) automaton (static)."""
    if not _GRAMMAR_CACHE:
        from .grammar import grammar_report
        _GRAMMAR_CACHE.update(grammar_report())
    return _GRAMMAR_CACHE


def theory_payload(payload: Dict) -> Dict:
    """Per-formula syllabus lab: LL(1) trace, SLR(1) trace, TAC, blocks, DAGs, target code."""
    from .grammar import SLR_PARSER, ll1_parse
    from .tac import tac_report, workbook_tac_report
    formula = str(payload.get("formula") or "=1").strip()
    if not formula.startswith("="):
        formula = "=" + formula
    nregs = _int(payload, "nregs", 4, 2, 16)
    out: Dict[str, Any] = {"formula": formula, "ll1": ll1_parse(formula),
                           "slr": SLR_PARSER.parse(formula)}
    ctx = {k: payload[k] for k in ("demo", "workbook", "xlsx_base64") if payload.get(k)}
    symbols, sheet, wb, rep = None, "", None, None
    if ctx:
        wb = load_workbook(ctx)
        rep = analyze(wb)
        symbols = rep.symbols
        sheet = payload.get("sheet") if payload.get("sheet") in wb.sheets else next(iter(wb.sheets))
    try:
        out["tac"] = tac_report(formula, payload.get("target") or "result", symbols, sheet, nregs)
    except SheetGuardError as e:
        out["tac"] = {"error": e.to_dict()}
    if rep is not None and payload.get("workbook_tac", True):
        out["workbook_tac"] = workbook_tac_report(wb, rep.symbols, rep.graph, nregs=8)
    return jsonable(out)


def multitarget_payload(payload: Dict) -> Dict:
    from .multitarget import multitarget_report
    wb = load_workbook(payload)
    return jsonable(multitarget_report(wb, analyze(wb), verify=payload.get("verify", True)))


def diff_payload(payload: Dict) -> Dict:
    """{demo} | {workbook, against: 'fixed'} | {old: {...source}, new: {...source}}"""
    from .diff import diff_demo, diff_workbooks
    if payload.get("old") and payload.get("new"):
        return jsonable(diff_workbooks(load_workbook(payload["old"]), load_workbook(payload["new"])))
    if payload.get("demo") and not payload.get("workbook"):
        return jsonable(diff_demo())
    wb = load_workbook(payload)
    rep = analyze(wb)
    return jsonable(diff_workbooks(wb, apply_fixes(wb, rep.fixes)))


def fuzz_payload(payload: Dict) -> Dict:
    from .fuzz import fuzz_workbook
    wb = load_workbook(payload)
    return jsonable(fuzz_workbook(wb, analyze(wb), max_runs=_int(payload, "max_runs", 2000, 1, 5000)))


ROUTES = {
    "multitarget": multitarget_payload, "diff": diff_payload, "fuzz": fuzz_payload,
    "grammar": grammar_payload, "theory": theory_payload,
    "analyze": analyze_payload, "parse": parse_payload, "fix": fix_payload,
    "export": export_payload, "compile": compile_payload, "bench": bench_payload,
    "demo": demo_workbook_payload, "health": health_payload,
}
