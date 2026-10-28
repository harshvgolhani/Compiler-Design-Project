"""Phase 8b - Vectorizing back-end: compile formula columns to NumPy.

A column of copied formulas  D2=B2*C2, D3=B3*C3, ... D10001=B10001*C10001
is, to a compiler, a *loop*:      for r in 2..10001:  D[r] = B[r] * C[r]
and a loop with no loop-carried dependence can be vectorized into one array
operation:                       D[2:10001] = B[2:10001] * C[2:10001]

Steps
  1. find vertical runs of formulas with identical R1C1 form (the loop body)
  2. dependence analysis: a run that reads its own column (running totals,
     D3 = D2 + C3) has a loop-carried dependence and stays scalar
  3. every relative reference must map to a contiguous slice of another
     vector or of an input column; absolute references become scalars
  4. schedule vector groups and remaining scalar cells in topological order
  5. emit NumPy code; aggregates like SUM(D2:D10001) become np.sum(slice)

The fast path is speculative: if any input is non-numeric, or a result is
not finite (e.g. division by zero), the engine *deoptimizes* to the scalar
compiled code, which implements full spreadsheet error semantics.
"""
from __future__ import annotations

import re
import time
from collections import defaultdict
from typing import Dict, List, Optional, Tuple

from .ast_nodes import (BinOp, CellRef, FuncCall, NameRef, Node, Number, Percent, RangeRef,
                        UnaryOp)
from .codegen import RUNTIME_NAMESPACE, CodeGenerator
from .evaluator import evaluation_order
from .graph import DependencyGraph, _sort_key
from .refs import make_key, split_key
from .symbols import SymbolTable
from .transforms import r1c1
from .workbook import Workbook

_CMP = {"=": "==", "<>": "!=", "<": "<", ">": ">", "<=": "<=", ">=": ">="}


class _Deopt(Exception):
    pass


def _is_boolean(n: Node) -> bool:
    """A column of TRUE/FALSE results must stay scalar: NumPy would sum True as 1, while
    SUM() over spreadsheet cells skips booleans."""
    if isinstance(n, BinOp) and n.op in _CMP:
        return True
    if isinstance(n, FuncCall) and n.name == "IF":
        return any(_is_boolean(a) for a in n.args[1:])
    return False


class Group:
    def __init__(self, gid: int, sheet: str, col: int, r1: int, r2: int, model_key: str):
        self.id, self.sheet, self.col, self.r1, self.r2 = gid, sheet, col, r1, r2
        self.model_key = model_key
        self.name = re.sub(r"\W", "_", f"V{gid}_{sheet}_{col}")
        self.code: Optional[str] = None

    @property
    def keys(self) -> List[str]:
        return [make_key(self.sheet, self.col, r) for r in range(self.r1, self.r2 + 1)]

    def __len__(self):
        return self.r2 - self.r1 + 1


class VectorCompiler:
    def __init__(self, wb: Workbook, symbols: SymbolTable, graph: DependencyGraph,
                 types: Optional[dict] = None, asts: Optional[Dict[str, Node]] = None,
                 min_run: int = 4):
        self.wb, self.symbols, self.graph = wb, symbols, graph
        self.types = types or {}
        self.asts = asts or {}
        self.min_run = min_run
        self.groups: List[Group] = []
        self.group_of: Dict[str, Group] = {}
        self.const_cols: Dict[Tuple[str, int, int, int], str] = {}
        self.scalar_inputs: Dict[str, str] = {}
        self.cg = CodeGenerator(wb, symbols, graph, types, asts)
        self.stats = defaultdict(int)
        self.source = ""
        self.rejected: List[Tuple[str, str]] = []

    def ast_of(self, key):
        if key in self.cg.opaque:
            return self.cg.opaque[key]
        return self.asts.get(key) or (self.wb.cell(key).ast if self.wb.cell(key) else None)

    # ------------------------------------------------------------ 1. find loops
    def find_runs(self) -> None:
        blocked = {k for c in self.graph.cycles for k in c} | set(self.graph.blocked) \
            | set(self.cg.opaque)
        for sh in self.wb.sheets.values():
            cols = defaultdict(list)
            for c in sh:
                if c.is_formula and c.ast is not None and c.key not in blocked:
                    cols[c.col].append(c)
            for col, cells in cols.items():
                cells.sort(key=lambda c: c.row)
                run = []
                for c in cells:
                    sig = r1c1(self.ast_of(c.key), c.col, c.row)
                    if run and (c.row != run[-1][0].row + 1 or sig != run[-1][1]):
                        self._maybe_group(run)
                        run = []
                    run.append((c, sig))
                self._maybe_group(run)

    def _maybe_group(self, run) -> None:
        if len(run) < self.min_run:
            return
        first = run[0][0]
        g = Group(len(self.groups), first.sheet, first.col, first.row, run[-1][0].row, first.key)
        self.groups.append(g)
        for c, _ in run:
            self.group_of[c.key] = g

    # ------------------------------------------------------------ 2-3. vectorize a body
    def _source_slice(self, sheet: str, col: int, r1: int, n: int, me: Group) -> Optional[str]:
        """Array expression for cells (sheet, col, r1 .. r1+n-1)."""
        first = make_key(sheet, col, r1)
        g = self.group_of.get(first)
        if g is not None:
            if g is me:
                return None                              # loop-carried dependence
            if g.r1 <= r1 and r1 + n - 1 <= g.r2:
                a = r1 - g.r1
                return g.name if (a == 0 and n == len(g)) else f"{g.name}[{a}:{a + n}]"
            return None
        keys = [make_key(sheet, col, r) for r in range(r1, r1 + n)]
        for k in keys:
            c = self.wb.cell(k)
            if c is None or c.is_formula or isinstance(c.raw, bool) or \
                    not isinstance(c.raw, (int, float)):
                return None
        sig = (sheet, col, r1, n)
        if sig not in self.const_cols:
            self.const_cols[sig] = f"K{len(self.const_cols)}"
        return self.const_cols[sig]

    def _scalar_input(self, key: str) -> Optional[str]:
        c = self.wb.cell(key)
        if c is None or c.is_formula or isinstance(c.raw, bool) or \
                not isinstance(c.raw, (int, float)):
            if c is not None and c.is_formula and self.cg.proven_numeric(key) and \
                    key not in self.group_of:
                return self.cg.var(key)                  # scalar formula computed earlier
            return None
        self.scalar_inputs[key] = self.cg.var(key)
        return self.cg.var(key)

    def vec(self, n: Node, g: Group, sheet: str) -> Optional[str]:
        model = self.wb.cell(g.model_key)
        if isinstance(n, Number):
            return repr(float(n.value))
        if isinstance(n, (CellRef, NameRef)):
            ref = self.symbols.resolve(n, sheet)
            if ref is None:
                return None
            if isinstance(ref, RangeRef):
                return None
            if isinstance(n, NameRef) or ref.abs_row:
                return self._scalar_input(make_key(ref.sheet, ref.col, ref.row))
            dr = ref.row - model.row
            return self._source_slice(ref.sheet, ref.col, g.r1 + dr, len(g), g)
        if isinstance(n, BinOp):
            a, b = self.vec(n.left, g, sheet), self.vec(n.right, g, sheet)
            if a is None or b is None:
                return None
            if n.op in "+-*/":
                return f"({a} {n.op} {b})"
            if n.op == "^":
                return f"np.power({a}, {b})"
            if n.op in _CMP:
                return f"({a} {_CMP[n.op]} {b})"
            return None
        if isinstance(n, UnaryOp):
            a = self.vec(n.operand, g, sheet)
            return None if a is None else (f"(-{a})" if n.op == "-" else a)
        if isinstance(n, Percent):
            a = self.vec(n.operand, g, sheet)
            return None if a is None else f"({a} / 100.0)"
        if isinstance(n, FuncCall):
            args = [self.vec(a, g, sheet) for a in n.args]
            if any(a is None for a in args):
                return None
            if n.name == "ABS" and len(args) == 1:
                return f"np.abs({args[0]})"
            if n.name == "SQRT" and len(args) == 1:
                return f"np.sqrt({args[0]})"
            if n.name == "ROUND" and len(args) == 2 and isinstance(n.args[1], Number):
                k = 10.0 ** int(n.args[1].value)
                return (f"(np.sign({args[0]}) * np.floor(np.abs({args[0]}) * {k!r} + 0.5 + 1e-9) "
                        f"/ {k!r})")
            if n.name in ("MIN", "MAX") and len(args) == 2:
                return f"np.{'minimum' if n.name == 'MIN' else 'maximum'}({args[0]}, {args[1]})"
            if n.name == "IF" and len(args) == 3:
                return f"np.where({args[0]}, {args[1]}, {args[2]})"
            return None
        return None

    # ------------------------------------------------------------ scalar aggregates
    def scalar_fast(self, n: Node, sheet: str) -> Optional[str]:
        """SUM/AVERAGE/MIN/MAX over a slice of a vector -> one NumPy reduction."""
        if isinstance(n, FuncCall) and n.name in ("SUM", "AVERAGE", "MIN", "MAX") and \
                len(n.args) == 1 and isinstance(n.args[0], RangeRef):
            r = self.symbols.resolve(n.args[0], sheet)
            if r is None or r.col1 is None or r.row1 is None or r.col1 != r.col2:
                return None
            sl = self._source_slice(r.sheet, r.col1, r.row1, r.row2 - r.row1 + 1, None)
            if sl is None:
                return None
            fn = {"SUM": "sum", "AVERAGE": "mean", "MIN": "min", "MAX": "max"}[n.name]
            return f"float(np.{fn}({sl}))"
        if isinstance(n, BinOp) and n.op in "+-*/":
            a, b = self.scalar_fast(n.left, sheet), self.scalar_fast(n.right, sheet)
            if a is None or b is None:
                return None
            return f"({a} {n.op} {b})"
        if isinstance(n, Number):
            return repr(float(n.value))
        if isinstance(n, (CellRef, NameRef)):
            ref = self.symbols.resolve(n, sheet)
            if isinstance(ref, CellRef):
                k = make_key(ref.sheet, ref.col, ref.row)
                c = self.wb.cell(k)
                if c is not None and (self.cg.proven_numeric(k) or k in self.group_of):
                    if not c.is_formula:
                        self.scalar_inputs[k] = self.cg.var(k)
                    return self.cg.var(k)
        return None

    # ------------------------------------------------------------ 4-5. schedule + emit
    def generate(self) -> str:
        self.find_runs()
        for g in list(self.groups):
            model = self.wb.cell(g.model_key)
            root = self.ast_of(g.model_key)
            g.code = None if _is_boolean(root) else self.vec(root, g, model.sheet)
            if g.code is None:
                self.rejected.append((f"{g.sheet}!{g.keys[0].split('!')[1]}:"
                                      f"{g.keys[-1].split('!')[1]}", "not vectorizable"))
        # drop groups that could not be vectorized, then re-check (dependencies may change)
        changed = True
        while changed:
            changed = False
            keep = [g for g in self.groups if g.code is not None]
            if len(keep) != len(self.groups):
                self.groups = keep
                self.group_of = {k: g for g in keep for k in g.keys}
                self.const_cols.clear()
                for g in self.groups:
                    m = self.wb.cell(g.model_key)
                    g.code = self.vec(self.ast_of(g.model_key), g, m.sheet)
                    if g.code is None:
                        changed = True

        order = evaluation_order(self.graph)
        cycle = {k for c in self.graph.cycles for k in c}
        # topological schedule over units (a group = one unit)
        unit_of = {}
        for k in order:
            unit_of[k] = self.group_of[k] if k in self.group_of else k
        units, seen = [], set()
        deps = defaultdict(set)
        for k in order:
            u = unit_of[k]
            for p in self.graph.preds.get(k, ()):
                pu = unit_of.get(p, p)
                if pu is not u:
                    deps[id(u) if isinstance(u, Group) else u].add(id(pu) if isinstance(pu, Group)
                                                                   else pu)
            uid = id(u) if isinstance(u, Group) else u
            if uid not in seen:
                seen.add(uid)
                units.append(u)
        uid = {(id(u) if isinstance(u, Group) else u): u for u in units}
        indeg = {i: len([d for d in deps[i] if d in uid]) for i in uid}
        succ = defaultdict(list)
        for i in uid:
            for d in deps[i]:
                if d in uid:
                    succ[d].append(i)
        pos = {(id(u) if isinstance(u, Group) else u): n for n, u in enumerate(units)}
        import heapq
        ready = [(pos[i], i) for i, d in indeg.items() if d == 0]
        heapq.heapify(ready)
        sched = []
        while ready:
            _, i = heapq.heappop(ready)
            sched.append(uid[i])
            for s in succ[i]:
                indeg[s] -= 1
                if indeg[s] == 0:
                    heapq.heappush(ready, (pos[s], s))
        if len(sched) != len(units):                       # unit-level cycle: give up
            self.groups, self.group_of = [], {}
            sched = order

        chunks = []                    # (group or None, [lines])
        needs_spill = set()
        scalar_reads = set()
        for u in sched:
            if isinstance(u, Group):
                self.stats["vector_groups"] += 1
                self.stats["vector_cells"] += len(u)
                chunks.append((u, [
                    f"    # {u.sheet}!{u.keys[0].split('!')[1]}:{u.keys[-1].split('!')[1]}  "
                    f"({len(u)} cells)  {self.wb.cell(u.model_key).raw} ...",
                    f"    {u.name} = {u.code}",
                    f"    if not np.isfinite({u.name}).all(): raise _Deopt"]))
                continue
            k = u
            c = self.wb.cell(k)
            if c is None or not c.is_formula:
                continue
            v = self.cg.var(k)
            self.stats["scalar_cells"] += 1
            if k in cycle:
                chunks.append((None, [f"    {v} = CIRC"]))
                continue
            ast = self.ast_of(k)
            fast = self.scalar_fast(ast, c.sheet) if ast is not None else None
            if fast is not None:
                self.stats["numpy_reductions"] += fast.count("np.")
                chunks.append((None, [f"    {v} = _ok({fast})"]))   # inf/NaN -> deoptimize
                for p in self.graph.preds.get(k, ()):
                    if p in self.group_of and self.cg.var(p) in fast:
                        needs_spill.add(id(self.group_of[p]))
                continue
            for p in self.graph.preds.get(k, ()):
                if p in self.group_of:
                    needs_spill.add(id(self.group_of[p]))
                elif self.wb.cell(p) is not None and not self.wb.cell(p).is_formula:
                    scalar_reads.add(p)
            safe = self.cg.safe(ast, c.sheet) if ast is not None else "XLError('#ERROR!')"
            chunks.append((None, [f"    try:\n        {v} = _fin({safe})\n"
                                  f"    except XLError as _e:\n        {v} = _e"]))
        body = []
        for g, lines in chunks:
            body.extend(lines)
            if g is not None and id(g) in needs_spill:
                names = ", ".join(self.cg.var(k) for k in g.keys)
                body.append(f"    ({names},) = {g.name}.tolist()")
                self.stats["spilled_groups"] += 1
        for p in scalar_reads:
            self.scalar_inputs[p] = self.cg.var(p)

        head = ["def recalc(I):",
                f"    # NumPy vector program for '{self.wb.name}'"]
        self._key_lists = []
        for (sheet, col, r1, n), name in sorted(self.const_cols.items(), key=lambda x: x[1]):
            keys = [make_key(sheet, col, r) for r in range(r1, r1 + n)]
            self._key_lists.append(keys)
            head.append(f"    {name} = np.fromiter(map(I.__getitem__, _KEYS[{len(self._key_lists) - 1}]),"
                        f" float, {n})   # {keys[0]}:{keys[-1].split('!')[1]}")
            self.stats["input_vectors"] += 1
        for k, name in sorted(self.scalar_inputs.items(), key=lambda x: _sort_key(x[0])):
            head.append(f"    {name} = I.get({k!r})")
        vec_names = [g.name for g in self.groups]
        scal = [k for k in order if k not in self.group_of and self.wb.cell(k) is not None
                and self.wb.cell(k).is_formula]
        tail = ["    return {'vectors': {" + ", ".join(f"{g.name!r}: {g.name}" for g in self.groups)
                + "}, 'scalars': {" + ", ".join(f"{k!r}: {self.cg.var(k)}" for k in scal) + "}}"]
        self.source = "\n".join(head + [""] + body + [""] + tail) + "\n"
        self._vec_names = vec_names
        return self.source

    def compile(self, fallback=None):
        import numpy as np
        if not self.source:
            self.generate()
        ns = dict(RUNTIME_NAMESPACE)
        ns.update({"np": np, "_Deopt": _Deopt, "_KEYS": self._key_lists})
        t = time.perf_counter()
        exec(compile(self.source, f"<sheetguard-numpy:{self.wb.name}>", "exec"), ns)
        self.stats["compile_ms"] = round((time.perf_counter() - t) * 1000, 2)
        fast = ns["recalc"]
        groups = list(self.groups)

        def recalc(I):
            with np.errstate(all="ignore"):
                try:
                    return fast(I)
                except Exception:                     # speculation failed -> deoptimize
                    self.stats["deopts"] += 1
                    if fallback is None:
                        raise
                    out = fallback(I)
                    return {"vectors": {}, "scalars": out, "deoptimized": True}

        def materialize(result) -> Dict[str, object]:
            if result.get("deoptimized"):
                return dict(result["scalars"])
            out = dict(result["scalars"])
            for g in groups:
                arr = result["vectors"][g.name]
                for k, x in zip(g.keys, arr.tolist()):
                    out[k] = x
            return out

        recalc.materialize = materialize
        return recalc


def pretty_source(src: str, width: int = 140) -> str:
    """Shorten very long generated lines (huge key lists / unpackings) for display."""
    out = []
    for line in src.splitlines():
        out.append(line if len(line) <= width else line[:width - 12] + " … " + line[-8:])
    return "\n".join(out)


def vector_compile(wb, symbols, graph, types=None, asts=None, fallback=None, min_run=4):
    vc = VectorCompiler(wb, symbols, graph, types, asts, min_run)
    vc.generate()
    return vc, vc.compile(fallback)
