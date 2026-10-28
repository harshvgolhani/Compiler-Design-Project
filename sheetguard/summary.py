"""Deliverables: a plain-English summary, a bug-impact report, and a health score.

The summary is generated from *structural features* that the compiler has
already computed (symbol table, DAG, types, diagnostics). It is template text,
so it is deterministic and every claim in it can be traced back to an analysis.
An LLM is optional and, if configured, only rephrases the final sentence. It
never takes part in the analysis.

The impact report answers the question a manager asks: "so what?". It
evaluates the workbook as written and again after the automatic fixes, then
lists the outputs whose values change.
"""
from __future__ import annotations

import json
import os
import urllib.request
from typing import Dict, List, Optional

from . import runtime as rt
from .evaluator import Interpreter
from .fixes import apply_fixes
from .graph import _sort_key
from .refs import split_key


def health_score(counts: Dict[str, int]) -> Dict:
    raw = 100 - 8 * counts.get("error", 0) - 3 * counts.get("warning", 0) \
        - 0.5 * counts.get("info", 0)
    score = int(max(5, min(100, round(raw))))
    grade = "A" if score >= 90 else "B" if score >= 75 else "C" if score >= 60 else \
        "D" if score >= 40 else "F"
    return {"score": score, "grade": grade}


def _label(wb, key: str) -> str:
    sheet, col, row = split_key(key)
    sh = wb.sheets[sheet]
    row_label = next((c.raw for c in sh if c.row == row and c.col < col and
                      isinstance(c.raw, str) and not c.is_formula), "")
    col_label = next((c.raw for c in sh if c.col == col and c.row < row and
                      isinstance(c.raw, str) and not c.is_formula), "")
    if row_label and col_label and col_label.lower() not in ("value", "metric"):
        return f"{row_label} — {col_label}"
    return row_label or col_label or key


def _fmt_value(v, fmt: str = "") -> str:
    if isinstance(v, rt.XLError):
        return v.code
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, (int, float)):
        if "%" in (fmt or ""):
            return f"{v * 100:.1f}%"
        if "$" in (fmt or ""):
            return f"${v:,.2f}"
        if "₹" in (fmt or ""):
            return f"₹{v:,.0f}"
        return f"{v:,.2f}".rstrip("0").rstrip(".") if abs(v) < 1e15 else str(v)
    return "" if v is None else str(v)


def compute_values(wb, rep) -> Dict[str, object]:
    return Interpreter(wb, rep.symbols, rep.graph).run()


def impact_report(wb, rep, values: Optional[Dict] = None) -> Dict:
    """Evaluate before and after the auto-fixes; report changed outputs."""
    from .pipeline import analyze
    before = values or compute_values(wb, rep)
    if not rep.fixes:
        return {"changes": [], "after_counts": rep.counts(), "after_values": before}
    fixed_wb = apply_fixes(wb, rep.fixes)
    after_rep = analyze(fixed_wb)
    after = compute_values(fixed_wb, after_rep)
    fixed_cells = {f.cell for f in rep.fixes}
    sinks = {k for k in rep.graph.nodes if not rep.graph.succs.get(k)}
    changes = []
    for c in wb.formula_cells():
        k = c.key
        b, a = before.get(k), after.get(k)
        if isinstance(b, float) and isinstance(a, float) and abs(a - b) <= 1e-9 * max(1, abs(b)):
            continue
        if b == a or (str(b) == str(a)):
            continue
        is_output = k in sinks or c.sheet.lower() in ("summary", "dashboard", "report") or \
            "total" in _label(wb, k).lower()
        rel = None
        if isinstance(b, float) and isinstance(a, float) and b != 0:
            rel = (a - b) / abs(b)
        changes.append({
            "cell": k, "label": _label(wb, k), "before": _fmt_value(b, c.fmt),
            "after": _fmt_value(a, c.fmt), "delta_pct": None if rel is None else round(rel * 100, 2),
            "output": is_output, "fixed_here": k in fixed_cells,
        })
    report_sheets = ("summary", "dashboard", "report", "overview")
    changes.sort(key=lambda x: (not x["output"],
                                x["cell"].split("!")[0].lower() not in report_sheets,
                                -abs(x["delta_pct"] or 0), _sort_key(x["cell"])))
    return {"changes": changes, "after_counts": after_rep.counts(),
            "after_score": health_score(after_rep.counts()),
            "remaining": [d.to_dict() for d in after_rep.diagnostics]}


def structural_features(wb, rep) -> Dict:
    g = rep.graph
    formulas = [c for c in wb.formula_cells()]
    inputs = [c for c in wb.cells() if not c.is_formula and isinstance(c.raw, (int, float))
              and not isinstance(c.raw, bool)]
    influence = []
    for c in inputs:
        n = len(g.dependents(c.key))
        if n:
            influence.append((n, c.key))
    influence.sort(reverse=True)
    biggest = max(formulas, key=lambda c: sum(1 for _ in c.ast.walk()) if c.ast else 0,
                  default=None)
    names = [s.name for s in rep.symbols.all_symbols() if s.kind in ("range", "constant")]
    flow = []
    for (a, b), n in sorted(rep.symbols.links.items(), key=lambda x: -x[1]):
        flow.append(f"{b} → {a}")
    by_cat: Dict[str, int] = {}
    for d in rep.diagnostics:
        by_cat[d.title] = by_cat.get(d.title, 0) + 1
    return {
        "sheets": list(wb.sheets), "formulas": len(formulas), "inputs": len(inputs),
        "named_ranges": names, "levels": len(g.formula_levels()), "cycles": len(g.cycles),
        "most_influential": [{"cell": k, "label": _label(wb, k), "affects": n}
                             for n, k in influence[:3]],
        "most_complex": {"cell": biggest.key, "formula": biggest.raw} if biggest else None,
        "flow": flow, "issues_by_category": by_cat,
    }


def summarize(wb, rep, impact: Optional[Dict] = None) -> Dict:
    f = structural_features(wb, rep)
    cnt = rep.counts()
    hs = health_score(cnt)
    parts = []
    names = f["named_ranges"]
    parts.append(
        f"“{wb.name}” is a {len(f['sheets'])}-sheet model ({', '.join(f['sheets'])}) with "
        f"{f['formulas']} formulas driven by {f['inputs']} numeric inputs"
        + (f" and {len(names)} named assumptions ({', '.join(names[:4])}"
           f"{', …' if len(names) > 4 else ''})" if names else "") + ".")
    if f["flow"]:
        parts.append(f"Data flows {'; '.join(f['flow'][:3])}, through a dependency chain "
                     f"{f['levels']} levels deep.")
    if f["most_influential"]:
        m = f["most_influential"][0]
        parts.append(f"The most influential input is {m['label']} ({m['cell']}): changing it "
                     f"affects {m['affects']} cells.")
    issues = cnt["error"] + cnt["warning"]
    if issues:
        top = [d for d in rep.diagnostics if d.severity == "error"][:2] or rep.diagnostics[:2]
        tops = "; ".join(f"{d.title.lower()} at {d.cell}" for d in top)
        parts.append(f"SheetGuard found {cnt['error']} error(s) and {cnt['warning']} warning(s)"
                     f" — most serious: {tops}. {len(rep.fixes)} can be fixed automatically.")
    else:
        parts.append("SheetGuard found no errors or warnings.")
    final = None
    if impact and impact.get("changes"):
        outs = [c for c in impact["changes"] if c["output"]] or impact["changes"]
        c0 = outs[0]
        final = (f"Left uncorrected, the bugs change {len(outs)} reported figure(s); for example "
                 f"{c0['label']} shows {c0['before']} but should be {c0['after']}.")
    elif issues == 0:
        final = "The model is internally consistent and safe to rely on."
    if final:
        final = polish_sentence(final) or final
        parts.append(final)
    return {"text": " ".join(parts), "features": f, "health": hs, "llm_used": bool(
        os.environ.get("SHEETGUARD_LLM") and os.environ.get("ANTHROPIC_API_KEY"))}


def polish_sentence(sentence: str) -> Optional[str]:
    """Optional: let an LLM rephrase ONLY the final sentence. Enabled when both
    SHEETGUARD_LLM=1 and ANTHROPIC_API_KEY are set; otherwise returns None."""
    if not (os.environ.get("SHEETGUARD_LLM") and os.environ.get("ANTHROPIC_API_KEY")):
        return None
    try:
        body = json.dumps({
            "model": os.environ.get("SHEETGUARD_LLM_MODEL", "claude-haiku-4-5"),
            "max_tokens": 120,
            "messages": [{"role": "user", "content":
                          "Rephrase this sentence for a busy finance manager. Keep every number "
                          "exactly as written, one sentence, no preamble:\n\n" + sentence}],
        }).encode()
        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages", data=body, method="POST",
            headers={"content-type": "application/json",
                     "x-api-key": os.environ["ANTHROPIC_API_KEY"],
                     "anthropic-version": "2023-06-01"})
        with urllib.request.urlopen(req, timeout=8) as r:
            out = json.loads(r.read())["content"][0]["text"].strip()
        return out if out else None
    except Exception:
        return None
