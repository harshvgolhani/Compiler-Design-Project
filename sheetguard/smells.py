"""Phase 6 - Bug / smell detection (static analysis over AST + DAG).

The detectors encode the classic spreadsheet-error taxonomy (Panko, EuSpRIG)
as compiler analyses:

  SG401 inconsistent formula   a cell whose R1C1 form differs from the formulas
                               copied around it  (copy-paste / drag errors)
  SG402 off-by-one range       SUM(B2:B8) when the data block runs to B9
  SG403 hard-coded constant    a literal (0.18) that duplicates an input (TaxRate)
  SG404 overwritten formula    a typed number in the middle of a formula column
  SG405 double counting        an aggregate whose range includes its own subtotal
  SG105 empty reference        a formula that reads a blank cell
"""
from __future__ import annotations

from collections import Counter
from typing import Dict, List, Optional, Tuple

from .ast_nodes import CellRef, FuncCall, NameRef, Node, Number, Percent, RangeRef, UnaryOp
from .diagnostics import Diagnostic
from .graph import DependencyGraph
from .refs import a1, make_key
from .symbols import SymbolTable
from .transforms import clone, map_tree, r1c1, shift
from .workbook import Cell, Sheet, Workbook

AGGREGATES = {"SUM", "AVERAGE", "MIN", "MAX", "COUNT", "COUNTA", "MEDIAN", "PRODUCT",
              "STDEV", "STDEV.S", "VAR", "SUBTOTAL", "SUMPRODUCT"}
# argument positions that are 'structural' (indices, digits, counts) - not magic numbers
STRUCTURAL_ARGS = {
    "ROUND": {1}, "ROUNDUP": {1}, "ROUNDDOWN": {1}, "VLOOKUP": {2, 3}, "HLOOKUP": {2, 3},
    "INDEX": {1, 2}, "MATCH": {2}, "LEFT": {1}, "RIGHT": {1}, "MID": {1, 2}, "CHOOSE": {0},
    "DATE": {0, 1, 2}, "TEXT": {1}, "REPT": {1}, "OFFSET": {1, 2, 3, 4}, "EDATE": {1},
    "EOMONTH": {1}, "LOG": {1}, "XLOOKUP": {4, 5}, "WEEKDAY": {1}, "LARGE": {1}, "SMALL": {1},
    "CEILING": {1}, "FLOOR": {1}, "MOD": {1}, "POWER": {1}, "RANDBETWEEN": {0, 1},
    "SUBTOTAL": {0},
}
HARMLESS = {0.0, 1.0, -1.0, 2.0, 100.0}


class SmellDetector:
    def __init__(self, wb: Workbook, symbols: SymbolTable, graph: DependencyGraph,
                 prior: List[Diagnostic] | None = None):
        self.wb, self.symbols, self.graph = wb, symbols, graph
        self.diagnostics: List[Diagnostic] = []
        self.prior = prior or []

    def add(self, code, severity, cell, message, explanation, fix, suggested=None, related=None):
        self.diagnostics.append(Diagnostic(code, severity, cell, message, explanation, fix,
                                           suggested, related or [], phase="smells"))

    def run(self) -> "SmellDetector":
        for sh in self.wb.sheets.values():
            self._consistency(sh, vertical=True)
            self._consistency(sh, vertical=False)
        for cell in self.wb.formula_cells():
            if cell.ast is None:
                continue
            self._ranges(cell)
            self._magic_numbers(cell)
        self._empty_refs()
        return self

    # ------------------------------------------------------------ helpers
    def _label(self, sh: Sheet, col: int, row: int, vertical: bool) -> str:
        """Human label for a data row/column (first text cell in that row/column)."""
        if vertical:
            for c in range(1, col):
                x = sh.get(c, row)
                if x and isinstance(x.raw, str) and not x.is_formula:
                    return x.raw
        else:
            for r in range(1, row):
                x = sh.get(col, r)
                if x and isinstance(x.raw, str) and not x.is_formula:
                    return x.raw
        return ""

    def _covers(self, cell: Cell, keys: set, exclude: Optional[str] = None) -> int:
        """How many of `keys` (other than `exclude`) does any range in `cell`'s formula cover?"""
        n = 0
        for node in cell.ast.walk():
            if isinstance(node, RangeRef):
                ref = self.symbols.resolve(node, cell.sheet)
                if ref is not None:
                    hit = keys.intersection(self.graph.expand(ref))
                    n += len(hit) - (1 if exclude in hit else 0)
        return n

    def _is_aggregate(self, cell: Optional[Cell]) -> bool:
        return bool(cell and cell.ast is not None and any(
            isinstance(n, FuncCall) and n.name in AGGREGATES and
            any(isinstance(a, RangeRef) for a in n.args) for n in cell.ast.walk()))

    @staticmethod
    def _is_number_cell(c: Optional[Cell]) -> bool:
        return bool(c) and (c.is_formula or (isinstance(c.raw, (int, float))
                                             and not isinstance(c.raw, bool)))

    # ------------------------------------------------------------ SG401 / SG404
    def _consistency(self, sh: Sheet, vertical: bool) -> None:
        lines: Dict[int, List[Tuple[int, Cell]]] = {}
        for (c, r), cell in sh.cells.items():
            line, pos = (c, r) if vertical else (r, c)
            lines.setdefault(line, []).append((pos, cell))
        for line, items in lines.items():
            items.sort(key=lambda x: x[0])
            run: List[Tuple[int, Cell]] = []
            for pos, cell in items:
                if run and pos != run[-1][0] + 1:
                    self._check_run(sh, run, vertical)
                    run = []
                run.append((pos, cell))
            if run:
                self._check_run(sh, run, vertical)

    def _check_run(self, sh: Sheet, run, vertical: bool) -> None:
        keys = {c.key for _, c in run}
        formulas = [(p, c) for p, c in run if c.is_formula and c.ast is not None
                    and self._covers(c, keys, c.key) < 2]       # skip totals over the run
        if len(formulas) < 3:
            return
        sigs = {c.key: r1c1(c.ast, c.col, c.row) for _, c in formulas}
        dominant, count = Counter(sigs.values()).most_common(1)[0]
        if count < 3 or count < 0.6 * len(formulas):
            return
        model = next(c for _, c in formulas if sigs[c.key] == dominant)
        dom_positions = [p for p, c in formulas if sigs[c.key] == dominant]
        lo, hi = min(dom_positions), max(dom_positions)
        axis = "column" if vertical else "row"
        where = a1(model.col, model.row).rstrip("0123456789") if vertical else str(model.row)

        def expected_for(cell: Cell) -> str:
            return "=" + shift(model.ast, cell.col - model.col, cell.row - model.row).unparse()

        dom_cells = [x for q, x in formulas if sigs[x.key] == dominant]

        def nearest(pos):
            return min(dom_cells, key=lambda x: abs((x.row if vertical else x.col) - pos))

        for p, c in formulas:
            if sigs[c.key] == dominant:
                continue
            want = expected_for(c)
            near = nearest(p)
            self.add("SG401", "warning", c.key,
                     f"formula differs from the {count} similar formulas in its {axis}",
                     explanation=f"The other {count} cells in this {axis} all follow one pattern "
                                 f"(relative form {dominant}; e.g. {near.address} is {near.raw}), "
                                 f"but {c.address} has {c.raw}. This is the signature of a "
                                 f"copy-paste or drag-fill mistake.",
                     fix=f"Use {want} to match the pattern.", suggested=want,
                     related=[x.key for _, x in formulas if sigs[x.key] == dominant][:6])

        # constants sitting inside the formula pattern = overwritten formulas
        by_pos = {q: x for q, x in formulas}
        for p, c in run:
            if c.is_formula or not self._is_number_cell(c):
                continue
            if not (lo <= p <= hi + 1):
                continue
            neighbours = [x for q, x in ((p - 1, by_pos.get(p - 1)), (p + 1, by_pos.get(p + 1)))
                          if x is not None and sigs[x.key] == dominant]
            if not neighbours:
                continue
            want = expected_for(c)
            self.add("SG404", "warning", c.key,
                     f"hard-typed value {c.raw} in a {axis} of formulas",
                     explanation=f"{c.address} contains the fixed number {c.raw}, while the "
                                 f"cells around it compute a formula (e.g. {neighbours[0].address}"
                                 f" is {neighbours[0].raw}). Someone probably "
                                 f"pasted a value over the formula, so this cell will no longer "
                                 f"update when its inputs change.",
                     fix=f"Restore the formula {want}.", suggested=want,
                     related=[x.key for x in neighbours])

    # ------------------------------------------------------------ SG402 / SG405
    def _ranges(self, cell: Cell) -> None:
        for node in cell.ast.walk():
            if not (isinstance(node, FuncCall) and node.name in AGGREGATES):
                continue
            for arg in node.args:
                if not isinstance(arg, RangeRef):
                    continue
                ref = self.symbols.resolve(arg, cell.sheet)
                if ref is None or ref.row1 is None or ref.col1 is None:
                    continue
                self._off_by_one(cell, node, arg, ref)
                self._double_count(cell, node, arg, ref)

    def _off_by_one(self, cell: Cell, call: FuncCall, arg: RangeRef, ref: RangeRef) -> None:
        sh = self.wb.sheets[ref.sheet]
        rows, cols = ref.shape
        if cols == 1 and rows >= 2:
            vertical = True
        elif rows == 1 and cols >= 2:
            vertical = False
        else:
            return
        inside = list(self.graph.expand(ref))
        if len(inside) < 2:
            return

        def at(k):
            return sh.get(ref.col1, k) if vertical else sh.get(k, ref.row1)

        first, last = (ref.row1, ref.row2) if vertical else (ref.col1, ref.col2)
        sample_last, sample_first = at(last), at(first)
        for pos, sample, side in ((last + 1, sample_last, "end"), (first - 1, sample_first,
                                                                    "start")):
            if pos < 1:
                continue
            nxt = at(pos)
            if nxt is None or nxt.key == cell.key or not self._is_number_cell(nxt):
                continue
            if self._is_aggregate(nxt) or sample is None:
                continue
            new = arg.normalized()
            if vertical:
                if side == "end":
                    new.row2 += 1
                else:
                    new.row1 -= 1
            elif side == "end":
                new.col2 += 1
            else:
                new.col1 -= 1
            label = self._label(sh, nxt.col, nxt.row, vertical)
            what = f"{'row' if vertical else 'column'} {nxt.row if vertical else nxt.address}"
            fixed = self._replace(cell, arg, new)
            self.add("SG402", "warning", cell.key,
                     f"{call.name}({arg.unparse()}) misses {nxt.address} at the {side} of the data",
                     explanation=f"The range {arg.unparse()} stops one cell short: {what}"
                                 f"{' (' + label + ')' if label else ''} holds {nxt.raw} and "
                                 f"belongs to the same block of data, but is left out of the "
                                 f"{call.name.lower()}.",
                     fix=f"Extend the range to {new.unparse()}.", suggested=fixed,
                     related=[nxt.key])
            return

    def _double_count(self, cell: Cell, call: FuncCall, arg: RangeRef, ref: RangeRef) -> None:
        inside = set(self.graph.expand(ref)) - {cell.key}
        for key in sorted(inside):
            sub = self.wb.cell(key)
            if not self._is_aggregate(sub):
                continue
            if self._covers(sub, inside - {key}) < 2:
                continue
            new = arg.normalized()
            fixable = False
            if ref.shape[1] == 1 and sub.row == ref.row2:
                new.row2 -= 1
                fixable = True
            elif ref.shape[0] == 1 and sub.col == ref.col2:
                new.col2 -= 1
                fixable = True
            label = self._label(self.wb.sheets[sub.sheet], sub.col, sub.row, True)
            self.add("SG405", "warning", cell.key,
                     f"{call.name}({arg.unparse()}) includes the subtotal {sub.address}",
                     explanation=f"{sub.address}{' (' + label + ')' if label else ''} is itself "
                                 f"{sub.raw} — a total of other cells in the same range. "
                                 f"Including it counts every value twice, so the "
                                 f"{call.name.lower()} is inflated.",
                     fix=(f"Shrink the range to {new.unparse()}." if fixable else
                          f"Exclude {sub.address} from the range."),
                     suggested=self._replace(cell, arg, new) if fixable else None,
                     related=[key])
            return

    # ------------------------------------------------------------ SG403
    def _input_values(self) -> List[Tuple[float, str, str]]:
        """(value, replacement text, description) for every named numeric input."""
        if hasattr(self, "_inputs"):
            return self._inputs
        out = []
        for sym in self.symbols.all_symbols():
            if sym.kind == "range" and isinstance(sym.target, CellRef):
                ref = self.symbols.resolve(sym.target, sym.target.sheet or "")
                c = self.wb.cell(make_key(ref.sheet, ref.col, ref.row)) if ref else None
                if c is not None and isinstance(c.raw, (int, float)) and not isinstance(c.raw,
                                                                                          bool):
                    out.append((float(c.raw), sym.name, f"the named input {sym.name} "
                                f"({c.key})"))
        self._inputs = out
        return out

    def _magic_numbers(self, cell: Cell) -> None:
        if not any(isinstance(n, (CellRef, RangeRef, NameRef)) for n in cell.ast.walk()):
            return                                     # pure constant formula -> optimizer
        structural = set()
        for n in cell.ast.walk():
            if isinstance(n, FuncCall):
                for i in STRUCTURAL_ARGS.get(n.name, ()):
                    if i < len(n.args):
                        structural.update(id(x) for x in n.args[i].walk())
            if hasattr(n, "op") and getattr(n, "op", None) == "^":
                structural.update(id(x) for x in n.right.walk())
        seen = set()
        for n in cell.ast.walk():
            if id(n) in structural or id(n) in seen:
                continue
            lit, value = None, None
            if isinstance(n, Percent) and isinstance(n.operand, Number):
                lit, value = n, n.operand.value / 100
                seen.add(id(n.operand))
            elif isinstance(n, Number):
                lit, value = n, n.value
            if lit is None or value in HARMLESS:
                continue
            match = next((x for x in self._input_values() if abs(x[0] - value) < 1e-12), None)
            distinctive = match is not None and round(value * 100, 6) % 5 != 0
            if match and not distinctive:
                self.add("SG403", "info", cell.key,
                         f"hard-coded constant {lit.unparse()} in a formula",
                         explanation=f"Magic numbers buried in formulas are hard to find and "
                                     f"update. (It has the same value as {match[2]} — if that is "
                                     f"what is meant, reference it by name.)",
                         fix="Move the value to a labelled input cell and reference it by name.")
            elif match:
                fixed = self._replace(cell, lit, NameRef(match[1]))
                self.add("SG403", "warning", cell.key,
                         f"hard-coded {lit.unparse()} duplicates {match[1]}",
                         explanation=f"The literal {lit.unparse()} has the same value as "
                                     f"{match[2]}. If that input changes, this cell will "
                                     f"silently keep using the old number.",
                         fix=f"Reference the input instead: {fixed}.", suggested=fixed)
            else:
                self.add("SG403", "info", cell.key,
                         f"hard-coded constant {lit.unparse()} in a formula",
                         explanation="Magic numbers buried in formulas are hard to find and "
                                     "update. Assumptions belong in labelled input cells.",
                         fix="Move the value to an input cell (e.g. on the Inputs sheet) and "
                             "reference it by name.")

    # ------------------------------------------------------------ SG105
    def _empty_refs(self) -> None:
        div0 = {d.cell for d in self.prior if d.code == "SG304"}
        for key, empties in self.graph.empty_refs.items():
            if key in div0:
                continue
            refs = ", ".join(sorted(set(e.split("!")[1] for e in empties)))
            self.add("SG105", "warning", key, f"reads empty cell(s) {refs}",
                     explanation=f"{refs} {'is' if ',' not in refs else 'are'} blank, so the "
                                 f"formula silently uses 0 (or empty text). This is usually a "
                                 f"reference that points one cell off.",
                     fix="Check that the reference points at the intended cell.",
                     related=sorted(set(empties)))

    # ------------------------------------------------------------ rewrite helper
    @staticmethod
    def _replace(cell: Cell, old: Node, new: Node) -> str:
        root = clone(cell.ast)
        target = (old.span, old.unparse(), type(old))

        def swap(x):
            if (x.span, x.unparse(), type(x)) == target:
                return new
            return x
        return "=" + map_tree(root, swap).unparse()


def detect_smells(wb, symbols, graph, prior=None) -> SmellDetector:
    return SmellDetector(wb, symbols, graph, prior).run()
