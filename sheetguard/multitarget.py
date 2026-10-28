"""Multi-target code generation: the same IR (ASTs + topological levels of the
dependency DAG) compiled to two more target languages.

  * JavaScript: a self-contained function `recalc(I)` with a small runtime
    prelude implementing spreadsheet semantics (errors are thrown objects,
    so #DIV/0! propagates exactly like in the interpreter).
  * SQL (SQLite dialect): the whole workbook becomes ONE query. Every level of
    the topological sort becomes a common table expression (CTE) that adds
    one column per formula cell; a cell can only read columns from earlier
    CTEs, which is exactly the guarantee topological order gives us.

        WITH inputs AS (SELECT 1200 AS "Sales!B2", 45 AS "Sales!C2", ...),
             l1 AS (SELECT *, ("Sales!B2" * "Sales!C2") AS "Sales!D2", ... FROM inputs),
             l2 AS (SELECT *, ("Sales!D2" * (1 - "Inputs!B3")) AS "Sales!E2", ... FROM l1)
        SELECT * FROM l8;

Both targets are verified by *executing* the generated code (Node.js for JS,
Python's built-in sqlite3 for SQL) and comparing every cell with the interpreter.
"""
from __future__ import annotations

import json
import math
import os
import shutil
import sqlite3
import subprocess
import tempfile
import time
from typing import Dict, List, Optional, Tuple

from . import runtime as rt
from .functions import arity_ok
from .ast_nodes import (ArrayLit, BinOp, Bool, CellRef, ErrorLit, FuncCall, Missing, NameRef,
                        Node, Number, Percent, RangeRef, String, UnaryOp)
from .evaluator import Interpreter, evaluation_order, input_value, opaque_cells
from .graph import _sort_key
from .refs import make_key


def _range_keys(wb, r: RangeRef) -> List[str]:
    c1, c2, r1, r2 = wb.sheets[r.sheet].bounds(r)
    return [make_key(r.sheet, c, row) for row in range(r1, r2 + 1) for c in range(c1, c2 + 1)]


# =========================================================================== JavaScript
JS_PRELUDE = r"""
// ---- SheetGuard JS runtime: spreadsheet value semantics -------------------
const E = (code) => ({ err: code });
const isErr = (v) => v !== null && typeof v === "object" && !Array.isArray(v) && "err" in v;
function sc(v) { if (Array.isArray(v)) { if (v.length === 1) return v[0]; throw E("#VALUE!"); } return v; }
const NUMTEXT = /^\s*([-+]?)\s*[$€£₹¥]?\s*([-+]?\d[\d,]*(\.\d*)?|\.\d+)([eE][-+]?\d+)?\s*(%?)\s*$/;
function num(v) {
  if (isErr(v)) throw v;
  if (v === null || v === undefined) return 0;
  if (typeof v === "boolean") return v ? 1 : 0;
  if (typeof v === "number") return v;
  if (Array.isArray(v)) { if (v.length === 1) return num(v[0]); throw E("#VALUE!"); }
  const m = NUMTEXT.exec(String(v));
  if (m) { let x = parseFloat((m[2] + (m[4] || "")).replace(/,/g, "")); if (m[1] === "-") x = -x; return m[5] ? x / 100 : x; }
  throw E("#VALUE!");
}
function txt(v) {
  v = sc(v);
  if (isErr(v)) throw v;
  if (v === null || v === undefined) return "";
  if (typeof v === "boolean") return v ? "TRUE" : "FALSE";
  if (typeof v === "number") return g15(v);
  return String(v);
}
function g15(v) {
  if (!isFinite(v)) throw E("#NUM!");
  if (Number.isInteger(v) && Math.abs(v) < 1e15) return String(v === 0 ? 0 : v);
  let [m, ex] = v.toExponential(14).split("e"); const X = parseInt(ex, 10);
  if (X < -4 || X >= 15) { if (m.includes(".")) m = m.replace(/\.?0+$/, ""); return m + "E" + (X < 0 ? "-" : "+") + String(Math.abs(X)).padStart(2, "0"); }
  let f = v.toFixed(14 - X); if (f.includes(".")) f = f.replace(/\.?0+$/, ""); return f;
}
function ref(v) { if (isErr(v)) throw v; return v === undefined ? null : v; }
function fin(v) { v = sc(v); if (isErr(v)) throw v; if (typeof v === "number" && !isFinite(v)) throw E("#NUM!"); return v; }
const add = (a, b) => num(a) + num(b), sub = (a, b) => num(a) - num(b), mul = (a, b) => num(a) * num(b);
function div(a, b) { const n = num(a), d = num(b); if (d === 0) throw E("#DIV/0!"); return n / d; }
function pow(a, b) { const x = num(a), y = num(b); if (x === 0 && y < 0) throw E("#DIV/0!"); const r = Math.pow(x, y); if (!isFinite(r)) throw E("#NUM!"); return r; }
const neg = (a) => -num(a), pct = (a) => num(a) / 100, cat = (a, b) => txt(a) + txt(b);
function rank(v) { return v === null ? 0 : typeof v === "boolean" ? 3 : typeof v === "number" ? 1 : 2; }
function cmp(op, a, b) {
  a = sc(a); b = sc(b);
  if (isErr(a)) throw a; if (isErr(b)) throw b;
  if (a === null) a = typeof b === "string" ? "" : typeof b === "boolean" ? false : 0;
  if (b === null) b = typeof a === "string" ? "" : typeof a === "boolean" ? false : 0;
  let x = a, y = b;
  if (rank(a) !== rank(b)) { x = rank(a); y = rank(b); } else if (typeof a === "string") { x = a.toLowerCase(); y = b.toLowerCase(); }
  return { "=": x === y, "<>": x !== y, "<": x < y, ">": x > y, "<=": x <= y, ">=": x >= y }[op];
}
function truthy(v) {
  v = sc(v);
  if (isErr(v)) throw v;
  if (typeof v === "boolean") return v; if (v === null) return false; if (typeof v === "number") return v !== 0;
  if (typeof v === "string") { const u = v.toUpperCase(); if (u === "TRUE" || u === "FALSE") return u === "TRUE"; }
  throw E("#VALUE!");
}
function nums(args) {
  const out = [];
  for (const a of args) {
    if (Array.isArray(a)) { for (const v of a) { if (isErr(v)) throw v; if (typeof v === "number") out.push(v); } }
    else if (a !== null) out.push(num(a));
  }
  return out;
}
function logicals(a) { const v = a.flat().filter((x) => x !== null).map(truthy); if (!v.length) throw E("#VALUE!"); return v; }
function tryv(f) { try { return f(); } catch (e) { if (isErr(e)) return e; throw e; } }
function round(x, n) { x = num(x); n = Math.trunc(num(n || 0)); if (n > 15) return x; if (n < -308) return 0; const f = Math.pow(10, n); return Math.sign(x) * Math.floor(Math.abs(x) * f + 0.5 + 1e-9) / f; }
const F = {
  SUM: (...a) => nums(a).reduce((s, x) => s + x, 0),
  PRODUCT: (...a) => nums(a).reduce((s, x) => s * x, 1),
  AVERAGE: (...a) => { const x = nums(a); if (!x.length) throw E("#DIV/0!"); return x.reduce((s, y) => s + y, 0) / x.length; },
  MIN: (...a) => { const x = nums(a); return x.length ? Math.min(...x) : 0; },
  MAX: (...a) => { const x = nums(a); return x.length ? Math.max(...x) : 0; },
  COUNT: (...a) => a.flat().filter((v) => typeof v === "number").length,
  COUNTA: (...a) => a.flat().filter((v) => v !== null && v !== "").length,
  ROUND: (x, n) => round(x, n), ABS: (x) => Math.abs(num(x)), INT: (x) => Math.floor(num(x)),
  SQRT: (x) => { x = num(x); if (x < 0) throw E("#NUM!"); return Math.sqrt(x); },
  MOD: (a, b) => { a = num(a); b = num(b); if (b === 0) throw E("#DIV/0!"); return a - b * Math.floor(a / b); },
  POWER: (a, b) => pow(a, b), PI: () => Math.PI,
  AND: (...a) => logicals(a).every((x) => x),
  OR: (...a) => logicals(a).some((x) => x),
  NOT: (x) => !truthy(x),
  CONCATENATE: (...a) => a.flat().map(txt).join(""), CONCAT: (...a) => a.flat().map(txt).join(""),
  LEN: (s) => txt(s).length, UPPER: (s) => txt(s).toUpperCase(), LOWER: (s) => txt(s).toLowerCase(),
  ISBLANK: (v) => v === null, ISNUMBER: (v) => typeof v === "number",
};
function call(name, args) { const f = F[name]; if (!f) throw E("#NAME?"); return f(...args); }
function iferror(a, b) { try { const v = a(); if (isErr(v)) throw v; return v; } catch (e) { if (isErr(e)) return b(); throw e; } }
""".strip()

JS_SUPPORTED = {"SUM", "PRODUCT", "AVERAGE", "MIN", "MAX", "COUNT", "COUNTA", "ROUND", "ABS", "INT",
                "SQRT", "MOD", "POWER", "PI", "AND", "OR", "NOT", "CONCATENATE", "CONCAT", "LEN",
                "UPPER", "LOWER", "ISBLANK", "ISNUMBER", "IF", "IFERROR"}
_JS_BIN = {"+": "add", "-": "sub", "*": "mul", "/": "div", "^": "pow", "&": "cat"}


class JSGenerator:
    def __init__(self, wb, symbols, graph, asts=None):
        self.wb, self.symbols, self.graph = wb, symbols, graph
        self.asts = asts or {}
        self.opaque = opaque_cells(wb)
        self.unsupported: Dict[str, str] = {}

    def v(self, key: str) -> str:
        return f"V[{json.dumps(key)}]"

    def expr(self, n: Node, sheet: str, cell: str) -> str:
        t = type(n)
        if t is Number:
            return repr(float(n.value))
        if t is String:
            return json.dumps(n.value)
        if t is Bool:
            return "true" if n.value else "false"
        if t is ErrorLit:
            return f"(() => {{ throw E({json.dumps(n.value)}); }})()"
        if t is Missing:
            return "null"
        if t is ArrayLit:
            return "[" + ", ".join(self.expr(x, sheet, cell) for row in n.rows for x in row) + "]"
        if t in (CellRef, RangeRef, NameRef):
            if t is NameRef:
                sym = self.symbols.lookup(n.name, sheet)
                if sym is not None and sym.kind == "constant":
                    return self.expr(sym.target, sheet, cell)
            r = self.symbols.resolve(n, sheet)
            if r is None:
                code = "#NAME?" if t is NameRef else "#REF!"
                return f"(() => {{ throw E({json.dumps(code)}); }})()"
            if isinstance(r, CellRef):
                k = make_key(r.sheet, r.col, r.row)
                return f"ref({self.v(k)})" if self.wb.cell(k) else "null"
            keys = _range_keys(self.wb, r)
            return "[" + ", ".join(self.v(k) if self.wb.cell(k) else "null" for k in keys) + "]"
        if t is BinOp:
            a, b = self.expr(n.left, sheet, cell), self.expr(n.right, sheet, cell)
            if n.op in _JS_BIN:
                return f"{_JS_BIN[n.op]}({a}, {b})"
            return f"cmp({json.dumps(n.op)}, {a}, {b})"
        if t is UnaryOp:
            a = self.expr(n.operand, sheet, cell)
            return f"neg({a})" if n.op == "-" else f"num({a})"
        if t is Percent:
            return f"pct({self.expr(n.operand, sheet, cell)})"
        if t is FuncCall:
            if not arity_ok(n.name, len(n.args)):
                return '(() => { throw E("#N/A"); })()'
            args = [self.expr(a, sheet, cell) for a in n.args]
            if n.name == "IF":
                return (f"(truthy({args[0]}) ? {args[1] if len(args) > 1 else 'false'} : "
                        f"{args[2] if len(args) > 2 else 'false'})")
            if n.name == "IFERROR":
                return f"iferror(() => {args[0]}, () => {args[1]})"
            if n.name not in JS_SUPPORTED:
                self.unsupported[cell] = n.name
            if n.name in rt.ERROR_TOLERANT:
                args = [f"tryv(() => {s})" for s in args]
            if n.name in rt.REF_AGG:
                args = [f"[{s}]" if self._is_cell(a, sheet) else s for a, s in zip(n.args, args)]
            return f"call({json.dumps(n.name)}, [{', '.join(args)}])"
        return "null"

    def _is_cell(self, a: Node, sheet: str) -> bool:
        if type(a) not in (CellRef, NameRef):
            return False
        if type(a) is NameRef:
            sym = self.symbols.lookup(a.name, sheet)
            if sym is None or sym.kind == "constant":
                return False
        return isinstance(self.symbols.resolve(a, sheet), CellRef)

    def generate(self) -> str:
        cyc = {k for c in self.graph.cycles for k in c}
        lines = [JS_PRELUDE, "",
                 "// ---- generated from the workbook: one statement per formula cell, "
                 "in topological order",
                 "function recalc(I) {", "  const V = Object.assign({}, I);"]
        n = 0
        for key in evaluation_order(self.graph):
            c = self.wb.cell(key)
            if c is None or not c.is_formula:
                continue
            n += 1
            lines.append(f"  // {key}  {c.raw}")
            if key in cyc:
                lines.append(f"  {self.v(key)} = E(\"#CIRC!\");")
                continue
            ast = self.opaque.get(key) or self.asts.get(key) or c.ast
            if ast is None:
                lines.append(f"  {self.v(key)} = E(\"#ERROR!\");")
                continue
            e = self.expr(ast, c.sheet, key)
            lines.append(f"  try {{ {self.v(key)} = fin({e}); }} catch (e) {{ {self.v(key)} = "
                         f"isErr(e) ? e : E(\"#VALUE!\"); }}")
        lines += ["  return V;", "}", "",
                  "if (typeof module !== 'undefined' && require.main === module) {",
                  "  const I = JSON.parse(require('fs').readFileSync(0, 'utf8'));",
                  "  process.stdout.write(JSON.stringify(recalc(I)));", "}"]
        self.formula_cells = n
        return "\n".join(lines) + "\n"


# =========================================================================== SQL
def _q(key: str) -> str:
    return '"' + key.replace('"', '""') + '"'


def _sql_lit(v) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, (int, float)):
        return repr(float(v)) if not float(v).is_integer() else str(int(v))
    if isinstance(v, rt.XLError):
        return "NULL"
    return "'" + str(v).replace("'", "''") + "'"


SQL_SUPPORTED = {"SUM", "AVERAGE", "MIN", "MAX", "ROUND", "ABS", "INT", "SQRT", "POWER", "IF",
                 "IFERROR", "AND", "OR", "NOT", "COUNT", "PI"}


class SQLGenerator:
    """Errors become NULL (SQL has no error values); NULL propagates through
    arithmetic just like an error does, and IFERROR becomes COALESCE."""

    def __init__(self, wb, symbols, graph, asts=None):
        self.wb, self.symbols, self.graph = wb, symbols, graph
        self.asts = asts or {}
        self.opaque = opaque_cells(wb)
        self.unsupported: Dict[str, str] = {}

    def col(self, key: str) -> str:
        c = self.wb.cell(key)
        if c is None:
            return "0"                       # a blank cell is 0 in arithmetic
        return _q(key)

    def _numeric_item(self, key: str) -> Optional[str]:
        """How a cell contributes to SUM/AVERAGE over a range (text and blanks skipped)."""
        c = self.wb.cell(key)
        if c is None:
            return None
        if not c.is_formula and not isinstance(c.raw, (int, float)) or isinstance(c.raw, bool):
            return None
        return _q(key)

    def _items(self, n: Node, sheet: str, cell: str) -> List[str]:
        if isinstance(n, (RangeRef, NameRef)):
            r = self.symbols.resolve(n, sheet)
            if isinstance(r, RangeRef):
                return [x for x in (self._numeric_item(k) for k in _range_keys(self.wb, r)) if x]
        if isinstance(n, (CellRef, NameRef)):
            sym = self.symbols.lookup(n.name, sheet) if isinstance(n, NameRef) else None
            r = self.symbols.resolve(n, sheet)
            if isinstance(r, CellRef) and (sym is None or sym.kind != "constant"):
                x = self._numeric_item(make_key(r.sheet, r.col, r.row))   # text/blank skipped
                return [x] if x else []
        return [self.expr(n, sheet, cell)]

    def expr(self, n: Node, sheet: str, cell: str) -> str:
        t = type(n)
        if t is Number:
            return _sql_lit(n.value)
        if t is String:
            return _sql_lit(n.value)
        if t is Bool:
            return "1" if n.value else "0"
        if t in (ErrorLit, Missing, ArrayLit):
            return "NULL"
        if t in (CellRef, RangeRef, NameRef):
            if t is NameRef:
                sym = self.symbols.lookup(n.name, sheet)
                if sym is not None and sym.kind == "constant":
                    return self.expr(sym.target, sheet, cell)
            r = self.symbols.resolve(n, sheet)
            if isinstance(r, CellRef):
                return self.col(make_key(r.sheet, r.col, r.row))
            self.unsupported[cell] = "unresolved reference" if r is None else "range used as a value"
            return "NULL"
        if t is BinOp:
            a, b = self.expr(n.left, sheet, cell), self.expr(n.right, sheet, cell)
            if n.op == "/":
                return f"(CASE WHEN {b} = 0 THEN NULL ELSE {a} * 1.0 / {b} END)"
            if n.op == "^":
                return f"POWER({a}, {b})"
            if n.op == "&":
                return f"(CAST({a} AS TEXT) || CAST({b} AS TEXT))"
            op = {"<>": "<>", "=": "="}.get(n.op, n.op)
            return f"({a} {op} {b})"
        if t is UnaryOp:
            a = self.expr(n.operand, sheet, cell)
            return f"(-{a})" if n.op == "-" else a
        if t is Percent:
            return f"({self.expr(n.operand, sheet, cell)} / 100.0)"
        if t is FuncCall:
            name = n.name
            if not arity_ok(name, len(n.args)):
                return "NULL /* wrong number of arguments: #N/A */"
            if name not in SQL_SUPPORTED:
                self.unsupported[cell] = name
                return f"NULL /* {name} not supported in SQL */"
            if name in ("SUM", "AVERAGE", "COUNT"):
                items = [x for a in n.args for x in self._items(a, sheet, cell)]
                total = "(" + " + ".join(items) + ")" if items else "0"
                if name == "SUM":
                    return total
                if name == "COUNT":                     # errors (NULL) are not counted
                    return ("(" + " + ".join(f"(CASE WHEN {x} IS NULL THEN 0 ELSE 1 END)"
                                             for x in items) + ")") if items else "0"
                return f"({total} * 1.0 / {len(items)})" if items else "NULL"
            if name in ("MIN", "MAX"):
                items = [x for a in n.args for x in self._items(a, sheet, cell)]
                if not items:
                    return "0"
                return items[0] if len(items) == 1 else f"{name}({', '.join(items)})"
            args = [self.expr(a, sheet, cell) for a in n.args]
            if name == "IF":
                return (f"(CASE WHEN ({args[0]}) IS NULL THEN NULL WHEN {args[0]} THEN {args[1] if len(args) > 1 else '0'} "
                        f"ELSE {args[2] if len(args) > 2 else '0'} END)")
            if name == "IFERROR":
                return f"COALESCE({args[0]}, {args[1]})"
            if name in ("AND", "OR"):
                args = [s for a, s in zip(n.args, args)
                        if s != "0" or not isinstance(a, (CellRef, NameRef))]
                if not args:
                    return "NULL"                       # only blank cells: #VALUE!
                nulls = " OR ".join(f"({a}) IS NULL" for a in args)
                body = f" {name} ".join(f"({a}) <> 0" for a in args)
                return f"(CASE WHEN {nulls} THEN NULL ELSE ({body}) END)"   # errors propagate
            if name == "NOT":
                return f"(NOT {args[0]})"
            if name == "INT":                   # big floats are integers already (and overflow CAST)
                x = args[0]
                return (f"(CASE WHEN ABS({x}) >= 4503599627370496 THEN {x} ELSE "
                        f"(CAST({x} AS INTEGER) - ({x} < CAST({x} AS INTEGER))) END)")
            if name == "PI":
                return "3.141592653589793"
            if name == "ROUND":
                # half away from zero on the decimal value, like a spreadsheet: 0.285 -> 0.29
                # although the double is 0.28499999999999998 (SQLite's ROUND gives 0.28)
                x = args[0]
                d = n.args[1] if len(n.args) > 1 else None
                f = (repr(10.0 ** int(d.value)) if isinstance(d, Number) and abs(d.value) <= 15
                     else f"POWER(10, (SELECT CASE WHEN k > 15 THEN 15 WHEN k < -308 THEN -308 "
                          f"ELSE k END FROM (SELECT CAST({args[1] if len(args) > 1 else 0} "
                          f"AS INTEGER) AS k)))")
                return (f"(CASE WHEN ABS({x}) * {f} >= 4503599627370496 THEN ({x}) ELSE "
                        f"(CASE WHEN ({x}) < 0 THEN -1.0 ELSE 1.0 END) * "
                        f"CAST(ABS({x}) * {f} + 0.5 + 1e-9 AS INTEGER) / {f} END)")
            return f"{name}({', '.join(args)})"
        return "NULL"

    def generate(self) -> str:
        cyc = {k for c in self.graph.cycles for k in c} | set(self.graph.blocked)
        # only inputs that some formula reads (dead-input elimination, as in codegen.py)
        inputs = [c for c in self.wb.cells() if not c.is_formula and self.graph.succs.get(c.key)]
        cols = [f"{_sql_lit(input_value(c.raw))} AS {_q(c.key)}" for c in inputs]
        ctes = ["inputs AS (\n  SELECT " + ",\n         ".join(cols or ["1 AS dummy"]) + "\n)"]
        prev = "inputs"
        level_of = {}
        for i, lvl in enumerate(self.graph.levels):
            for k in lvl:
                level_of[k] = i
        # group formula cells by topological level; cycle cells go last as NULL
        groups: Dict[int, List[str]] = {}
        for c in self.wb.formula_cells():
            lv = level_of.get(c.key, 10 ** 6)
            groups.setdefault(lv, []).append(c.key)
        n = 0
        for lv in sorted(groups):
            parts = []
            for key in sorted(groups[lv], key=_sort_key):
                c = self.wb.cell(key)
                ast = self.opaque.get(key) or self.asts.get(key) or c.ast
                if key in cyc or ast is None:
                    e = "NULL"
                else:
                    e = self.expr(ast, c.sheet, key)
                    if e not in ("NULL",) and not e.startswith("'"):
                        # overflow: a spreadsheet shows #NUM! where SQL would keep Inf
                        e = f"(CASE WHEN ABS({e}) > 1.7976931348623157e308 THEN NULL ELSE {e} END)"
                raw = str(c.raw).replace("*/", "* /")
                parts.append(f"    /* {raw} */ {e} AS {_q(key)}")
            n += 1
            name = f"l{n}"
            # OFFSET 0 is an optimization fence (SQLite and PostgreSQL): without it the query
            # flattener substitutes every column's expression into the next level, which
            # grows exponentially along a chain such as running totals.
            ctes.append(f"{name} AS (\n  SELECT *,\n" + ",\n".join(parts) +
                        f"\n  FROM {prev} LIMIT 1 OFFSET 0\n)")
            prev = name
        self.levels = n
        return ("-- generated by SheetGuard: one CTE per topological level of the dependency DAG\n"
                "WITH " + ",\n".join(ctes) + f"\nSELECT * FROM {prev};\n")


# =========================================================================== verification
def _norm(v):
    if isinstance(v, rt.XLError):
        return ("err", v.code)
    if isinstance(v, dict) and "err" in v:
        return ("err", v["err"])
    if isinstance(v, bool):
        return ("num", 1.0 if v else 0.0)
    if isinstance(v, (int, float)):
        return ("num", float(v))
    if v is None:
        return ("empty", None)
    return ("text", str(v))


def _same(a, b, sql=False) -> bool:
    ka, va = a
    kb, vb = b
    if sql and ka == "err":
        return kb == "empty"                     # SQL represents every error as NULL
    if sql and ka == "empty" and b == ("num", 0.0):
        return True                              # SQL has no blank: a blank result reads as 0
    if ka == "num" and kb == "num":
        return abs(va - vb) <= 1e-9 * max(1.0, abs(va), abs(vb))
    return a == b


def verify_js(wb, rep, source: str, unsupported: Dict = None) -> Dict:
    node = shutil.which("node")
    if not node:
        return {"ran": False, "reason": "Node.js is not installed"}
    ref = Interpreter(wb, rep.symbols, rep.graph).run()
    inputs = {c.key: _json_input(input_value(c.raw)) for c in wb.cells() if not c.is_formula}
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as fh:
        fh.write(source)
        path = fh.name
    try:
        p = subprocess.run([node, path], input=json.dumps(inputs), capture_output=True,
                           text=True, timeout=30, encoding="utf-8")
    except subprocess.TimeoutExpired:
        return {"ran": True, "ok": False, "reason": "the generated JavaScript ran for more than 30 s"}
    except OSError as e:
        return {"ran": False, "reason": f"could not start Node.js: {e}"}
    finally:
        os.unlink(path)
    if p.returncode != 0:
        return {"ran": True, "ok": False, "reason": p.stderr[-400:]}
    try:
        out = json.loads(p.stdout)
    except ValueError:
        return {"ran": True, "ok": False, "reason": "Node.js returned no result: " + p.stderr[-300:]}
    return _compare(wb, ref, out, sql=False, rep=rep, unsupported=unsupported)


def _json_input(v):
    if isinstance(v, rt.XLError):
        return {"err": v.code}
    return v


def verify_sql(wb, rep, source: str, unsupported: Dict = None) -> Dict:
    ref = Interpreter(wb, rep.symbols, rep.graph).run()
    con = sqlite3.connect(":memory:")
    for name, fn, n in (("POWER", _safe(math.pow), 2), ("SQRT", _safe(math.sqrt), 1)):
        con.create_function(name, n, fn)        # spreadsheet semantics: overflow -> NULL (#NUM!)
    deadline = time.monotonic() + 15
    con.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 20000)
    try:
        cur = con.execute(source)
        names = [d[0] for d in cur.description]
        row = cur.fetchone()
    except sqlite3.Error as e:
        return {"ran": True, "ok": False, "reason": str(e)}
    finally:
        con.close()
    out = dict(zip(names, row))
    return _compare(wb, ref, out, sql=True, rep=rep, unsupported=unsupported)


def _safe(fn):
    def f(*a):
        try:
            r = fn(*a)
        except (ValueError, OverflowError, TypeError, ZeroDivisionError):
            return None
        return None if isinstance(r, float) and (math.isinf(r) or math.isnan(r)) else r
    return f


_CMP_OPS = ("=", "<>", "<", ">", "<=", ">=")


def _touches_text(wb, rep, key: str, ref=None) -> bool:
    """Does this cell, or anything it depends on, involve a text or boolean value?"""
    for k in [key] + list(rep.graph.precedents(key)):
        c = wb.cell(k)
        if c is None:
            continue
        if ref is not None and isinstance(ref.get(k), (bool, str)):
            return True
        if c.ast is not None and any(isinstance(x, (BinOp, FuncCall)) and any(
                isinstance(y, Bool) or (isinstance(y, BinOp) and y.op in _CMP_OPS) or
                (isinstance(y, FuncCall) and y.name in ("AND", "OR", "NOT"))
                for y in x.children()) for x in c.ast.walk()):
            return True
        if not c.is_formula:
            if isinstance(c.raw, (str, bool)) and c.raw != "":
                return True
        elif c.ast is not None and any(isinstance(x, String) or
                                       (isinstance(x, BinOp) and x.op == "&")
                                       or (isinstance(x, FuncCall) and x.name in ("UPPER", "LOWER",
                                           "LEN", "CONCATENATE", "CONCAT", "LEFT", "TEXT"))
                                       for x in c.ast.walk()):
            return True
    return False


def _compare(wb, ref, out, sql: bool, rep=None, unsupported: Dict = None) -> Dict:
    total = same = 0
    diffs = []
    from .typecheck import parse_numeric_text
    text_nums = {c.key for c in wb.cells() if not c.is_formula and isinstance(c.raw, str)
                 and parse_numeric_text(c.raw) is not None}
    for c in wb.formula_cells():
        if c.key not in out:
            continue
        a, b = _norm(ref.get(c.key)), _norm(out.get(c.key))
        if sql and a[0] == "text":
            continue                              # text formatting differs by design in SQL
        total += 1
        if _same(a, b, sql):
            same += 1
        else:
            why = ""
            uns = unsupported or {}
            culprit = c.key if c.key in uns else next(
                (k for k in (rep.graph.precedents(c.key) if rep is not None else []) if k in uns), None)
            if culprit:
                why = (f"uses {uns[culprit]}(), which this back-end does not implement"
                       + ("" if culprit == c.key else f" (in {culprit})"))
            elif rep is not None and text_nums & set(rep.graph.precedents(c.key)):
                why = ("depends on a number stored as text: SQL converts it to 0, spreadsheets "
                       "to its value (the type checker flags this as SG305)")
            elif sql and rep is not None and _touches_text(wb, rep, c.key, ref):
                why = ("depends on a text or TRUE/FALSE value: SQL stores booleans as 0/1 and "
                       "silently converts text where a spreadsheet raises #VALUE!")
            diffs.append({"cell": c.key, "interpreter": str(a[1]), "target": str(b[1]),
                          "explained": why})
    unexplained = [d for d in diffs if not d["explained"]]
    return {"ran": True, "ok": not diffs, "ok_except_flagged": not unexplained, "cells": total,
            "matching": same, "diffs": diffs[:20], "unexplained": len(unexplained)}


def multitarget_report(wb, rep, verify: bool = True) -> Dict:
    asts = rep.optimization.asts if rep.optimization else None
    js = JSGenerator(wb, rep.symbols, rep.graph, asts)
    js_src = js.generate()
    sq = SQLGenerator(wb, rep.symbols, rep.graph, asts)
    sql_src = sq.generate()
    out = {
        "javascript": {"source": js_src, "lines": js_src.count("\n"),
                       "formula_cells": js.formula_cells, "unsupported": js.unsupported},
        "sql": {"source": sql_src, "lines": sql_src.count("\n"), "levels": sq.levels,
                "unsupported": sq.unsupported, "dialect": "SQLite 3 (PostgreSQL-compatible syntax)"},
    }
    if verify:
        out["javascript"]["verification"] = verify_js(wb, rep, js_src, js.unsupported)
        out["sql"]["verification"] = verify_sql(wb, rep, sql_src, sq.unsupported)
    return out
