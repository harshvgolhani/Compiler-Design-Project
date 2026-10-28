"""Intermediate representation #2: the whole-workbook Dependency DAG.

Nodes are cells; an edge  P -> D  means "D's formula reads P" (P is a
*precedent* of D, D is a *dependent* of P). On this graph we run three
classical algorithms:

  * Tarjan's strongly-connected-components  -> circular references
  * Kahn's topological sort (by levels)     -> the minimal, correct
                                               recalculation order; every
                                               level can be computed in parallel
  * forward / backward reachability (BFS)   -> impact analysis: "if I change B2,
                                               which cells change?"
"""
from __future__ import annotations

from functools import lru_cache
from collections import deque
from typing import Dict, Iterable, List, Optional, Set

from .ast_nodes import CellRef, NameRef, RangeRef
from .diagnostics import Diagnostic
from .refs import make_key, split_key
from .symbols import SymbolTable
from .workbook import Cell, Workbook

# Ranges larger than this are summarised instead of drawn edge-by-edge in the UI.
_MAX_EXPAND = 200_000


class DependencyGraph:
    def __init__(self, wb: Workbook, symbols: SymbolTable):
        self.wb = wb
        self.symbols = symbols
        self.nodes: Set[str] = set()
        self.preds: Dict[str, Set[str]] = {}     # cell -> cells it reads
        self.succs: Dict[str, Set[str]] = {}     # cell -> cells that read it
        self.range_refs: Dict[str, List[str]] = {}  # cell -> ranges as written (for the UI)
        self.empty_refs: Dict[str, List[str]] = {}  # cell -> referenced *empty* cells
        self.cycles: List[List[str]] = []
        self.order: List[str] = []
        self.levels: List[List[str]] = []
        self.diagnostics: List[Diagnostic] = []

    # ------------------------------------------------------------------ build
    def add_node(self, key: str) -> None:
        if key not in self.nodes:
            self.nodes.add(key)
            self.preds.setdefault(key, set())
            self.succs.setdefault(key, set())

    def add_edge(self, src: str, dst: str) -> None:
        self.add_node(src)
        self.add_node(dst)
        self.preds[dst].add(src)
        self.succs[src].add(dst)

    def expand(self, ref) -> Iterable[str]:
        """Keys of the *non-empty* cells covered by a resolved Cell/Range reference."""
        sheet = self.wb.sheets[ref.sheet]
        if isinstance(ref, CellRef):
            yield make_key(ref.sheet, ref.col, ref.row)
            return
        c1, c2, r1, r2 = sheet.bounds(ref)
        area = (c2 - c1 + 1) * (r2 - r1 + 1)
        if area <= len(sheet.cells) and area <= _MAX_EXPAND:
            for r in range(r1, r2 + 1):
                for c in range(c1, c2 + 1):
                    if (c, r) in sheet.cells:
                        yield make_key(ref.sheet, c, r)
        else:  # huge range (e.g. A:A): scan the sparse cell map instead
            for (c, r) in sorted(sheet.cells, key=lambda x: (x[1], x[0])):
                if c1 <= c <= c2 and r1 <= r <= r2:
                    yield make_key(ref.sheet, c, r)

    def build(self) -> "DependencyGraph":
        for cell in self.wb.cells():
            self.add_node(cell.key)
        for cell in self.wb.formula_cells():
            if cell.ast is None:
                continue
            for node in cell.ast.walk():
                if not isinstance(node, (CellRef, RangeRef, NameRef)):
                    continue
                ref = self.symbols.resolve(node, cell.sheet)
                if ref is None:
                    continue                    # already reported by the symbol table
                if isinstance(ref, RangeRef):
                    self.range_refs.setdefault(cell.key, []).append(ref.unparse())
                for src in self.expand(ref):
                    if isinstance(ref, CellRef) and self.wb.cell(src) is None:
                        self.empty_refs.setdefault(cell.key, []).append(src)
                    self.add_edge(src, cell.key)
        self.find_cycles()
        self.topological_levels()
        return self

    # ---------------------------------------------------- Tarjan's SCC (iterative)
    def find_cycles(self) -> List[List[str]]:
        index: Dict[str, int] = {}
        low: Dict[str, int] = {}
        on_stack: Set[str] = set()
        stack: List[str] = []
        sccs: List[List[str]] = []
        counter = 0

        for root in sorted(self.nodes):
            if root in index:
                continue
            work = [(root, iter(sorted(self.succs[root])))]
            index[root] = low[root] = counter
            counter += 1
            stack.append(root)
            on_stack.add(root)
            while work:
                v, it = work[-1]
                advanced = False
                for w in it:
                    if w not in index:
                        index[w] = low[w] = counter
                        counter += 1
                        stack.append(w)
                        on_stack.add(w)
                        work.append((w, iter(sorted(self.succs[w]))))
                        advanced = True
                        break
                    if w in on_stack:
                        low[v] = min(low[v], index[w])
                if advanced:
                    continue
                work.pop()
                if work:
                    parent = work[-1][0]
                    low[parent] = min(low[parent], low[v])
                if low[v] == index[v]:
                    comp = []
                    while True:
                        w = stack.pop()
                        on_stack.discard(w)
                        comp.append(w)
                        if w == v:
                            break
                    if len(comp) > 1 or v in self.succs[v]:
                        sccs.append(self._cycle_path(comp))

        self.cycles = sccs
        self.diagnostics = [d for d in self.diagnostics if d.code != "SG201"]
        for comp in sccs:
            path = " → ".join(comp + [comp[0]])
            for key in comp:
                self.diagnostics.append(Diagnostic(
                    "SG201", "error", key,
                    "circular reference: " + ("cell refers to itself" if len(comp) == 1
                                              else f"part of a {len(comp)}-cell cycle"),
                    explanation=f"These cells depend on each other in a loop ({path}). There is "
                                f"no valid order to compute them, so the sheet shows an error or "
                                f"a stale value.",
                    fix="Break the loop: one of these cells must use an input value instead "
                        "of a formula that leads back to itself.",
                    related=[k for k in comp if k != key], phase="graph"))
        return sccs

    def _cycle_path(self, comp: List[str]) -> List[str]:
        """Order an SCC's members along an actual cycle (nicer to read)."""
        members = set(comp)
        start = min(comp)
        path, seen, cur = [start], {start}, start
        while True:
            nxt = sorted(w for w in self.succs[cur] if w in members)
            nxt_unseen = [w for w in nxt if w not in seen]
            if not nxt_unseen:
                break
            cur = nxt_unseen[0]
            path.append(cur)
            seen.add(cur)
        rest = [k for k in sorted(comp) if k not in seen]
        return path + rest

    # ---------------------------------------------------- Kahn's algorithm (levels)
    def topological_levels(self) -> List[List[str]]:
        in_cycle = {k for comp in self.cycles for k in comp}
        indeg = {k: len(self.preds[k]) for k in self.nodes if k not in in_cycle}
        frontier = sorted((k for k, d in indeg.items() if d == 0), key=_sort_key)
        levels: List[List[str]] = []
        order: List[str] = []
        while frontier:
            levels.append(frontier)
            order.extend(frontier)
            nxt = []
            for k in frontier:
                for d in self.succs[k]:
                    if d in indeg:
                        indeg[d] -= 1
                        if indeg[d] == 0:
                            nxt.append(d)
            frontier = sorted(nxt, key=_sort_key)
        # anything downstream of a cycle can't be ordered either
        self.blocked = sorted((k for k, d in indeg.items() if d > 0), key=_sort_key)
        self.levels, self.order = levels, order
        return levels

    def recalc_order(self) -> List[str]:
        """Formula cells only, in a valid evaluation order."""
        return [k for k in self.order if self._is_formula(k)]

    def formula_levels(self) -> List[List[str]]:
        out = []
        for lvl in self.levels:
            f = [k for k in lvl if self._is_formula(k)]
            if f:
                out.append(f)
        return out

    def _is_formula(self, key: str) -> bool:
        c = self.wb.cell(key)
        return bool(c and c.is_formula)

    # ---------------------------------------------------- reachability
    def _bfs(self, start: str, edges: Dict[str, Set[str]]) -> List[str]:
        seen, q = {start}, deque([start])
        while q:
            for nxt in edges.get(q.popleft(), ()):
                if nxt not in seen:
                    seen.add(nxt)
                    q.append(nxt)
        seen.discard(start)
        return sorted(seen, key=_sort_key)

    def dependents_limited(self, key: str, limit: int = 200):
        """Impact analysis with a budget: (first `limit` dependents, exact count or None if
        there are more). Keeps per-cell reports O(limit) on very long dependency chains."""
        seen, q = {key}, deque([key])
        while q and len(seen) <= limit:
            for nxt in self.succs.get(q.popleft(), ()):
                if nxt not in seen:
                    seen.add(nxt)
                    q.append(nxt)
        seen.discard(key)
        exact = not q and len(seen) <= limit
        return sorted(seen, key=_sort_key)[:limit], (len(seen) if exact else None)

    def dependents(self, key: str, transitive: bool = True) -> List[str]:
        """Impact analysis: every cell whose value can change if `key` changes."""
        return self._bfs(key, self.succs) if transitive else sorted(self.succs.get(key, ()),
                                                                    key=_sort_key)

    def precedents(self, key: str, transitive: bool = True) -> List[str]:
        """Every cell `key` (directly or indirectly) reads."""
        return self._bfs(key, self.preds) if transitive else sorted(self.preds.get(key, ()),
                                                                    key=_sort_key)

    # ---------------------------------------------------- reporting
    def edges(self):
        for src in sorted(self.succs, key=_sort_key):
            for dst in sorted(self.succs[src], key=_sort_key):
                yield src, dst

    def stats(self) -> dict:
        n_edges = sum(len(v) for v in self.succs.values())
        longest = len(self.levels)
        fan_in = max(((k, len(v)) for k, v in self.preds.items()), key=lambda x: x[1],
                     default=(None, 0))
        fan_out = max(((k, len(v)) for k, v in self.succs.items()), key=lambda x: x[1],
                      default=(None, 0))
        return {"nodes": len(self.nodes), "edges": n_edges, "levels": longest,
                "cycles": len(self.cycles), "max_fan_in": {"cell": fan_in[0], "n": fan_in[1]},
                "max_fan_out": {"cell": fan_out[0], "n": fan_out[1]}}

    def to_dict(self) -> dict:
        in_cycle = {k for comp in self.cycles for k in comp}
        level_of = {k: i for i, lvl in enumerate(self.levels) for k in lvl}
        nodes = []
        for k in sorted(self.nodes, key=_sort_key):
            c = self.wb.cell(k)
            nodes.append({
                "id": k, "formula": c.raw if c and c.is_formula else None,
                "value": c.value if c else None, "is_formula": bool(c and c.is_formula),
                "empty": c is None, "level": level_of.get(k), "in_cycle": k in in_cycle,
                "in_degree": len(self.preds[k]), "out_degree": len(self.succs[k]),
            })
        return {"nodes": nodes, "edges": [{"from": a, "to": b} for a, b in self.edges()],
                "cycles": self.cycles, "levels": self.formula_levels(),
                "order": self.recalc_order(), "blocked": getattr(self, "blocked", []),
                "stats": self.stats()}

    def to_dot(self) -> str:
        in_cycle = {k for comp in self.cycles for k in comp}
        out = ["digraph DAG {", "  rankdir=LR;",
               '  node [shape=box, style="rounded,filled", fontname="Helvetica"];']
        for k in sorted(self.nodes, key=_sort_key):
            c = self.wb.cell(k)
            color = "#fecaca" if k in in_cycle else ("#dbeafe" if c and c.is_formula
                                                     else "#f1f5f9")
            label = k + (f"\\n{c.raw}" if c and c.is_formula else "")
            out.append(f'  "{k}" [label="{label.replace(chr(34), chr(39))}", '
                       f'fillcolor="{color}"];')
        for a, b in self.edges():
            red = ' [color="#dc2626", penwidth=2]' if a in in_cycle and b in in_cycle else ""
            out.append(f'  "{a}" -> "{b}"{red};')
        out.append("}")
        return "\n".join(out)


@lru_cache(maxsize=1 << 18)
def _sort_key(key: str):
    s, c, r = split_key(key)
    return (s, r, c)


def build_graph(wb: Workbook, symbols: SymbolTable) -> DependencyGraph:
    return DependencyGraph(wb, symbols).build()
