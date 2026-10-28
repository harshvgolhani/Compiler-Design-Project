"""Auto-correction: turn diagnostics into concrete edits, apply them, and export
an annotated, corrected copy of the workbook (.xlsx).

Several detectors can propose the same edit for one cell (F8's 0.18 is both a
magic number *and* inconsistent with its column). merge_diagnostics() folds
those into the single most specific diagnostic so the report never says the
same thing twice.
"""
from __future__ import annotations

import copy
from dataclasses import asdict, dataclass
from typing import Dict, List, Optional

from .diagnostics import Diagnostic
from .refs import parse_cell
from .workbook import Workbook, _coerce_literal

# when two detectors disagree on one cell, the more specific one wins
PRIORITY = ["SG101", "SG103", "SG104", "SG302", "SG403", "SG402", "SG405", "SG404", "SG305",
            "SG401", "SG303", "SG301", "SG304", "SG105", "SG501", "SG502"]


def _rank(code: str) -> int:
    return PRIORITY.index(code) if code in PRIORITY else len(PRIORITY)


def merge_diagnostics(diags: List[Diagnostic]) -> List[Diagnostic]:
    by_cell: Dict[str, List[Diagnostic]] = {}
    out = []
    for d in diags:
        if d.cell and d.suggested_formula:
            by_cell.setdefault(d.cell, []).append(d)
        else:
            out.append(d)
    for cell, group in by_cell.items():
        group.sort(key=lambda d: _rank(d.code))
        kept: List[Diagnostic] = []
        for d in group:
            twin = next((k for k in kept if k.suggested_formula == d.suggested_formula), None)
            if twin is None:
                kept.append(d)
            else:
                note = f"Also flagged as: {d.title.lower()} — {d.message}."
                if note not in twin.explanation:
                    twin.explanation = (twin.explanation + " " + note).strip()
                twin.related = sorted(set(twin.related) | set(d.related))
        out.extend(kept)
    return out


@dataclass
class Fix:
    cell: str
    before: object
    after: object
    code: str
    title: str
    reason: str

    def to_dict(self) -> dict:
        return asdict(self)


def plan_fixes(wb: Workbook, diags: List[Diagnostic]) -> List[Fix]:
    best: Dict[str, Diagnostic] = {}
    for d in diags:
        if not (d.cell and d.suggested_formula):
            continue
        if d.cell not in best or _rank(d.code) < _rank(best[d.cell].code):
            best[d.cell] = d
    fixes = []
    for key, d in best.items():
        c = wb.cell(key)
        before = c.raw if c else None
        after = d.suggested_formula
        if not str(after).startswith("="):
            after = _coerce_literal(after)
        if before == after:
            continue
        fixes.append(Fix(key, before, after, d.code, d.title, d.message))
    from .refs import split_key
    fixes.sort(key=lambda f: (split_key(f.cell)[0], split_key(f.cell)[2], split_key(f.cell)[1]))
    return fixes


def apply_fixes(wb: Workbook, fixes: List[Fix]) -> Workbook:
    """Return a corrected deep copy of the workbook."""
    new = copy.deepcopy(wb)
    new.name = wb.name + " (SheetGuard corrected)"
    for f in fixes:
        sheet, _, ref = f.cell.rpartition("!")
        col, row, _, _ = parse_cell(ref)
        old = new.sheets[sheet].get(col, row)
        new.sheets[sheet].set(col, row, f.after, fmt=old.fmt if old else "")
    for c in new.cells():
        c.tokens = c.ast = c.error = None
        if c.is_formula:
            c.value = None
    return new


# ---------------------------------------------------------------- xlsx export
_FILL = {"error": "FECACA", "warning": "FDE68A", "info": "BFDBFE", "fixed": "BBF7D0"}


def _excel_accepts(ast) -> bool:
    """Excel refuses to load a formula whose built-in call has the wrong number of
    arguments ("We found a problem with some content"), so such cells are written as
    text, like unparseable ones. Unknown functions are fine: Excel shows #NAME?."""
    from . import functions
    from .ast_nodes import FuncCall
    for n in ast.walk():
        if isinstance(n, FuncCall) and functions.lookup(n.name) and \
                not functions.arity_ok(n.name, len(n.args)):
            return False
    return True


def export_xlsx(wb: Workbook, path: str, diags: Optional[List[Diagnostic]] = None,
                fixes: Optional[List[Fix]] = None) -> str:
    """Write an annotated copy: every flagged cell is coloured and carries a comment
    with the explanation; fixed cells are green and record the original formula.
    A 'SheetGuard Report' sheet lists every finding."""
    try:
        import openpyxl
        from openpyxl.comments import Comment
        from openpyxl.styles import Alignment, Font, PatternFill
    except ImportError as e:  # pragma: no cover
        raise RuntimeError("Exporting .xlsx needs openpyxl:  pip install openpyxl") from e

    plain = diags is None
    diags = diags or []
    fixes = fixes or []
    fixed = {f.cell: f for f in fixes}
    target = apply_fixes(wb, fixes) if fixes else wb
    book = openpyxl.Workbook()
    book.remove(book.active)
    by_cell: Dict[str, List[Diagnostic]] = {}
    for d in diags:
        if d.cell:
            by_cell.setdefault(d.cell, []).append(d)

    try:
        from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
    except ImportError:  # pragma: no cover
        import re
        ILLEGAL_CHARACTERS_RE = re.compile(r"[\000-\010]|[\013-\014]|[\016-\037]")
    from .parser import parse as _parse
    fills = {k: PatternFill("solid", fgColor=v) for k, v in _FILL.items()}
    bold = Font(bold=True)
    used_titles = set()

    def title_for(name: str) -> str:
        t = "".join("_" if ch in '[]:*?/\\' else ch for ch in name).strip("'")[:31] or "Sheet"
        base, i = t, 2
        while t.lower() in used_titles:
            t = f"{base[:28]}~{i}"
            i += 1
        used_titles.add(t.lower())
        return t

    for sh in target.sheets.values():
        ws = book.create_sheet(title_for(sh.name))
        for c in sh:
            raw = c.raw
            as_text = False
            if isinstance(raw, str):
                raw = ILLEGAL_CHARACTERS_RE.sub("", raw)
                if c.is_formula:
                    try:
                        as_text = not _excel_accepts(_parse(raw))
                    except Exception:            # a broken formula would corrupt the file
                        as_text = True
            x = ws.cell(row=c.row, column=c.col, value=raw)
            if as_text:
                x.data_type = "s"
            if c.fmt:
                x.number_format = c.fmt.replace('"$"', "$")
            if c.row == 1:
                x.font = bold
            notes = []
            if c.key in fixed:
                f = fixed[c.key]
                x.fill = fills["fixed"]
                notes.append(f"FIXED by SheetGuard ({f.code} {f.title})\nwas: {f.before}\n"
                             f"now: {f.after}")
            elif c.key in by_cell:
                sev = min(by_cell[c.key], key=lambda d: ["error", "warning", "info"]
                          .index(d.severity)).severity
                x.fill = fills[sev]
            for d in by_cell.get(c.key, []):
                if c.key in fixed and d.suggested_formula:
                    continue
                notes.append(f"{d.severity.upper()} {d.code} {d.title}: {d.message}\n"
                             f"Why: {d.explanation}\nFix: {d.fix}")
            if notes:
                x.comment = Comment(ILLEGAL_CHARACTERS_RE.sub("", "\n\n".join(notes))[:2000],
                                    "SheetGuard", width=420, height=220)
        for col in range(1, sh.max_col + 1):
            ws.column_dimensions[openpyxl.utils.get_column_letter(col)].width = 16
        ws.column_dimensions["A"].width = 26
    for (name, scope), definition in wb.names.items():
        try:
            from openpyxl.workbook.defined_name import DefinedName
            from .ast_nodes import CellRef, RangeRef
            target = _parse(definition if definition.startswith("=") else "=" + definition)
            if isinstance(target, CellRef):          # spreadsheet names are absolute; a
                target.abs_col = target.abs_row = True   # relative one would move in Excel
                definition = target.unparse()
            elif isinstance(target, RangeRef):
                target.abs_col1 = target.abs_col2 = target.abs_row1 = target.abs_row2 = True
                definition = target.unparse()
            dn = DefinedName(name, attr_text=definition)
            if hasattr(book.defined_names, "add"):
                book.defined_names.add(dn)
            else:  # pragma: no cover - older openpyxl
                book.defined_names.append(dn)
        except Exception:
            pass

    if plain:
        book.save(path)
        return path
    rep = book.create_sheet(title_for("SheetGuard Report"), 0)
    rep.append(["SheetGuard verification report", wb.name])
    rep["A1"].font = Font(bold=True, size=14)
    rep.append([])
    rep.append(["Severity", "Cell", "Code", "Issue", "Explanation", "Fix", "Auto-fixed?"])
    for cell in rep[3]:
        cell.font = Font(bold=True)
    row = 3
    for d in diags:
        rep.append([(" " if str(v).startswith("=") else "") + ILLEGAL_CHARACTERS_RE.sub("", str(v)) for v in
                    (d.severity, d.cell or "", d.code, f"{d.title}: {d.message}", d.explanation,
                     d.fix, "yes" if d.cell in fixed and d.suggested_formula else "")])
        row += 1
        rep.cell(row=row, column=1).fill = fills[d.severity]
    for col, w in zip("ABCDEFG", (10, 14, 8, 50, 80, 45, 11)):
        rep.column_dimensions[col].width = w
    for row in rep.iter_rows(min_row=4):
        for x in row:
            x.alignment = Alignment(wrap_text=True, vertical="top")
    book.save(path)
    return path
