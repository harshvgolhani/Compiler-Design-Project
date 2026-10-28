"""Semantic diff between two versions of a workbook.

A text diff of formulas is noisy (=SUM( B2:B8 ) vs =SUM(B2:B8) "differ") and
blind to meaning. SheetGuard compares the *compiled* forms instead:

  * cells are classified: added, removed, value changed, formula replaced by a
    value (or the reverse), formula changed, or cosmetic only (same AST)
  * for changed formulas, the two ASTs are aligned top-down and only the
    *minimal changed subtrees* are reported, each with a semantic label:
        range resized          SUM(B2:B8) → SUM(B2:B9)   (+1 row)
        reference changed      C5 → C6                   (now same row)
        constant → reference   0.18 → TaxRate
        function renamed       SUMM → SUM
        operator changed       + → *
  * formulas that moved are matched by their position-independent R1C1 form
  * both versions are evaluated, so the diff also says which *outputs* changed
  * both versions are analysed, so it lists issues resolved and introduced
"""
from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from .ast_nodes import (ArrayLit, BinOp, CellRef, FuncCall, NameRef, Node, Number, Percent,
                        RangeRef, String, UnaryOp)
from .evaluator import Interpreter
from .graph import _sort_key
from .pipeline import analyze
from .runtime import XLError, display
from .transforms import r1c1
from .workbook import Workbook


def _head(n: Node):
    if isinstance(n, BinOp):
        return ("BinOp", n.op)
    if isinstance(n, UnaryOp):
        return ("Unary", n.op)
    if isinstance(n, Percent):
        return ("Percent",)
    if isinstance(n, FuncCall):
        return ("Call", n.name, len(n.args))
    if isinstance(n, ArrayLit):
        return ("Array", len(n.rows), len(n.rows[0]) if n.rows else 0)
    return None                                     # leaves are compared as a whole


def _classify(a: Node, b: Node) -> Tuple[str, str]:
    if isinstance(a, RangeRef) and isinstance(b, RangeRef) and a.sheet == b.sheet:
        if (a.col1, a.row1) == (b.col1, b.row1) and a.row2 is not None and b.row2 is not None:
            dr = (b.row2 - b.row1) - (a.row2 - a.row1)
            dc = (b.col2 - b.col1) - (a.col2 - a.col1)
            if dc == 0 and dr:
                return "range resized", f"{'+' if dr > 0 else ''}{dr} row(s)"
            if dr == 0 and dc:
                return "range resized", f"{'+' if dc > 0 else ''}{dc} column(s)"
        return "range changed", ""
    if isinstance(a, CellRef) and isinstance(b, CellRef):
        if a.sheet == b.sheet and a.col == b.col and a.row != b.row:
            return "reference changed", f"{b.row - a.row:+d} row(s)"
        if a.sheet == b.sheet and a.row == b.row and a.col != b.col:
            return "reference changed", f"{b.col - a.col:+d} column(s)"
        return "reference changed", ""
    if isinstance(a, (Number, Percent)) and isinstance(b, (NameRef, CellRef)):
        return "constant → reference", "hard-coded value replaced by an input"
    if isinstance(a, (NameRef, CellRef)) and isinstance(b, (Number, Percent)):
        return "reference → constant", "an input was hard-coded"
    if isinstance(a, NameRef) and isinstance(b, NameRef):
        return "name changed", ""
    if isinstance(a, Number) and isinstance(b, Number):
        return "constant changed", ""
    if isinstance(a, FuncCall) and isinstance(b, FuncCall) and \
            [x.unparse() for x in a.args] == [x.unparse() for x in b.args]:
        return "function renamed", ""
    if isinstance(a, BinOp) and isinstance(b, BinOp) and a.left.unparse() == b.left.unparse() \
            and a.right.unparse() == b.right.unparse():
        return "operator changed", f"{a.op} → {b.op}"
    return "expression rewritten", ""


def ast_changes(a: Node, b: Node) -> List[dict]:
    """Minimal changed subtrees between two ASTs (top-down alignment)."""
    if a.unparse() == b.unparse():
        return []
    ha, hb = _head(a), _head(b)
    if ha is not None and ha == hb:
        ka, kb = a.children(), b.children()
        if len(ka) == len(kb) and ka:
            out = []
            for x, y in zip(ka, kb):
                out += ast_changes(x, y)
            return out
    kind, detail = _classify(a, b)
    return [{"from": a.unparse(), "to": b.unparse(), "kind": kind, "detail": detail}]


def _norm(v):
    if isinstance(v, XLError):
        return v.code
    return v


def _equal(a, b) -> bool:
    if isinstance(a, float) and isinstance(b, float):
        return abs(a - b) <= 1e-9 * max(1.0, abs(a), abs(b))
    return _norm(a) == _norm(b)


def diff_workbooks(old: Workbook, new: Workbook) -> Dict:
    rep_a, rep_b = analyze(old), analyze(new)
    va = Interpreter(old, rep_a.symbols, rep_a.graph).run()
    vb = Interpreter(new, rep_b.symbols, rep_b.graph).run()
    keys = sorted({c.key for c in old.cells()} | {c.key for c in new.cells()}, key=_sort_key)
    cells = []
    counts: Dict[str, int] = {}
    edited = set()
    for k in keys:
        a, b = old.cell(k), new.cell(k)
        entry = None
        if a is None:
            entry = {"status": "added", "new": b.raw}
        elif b is None:
            entry = {"status": "removed", "old": a.raw}
        elif a.raw == b.raw:
            continue
        elif not a.is_formula and not b.is_formula:
            entry = {"status": "value changed", "old": a.raw, "new": b.raw}
            if isinstance(a.raw, str) and isinstance(b.raw, str) and \
                    a.raw.strip().lower() == b.raw.strip().lower():
                entry["note"] = "whitespace / capitalisation only"
        elif a.is_formula and not b.is_formula:
            entry = {"status": "formula → value", "old": a.raw, "new": b.raw}
        elif not a.is_formula and b.is_formula:
            entry = {"status": "value → formula", "old": a.raw, "new": b.raw}
        else:
            if a.ast is not None and b.ast is not None and a.ast.unparse() == b.ast.unparse():
                entry = {"status": "cosmetic", "old": a.raw, "new": b.raw,
                         "note": "same AST: spacing, case or redundant brackets only"}
            elif a.ast is None or b.ast is None:
                entry = {"status": "formula changed", "old": a.raw, "new": b.raw,
                         "changes": [{"from": a.raw, "to": b.raw,
                                      "kind": "syntax repaired" if a.ast is None else
                                      "syntax broken", "detail": ""}]}
            else:
                entry = {"status": "formula changed", "old": a.raw, "new": b.raw,
                         "changes": ast_changes(a.ast, b.ast)}
        entry["cell"] = k
        if entry["status"] != "cosmetic":
            edited.add(k)
        if k in va or k in vb:
            entry["old_value"] = display(va.get(k))
            entry["new_value"] = display(vb.get(k))
        counts[entry["status"]] = counts.get(entry["status"], 0) + 1
        cells.append(entry)

    # moved formulas: a removed formula re-appearing elsewhere with the same R1C1 form
    removed = [e for e in cells if e["status"] == "removed" and str(e.get("old", "")).startswith("=")]
    added = [e for e in cells if e["status"] == "added" and str(e.get("new", "")).startswith("=")]
    for r in removed:
        ca = old.cell(r["cell"])
        if ca.ast is None:
            continue
        sig = r1c1(ca.ast, ca.col, ca.row)
        for ad in added:
            cb = new.cell(ad["cell"])
            if cb.ast is not None and r1c1(cb.ast, cb.col, cb.row) == sig and "moved_from" not in ad:
                ad["status"] = "moved"
                ad["moved_from"] = r["cell"]
                r["status"] = "moved away"
                r["moved_to"] = ad["cell"]
                break

    # consequences: formula cells whose value changed although their formula did not
    ripple = []
    for c in new.formula_cells():
        if c.key in edited or old.cell(c.key) is None:
            continue
        if not _equal(va.get(c.key), vb.get(c.key)):
            causes = sorted(edited & set(rep_b.graph.precedents(c.key)), key=_sort_key)
            ripple.append({"cell": c.key, "old_value": display(va.get(c.key)),
                           "new_value": display(vb.get(c.key)), "because_of": causes[:6]})

    da = {(d.code, d.cell): d for d in rep_a.diagnostics}
    db = {(d.code, d.cell): d for d in rep_b.diagnostics}
    resolved = [{"code": k[0], "cell": k[1], "title": da[k].title, "message": da[k].message}
                for k in sorted(set(da) - set(db), key=lambda x: (x[1] or "", x[0]))]
    introduced = [{"code": k[0], "cell": k[1], "title": db[k].title, "message": db[k].message}
                  for k in sorted(set(db) - set(da), key=lambda x: (x[1] or "", x[0]))]
    return {
        "old": old.name, "new": new.name,
        "counts": counts, "cells": cells, "ripple": ripple,
        "issues": {"resolved": resolved, "introduced": introduced,
                   "persisting": len(set(da) & set(db)),
                   "old_counts": rep_a.counts(), "new_counts": rep_b.counts()},
        "summary": _summary(counts, ripple, resolved, introduced),
    }


def _summary(counts, ripple, resolved, introduced) -> str:
    parts = []
    n = sum(v for k, v in counts.items() if k != "cosmetic")
    parts.append(f"{n} cell(s) changed meaningfully")
    if counts.get("cosmetic"):
        parts.append(f"{counts['cosmetic']} only cosmetically")
    parts.append(f"{len(ripple)} other cell(s) changed value as a consequence")
    parts.append(f"{len(resolved)} issue(s) resolved, {len(introduced)} introduced")
    return "; ".join(parts) + "."


def diff_demo() -> Dict:
    """The demo comparison: the buggy workbook vs the auto-fixed one."""
    from .demo import build_demo_workbook
    from .fixes import apply_fixes
    old = build_demo_workbook()
    rep = analyze(old)
    new = apply_fixes(old, rep.fixes)
    new.sheets["Sales"].set(1, 1, "Region ")                       # a cosmetic-only change
    c = new.cell("Summary!B3")
    new.sheets["Summary"].set(2, 3, "= B2 * CommissionRate", fmt=c.fmt)   # cosmetic formula edit
    return diff_workbooks(old, new)
