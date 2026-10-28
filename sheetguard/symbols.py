"""Symbol table & name resolution - the 'linker' of SheetGuard.

Scopes are nested exactly like a block-structured language:

    builtin scope    SUM, IF, VLOOKUP ...        (the standard library)
      workbook scope   sheet names, workbook-level named ranges
        sheet scope      sheet-local named ranges  (shadow workbook names)

Resolution walks from the innermost scope outwards. Cross-sheet references
(``Rates!B2``) behave like external symbols in separate object files: the
resolver checks the target 'module' (sheet) exists and records the link, so
we can print a link map showing which sheet imports from which.
"""
from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from . import functions
from .ast_nodes import CellRef, FuncCall, NameRef, Node, RangeRef
from .diagnostics import Diagnostic
from .errors import SheetGuardError
from .parser import parse
from .workbook import Cell, Workbook

BUILTIN = "<builtin>"
WORKBOOK = "<workbook>"


@dataclass
class Symbol:
    name: str
    kind: str                         # function | sheet | range | constant
    scope: str                        # <builtin> | <workbook> | sheet name
    definition: str = ""
    target: Optional[Node] = None     # parsed definition for names
    refs: List[str] = field(default_factory=list)    # cells that use this symbol
    info: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"name": self.name, "kind": self.kind, "scope": self.scope,
                "definition": self.definition, "uses": len(self.refs),
                "refs": self.refs[:50], "info": self.info}


class SymbolTable:
    def __init__(self):
        self.scopes: Dict[str, Dict[str, Symbol]] = {BUILTIN: {}, WORKBOOK: {}}
        self.links: Dict[Tuple[str, str], int] = {}       # (from_sheet, to_sheet) -> count
        self.diagnostics: List[Diagnostic] = []

    # -- scope management -------------------------------------------------
    def define(self, sym: Symbol) -> Symbol:
        scope = self.scopes.setdefault(sym.scope, {})
        scope[sym.name.upper()] = sym
        return sym

    def lookup(self, name: str, sheet: Optional[str] = None) -> Optional[Symbol]:
        """Innermost-first lookup: sheet scope -> workbook scope."""
        key = name.upper()
        for scope in ([sheet] if sheet else []) + [WORKBOOK]:
            sym = self.scopes.get(scope, {}).get(key)
            if sym and sym.kind != "sheet":
                return sym
        return None

    def sheet(self, name: str) -> Optional[Symbol]:
        sym = self.scopes[WORKBOOK].get(name.upper())
        return sym if sym and sym.kind == "sheet" else None

    def function(self, name: str) -> Optional[Symbol]:
        key = name.upper()
        if key in self.scopes[BUILTIN]:
            return self.scopes[BUILTIN][key]
        sig = functions.lookup(key)
        if not sig:
            return None
        return self.define(Symbol(sig.name, "function", BUILTIN, sig.description,
                                  info={"category": sig.category, "arity": sig.arity_text(),
                                        "volatile": sig.volatile}))

    def all_symbols(self) -> List[Symbol]:
        out = []
        for scope in self.scopes.values():
            out.extend(scope.values())
        order = {"sheet": 0, "range": 1, "constant": 2, "function": 3}
        return sorted(out, key=lambda s: (order.get(s.kind, 9), s.scope, s.name))

    # -- resolution of one reference ---------------------------------------
    def resolve(self, node: Node, sheet: str):
        """Return a concrete CellRef/RangeRef (with .sheet filled in) for a reference
        node, following named ranges. Returns None if it cannot be resolved."""
        if isinstance(node, NameRef):
            sym = self.lookup(node.name, sheet)
            if not sym or sym.kind != "range" or sym.target is None:
                return None
            if sym.scope not in (WORKBOOK, BUILTIN):
                sheet = sym.scope          # sheet-scoped name: unqualified refs mean that sheet
            node = sym.target
        if isinstance(node, (CellRef, RangeRef)):
            target_sheet = node.sheet or sheet
            if not self.sheet(target_sheet):
                return None
            if isinstance(node, CellRef):
                return CellRef(node.col, node.row, node.abs_col, node.abs_row,
                               self.sheet(target_sheet).name).at(*node.span)
            r = node.normalized()
            r.sheet = self.sheet(target_sheet).name
            return r
        return None

    def to_dict(self) -> dict:
        return {
            "symbols": [s.to_dict() for s in self.all_symbols()],
            "links": [{"from": a, "to": b, "count": n} for (a, b), n in sorted(self.links.items())],
        }


def _split_areas(definition: str) -> List[str]:
    """'A'!$A$1,'B, Inc'!$B$2  ->  two areas (commas inside quotes or brackets ignored)."""
    parts, depth, quote, cur = [], 0, False, ""
    for ch in definition:
        if ch == "'":
            quote = not quote
        elif not quote and ch in "({":
            depth += 1
        elif not quote and ch in ")}":
            depth -= 1
        elif ch == "," and not quote and depth == 0:
            parts.append(cur.strip())
            cur = ""
            continue
        cur += ch
    parts.append(cur.strip())
    return [p for p in parts if p] or [definition]


def build_symbol_table(wb: Workbook) -> SymbolTable:
    """Pass 1: declare sheets & names.  Pass 2: resolve every use in every formula."""
    st = SymbolTable()

    # ---- pass 1: declarations
    for sh in wb.sheets.values():
        st.define(Symbol(sh.name, "sheet", WORKBOOK, f"{sh.max_col}×{sh.max_row} grid",
                         info={"cells": len(sh.cells),
                               "formulas": sum(1 for c in sh.cells.values() if c.is_formula)}))
    for (name, scope), definition in wb.names.items():
        try:
            areas = _split_areas(definition)
            target = parse(areas[0])
            if len(areas) > 1:
                st.diagnostics.append(Diagnostic(
                    "SG103", "info", None, f"named range '{name}' has {len(areas)} areas; "
                    f"SheetGuard uses the first one ({areas[0]})",
                    explanation=f"'{name}' = {definition} is a union of several ranges. Most "
                                f"functions accept it, but SheetGuard analyses only the first "
                                f"area.", fix="No action needed.", phase="symbols"))
        except SheetGuardError as e:
            st.diagnostics.append(Diagnostic(
                "SG103", "error", None, f"named range '{name}' has an invalid definition",
                explanation=e.pretty(), fix="Fix the definition in Data > Named ranges.",
                phase="symbols"))
            continue
        kind = "range" if isinstance(target, (CellRef, RangeRef)) else "constant"
        st.define(Symbol(name, kind, scope or WORKBOOK, definition, target))

    # ---- pass 2: uses
    for cell in wb.formula_cells():
        if cell.ast is None:
            continue
        _resolve_cell(st, wb, cell)
    return st


def _resolve_cell(st: SymbolTable, wb: Workbook, cell: Cell) -> None:
    here = cell.sheet
    for node in cell.ast.walk():
        if isinstance(node, FuncCall):
            sym = st.function(node.name)
            if sym is None:
                sugg = functions.suggest(node.name)
                hint = f" Did you mean {sugg[0]}?" if sugg else ""
                st.diagnostics.append(Diagnostic(
                    "SG101", "error", cell.key, f"unknown function {node.name}()",
                    explanation=f"{node.name} is not a built-in function, so the sheet shows "
                                f"#NAME? here.{hint}",
                    fix=f"Use {sugg[0]}()" if sugg else "Check the spelling of the function name.",
                    suggested_formula=(re.sub(re.escape(node.name) + r"(?=\s*\()", sugg[0],
                                              cell.raw, count=1, flags=re.I)
                                       if sugg else None),
                    phase="symbols"))
                continue
            sym.refs.append(cell.key)
            sig = functions.lookup(node.name)
            n = len(node.args)
            if n < sig.min_args or (sig.max_args is not None and n > sig.max_args):
                st.diagnostics.append(Diagnostic(
                    "SG102", "error", cell.key,
                    f"{sig.name}() takes {sig.arity_text()} argument(s) but was given {n}",
                    explanation=f"A call must match the function's declared signature, just as "
                                f"in any typed language. {sig.name}: {sig.description}.",
                    fix="Add or remove arguments so the call matches the signature.",
                    phase="symbols"))

        elif isinstance(node, NameRef):
            sym = st.lookup(node.name, here)
            if sym is None:
                known = [s.name for sc in (WORKBOOK, here) for s in st.scopes.get(sc, {}).values()
                         if s.kind in ("range", "constant")]
                sugg = difflib.get_close_matches(node.name, known, n=1, cutoff=0.6)
                st.diagnostics.append(Diagnostic(
                    "SG103", "error", cell.key, f"undefined name '{node.name}'",
                    explanation=f"'{node.name}' is used like a named range but no such name is "
                                f"defined, so the sheet shows #NAME? here." +
                                (f" Did you mean '{sugg[0]}'?" if sugg else ""),
                    fix=f"Rename it to '{sugg[0]}'" if sugg else
                        "Define it under Data > Named ranges, or use a cell reference.",
                    suggested_formula=(re.sub(r"(?<![A-Za-z0-9_.!$'\"])" + re.escape(node.name)
                                              + r"(?![A-Za-z0-9_.(!])", sugg[0], cell.raw, count=1)
                                       if sugg else None),
                    phase="symbols"))
            else:
                sym.refs.append(cell.key)
                tgt = sym.target
                if sym.kind == "range" and getattr(tgt, "sheet", None):
                    tsym = st.sheet(tgt.sheet)
                    if tsym is not None:
                        tsym.refs.append(cell.key)
                        if tsym.name != here:
                            k = (here, tsym.name)
                            st.links[k] = st.links.get(k, 0) + 1

        elif isinstance(node, (CellRef, RangeRef)) and node.sheet:
            target = st.sheet(node.sheet)
            if target is None:
                sugg = difflib.get_close_matches(node.sheet, list(wb.sheets), n=1, cutoff=0.5)
                st.diagnostics.append(Diagnostic(
                    "SG104", "error", cell.key, f"sheet '{node.sheet}' does not exist",
                    explanation="The formula points into a sheet that is not in this workbook "
                                "(renamed or deleted?), so the result is #REF!." +
                                (f" Did you mean '{sugg[0]}'?" if sugg else ""),
                    fix=f"Point the reference at '{sugg[0]}'" if sugg else
                        "Fix the sheet name in the reference.",
                    phase="symbols"))
                continue
            target.refs.append(cell.key)
            if target.name != here:
                k = (here, target.name)
                st.links[k] = st.links.get(k, 0) + 1
