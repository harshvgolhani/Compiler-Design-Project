"""Intermediate code: three-address code (TAC), basic blocks, DAGs, and target code.

This is the textbook back-end path, applied to spreadsheet formulas:

  AST  --syntax-directed translation-->  three-address code
        shown as quadruples, triples and indirect triples
  TAC  --leaders algorithm-->            basic blocks + flow graph
  each block --value numbering-->        DAG of the basic block, and
        local optimizations: constant folding, algebraic identities, common
        sub-expression elimination, copy propagation, dead-code elimination
  TAC  --simple code generator-->        assembly for a small register machine
        (getReg with register and address descriptors, spilling)

Example:  =IF(B2>0, B2*C2*(1+TaxRate), 0)

    t1 = B2 > 0
    ifFalse t1 goto L1
    t2 = B2 * C2
    t3 = 1 + Inputs!B2
    t4 = t2 * t3
    t5 = t4
    goto L2
  L1:
    t5 = 0
  L2:
    D2 = t5
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .functions import arity_ok
from .ast_nodes import (ArrayLit, BinOp, Bool, CellRef, ErrorLit, FuncCall, Missing, NameRef,
                        Node, Number, Percent, RangeRef, String, UnaryOp)
from .parser import parse

ARITH = {"+", "-", "*", "/", "^", "&", "=", "<>", "<", ">", "<=", ">="}
COMMUTATIVE = {"+", "*", "=", "<>"}


@dataclass
class Quad:
    op: str
    arg1: str = ""
    arg2: str = ""
    result: str = ""

    def text(self) -> str:
        o, a, b, r = self.op, self.arg1, self.arg2, self.result
        if o == "label":
            return f"{r}:"
        if o == "goto":
            return f"    goto {r}"
        if o == "ifFalse":
            return f"    ifFalse {a} goto {r}"
        if o == "param":
            return f"    param {a}"
        if o == "call":
            return f"    {r} = call {a}, {b}"
        if o == "copy":
            return f"    {r} = {a}"
        if o == "uminus":
            return f"    {r} = - {a}"
        return f"    {r} = {a} {o} {b}"

    def to_dict(self) -> dict:
        return {"op": self.op, "arg1": self.arg1, "arg2": self.arg2, "result": self.result,
                "text": self.text().strip()}


def _is_temp(x: str) -> bool:
    return x.startswith("t") and x[1:].isdigit()


def _is_const(x: str) -> bool:
    if not x:
        return False
    if x[0] == '"' or x in ("TRUE", "FALSE"):
        return True
    try:
        float(x)
        return True
    except ValueError:
        return False


def _num(x: str) -> Optional[float]:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _fmt_num(v: float) -> str:
    if float(v).is_integer() and abs(v) < 1e15:
        return str(int(v))
    return repr(float(v))          # exact: folding must not lose precision


# --------------------------------------------------------------------------- translation
class TACGen:
    """Syntax-directed translation of ASTs into three-address code."""

    def __init__(self, symbols=None, qualify: bool = False):
        self.symbols = symbols
        self.qualify = qualify
        self.code: List[Quad] = []
        self.ntemp = 0
        self.nlabel = 0

    def temp(self) -> str:
        self.ntemp += 1
        return f"t{self.ntemp}"

    def label(self) -> str:
        self.nlabel += 1
        return f"L{self.nlabel}"

    def emit(self, *a) -> None:
        self.code.append(Quad(*a))

    def addr(self, n: Node, sheet: str) -> str:
        """Name of a reference operand (qualified when crossing sheets)."""
        ref = None
        if self.symbols is not None and isinstance(n, (CellRef, RangeRef, NameRef)):
            ref = self.symbols.resolve(n, sheet)
        if ref is None:
            return n.unparse()
        if isinstance(ref, RangeRef):          # drop $ markers: addresses, not copy semantics
            ref.abs_col1 = ref.abs_col2 = ref.abs_row1 = ref.abs_row2 = False
        else:
            ref.abs_col = ref.abs_row = False
        text = ref.unparse()
        if not self.qualify and ref.sheet == sheet:
            return text.split("!", 1)[1] if "!" in text else text
        return text

    def gen(self, n: Node, sheet: str = "") -> str:
        if isinstance(n, Number):
            return _fmt_num(n.value)
        if isinstance(n, String):
            return '"' + n.value + '"'
        if isinstance(n, Bool):
            return "TRUE" if n.value else "FALSE"
        if isinstance(n, ErrorLit):
            return n.value
        if isinstance(n, Missing):
            return "BLANK"                       # an omitted argument: IF(A1,,0)
        if isinstance(n, ArrayLit):
            return n.unparse()
        if isinstance(n, (CellRef, RangeRef)):
            return self.addr(n, sheet)
        if isinstance(n, NameRef):
            if self.symbols is not None:
                sym = self.symbols.lookup(n.name, sheet)
                if sym is not None and sym.kind == "constant":
                    return self.gen(sym.target, sheet)
            # a named range is a variable; the whole-workbook TAC uses its resolved
            # address so that  Discount  and  Inputs!B3  get the same value number
            return self.addr(n, sheet) if self.qualify else n.name
        if isinstance(n, BinOp):
            a = self.gen(n.left, sheet)
            b = self.gen(n.right, sheet)
            t = self.temp()
            self.emit(n.op, a, b, t)
            return t
        if isinstance(n, UnaryOp):
            a = self.gen(n.operand, sheet)
            if n.op == "+":
                return a
            t = self.temp()
            self.emit("uminus", a, "", t)
            return t
        if isinstance(n, Percent):
            a = self.gen(n.operand, sheet)
            t = self.temp()
            self.emit("/", a, "100", t)
            return t
        if isinstance(n, FuncCall):
            if n.name == "IF" and len(n.args) in (2, 3):
                return self._if(n, sheet)
            args = [self.gen(a, sheet) for a in n.args]
            for a in args:
                self.emit("param", a)
            t = self.temp()
            self.emit("call", n.name, str(len(args)), t)
            return t
        return n.unparse()

    def _if(self, n: FuncCall, sheet: str) -> str:
        """IF(c, a, b) with short-circuit control flow (only one branch runs)."""
        c = self.gen(n.args[0], sheet)
        res = self.temp()
        l_else, l_end = self.label(), self.label()
        self.emit("ifFalse", c, "", l_else)
        a = self.gen(n.args[1], sheet)
        self.emit("copy", a, "", res)
        self.emit("goto", "", "", l_end)
        self.emit("label", "", "", l_else)
        b = self.gen(n.args[2], sheet) if len(n.args) > 2 else "FALSE"
        self.emit("copy", b, "", res)
        self.emit("label", "", "", l_end)
        return res

    def cell(self, target: str, ast: Node, sheet: str) -> None:
        r = self.gen(ast, sheet)
        self.emit("copy", r, "", target)


def translate(formula: str, target: str = "result", symbols=None, sheet: str = "") -> List[Quad]:
    g = TACGen(symbols)
    g.cell(target, parse(formula), sheet)
    return g.code


def translate_workbook(wb, symbols, graph, max_cells: Optional[int] = None) -> List[Quad]:
    """The whole workbook as one TAC program, cells in topological order
    (optionally only the first `max_cells` formula cells, for very large workbooks)."""
    from .evaluator import evaluation_order
    g = TACGen(symbols, qualify=True)
    cyc = {k for c in graph.cycles for k in c}
    n = 0
    for key in evaluation_order(graph):
        c = wb.cell(key)
        if c is None or not c.is_formula or c.ast is None or key in cyc:
            continue
        if max_cells is not None and n >= max_cells:
            break
        g.cell(key, c.ast, c.sheet)
        n += 1
    return g.code


# --------------------------------------------------------------------------- representations
def triples(code: List[Quad]) -> List[dict]:
    """Triples: results are referred to by the position (i) of the triple that computes
    them, so there is no result field. Labels disappear; jumps point at triple numbers."""
    pos_of_label = {}
    out_index = 0
    for q in code:
        if q.op == "label":
            pos_of_label[q.result] = out_index
        else:
            out_index += 1
    defined: Dict[str, int] = {}
    rows = []
    i = 0
    for q in code:
        if q.op == "label":
            continue

        def ref(x):
            return f"({defined[x]})" if x in defined else x
        if q.op == "copy":
            rows.append({"i": i, "op": "=", "arg1": q.result, "arg2": ref(q.arg1)})
            if _is_temp(q.result):
                defined[q.result] = i
        elif q.op in ("goto",):
            rows.append({"i": i, "op": "goto", "arg1": f"({pos_of_label[q.result]})", "arg2": ""})
        elif q.op == "ifFalse":
            rows.append({"i": i, "op": "ifFalse", "arg1": ref(q.arg1),
                         "arg2": f"({pos_of_label[q.result]})"})
        elif q.op == "param":
            rows.append({"i": i, "op": "param", "arg1": ref(q.arg1), "arg2": ""})
        else:
            rows.append({"i": i, "op": q.op, "arg1": ref(q.arg1), "arg2": ref(q.arg2)})
            defined[q.result] = i
        i += 1
    return rows


def indirect_triples(code: List[Quad], base: int = 100) -> List[dict]:
    """Indirect triples: a statement list of pointers into the triple table, so the
    optimizer can reorder statements without renumbering the triples."""
    return [{"statement": base + k, "points_to": f"({t['i']})"} for k, t in
            enumerate(triples(code))]


# --------------------------------------------------------------------------- basic blocks
def basic_blocks(code: List[Quad]) -> List[dict]:
    """Leaders: (1) the first instruction, (2) any jump target, (3) any instruction
    that follows a jump. A block runs from a leader up to the next leader."""
    if not code:
        return []
    leaders = {0}
    for i, q in enumerate(code):
        if q.op == "label":
            leaders.add(i)
        if q.op in ("goto", "ifFalse") and i + 1 < len(code):
            leaders.add(i + 1)
    starts = sorted(leaders)
    blocks = []
    for b, s in enumerate(starts):
        e = starts[b + 1] if b + 1 < len(starts) else len(code)
        blocks.append({"id": f"B{b + 1}", "start": s, "end": e,
                       "label": code[s].result if code[s].op == "label" else None,
                       "code": [q.text() for q in code[s:e]], "succ": []})
    label_block = {blk["label"]: blk["id"] for blk in blocks if blk["label"]}
    for b, blk in enumerate(blocks):
        last = code[blk["end"] - 1]
        nxt = blocks[b + 1]["id"] if b + 1 < len(blocks) else None
        if last.op == "goto":
            blk["succ"] = [label_block[last.result]]
        elif last.op == "ifFalse":
            blk["succ"] = [x for x in (nxt, label_block[last.result]) if x]
        elif nxt:
            blk["succ"] = [nxt]
    return blocks


# --------------------------------------------------------------------------- optimization
def _fold(op: str, a: float, b: float) -> Optional[str]:
    try:
        if op == "+":
            v = a + b
        elif op == "-":
            v = a - b
        elif op == "*":
            v = a * b
        elif op == "/":
            v = a / b if b else None
        elif op == "^":
            v = a ** b
        else:
            v = None
    except (OverflowError, ZeroDivisionError, ValueError):
        return None
    if v is None or isinstance(v, complex):
        return None
    return _fmt_num(v)


def optimize_tac(code: List[Quad]) -> Tuple[List[Quad], List[str], List[dict]]:
    """Local optimization of each basic block by value numbering (= building the
    block's DAG), followed by global dead-temporary elimination.
    Returns (optimized code, log of applied transformations, DAG of every block)."""
    log: List[str] = []
    out: List[Quad] = []
    dags: List[dict] = []
    blocks = basic_blocks(code)
    for blk in blocks:
        seg = code[blk["start"]:blk["end"]]
        new, dag = _value_number_block(seg, log, blk["id"])
        out.extend(new)
        dags.append(dag)
    # dead-code elimination: drop assignments to temps that are never used
    changed = True
    while changed:
        changed = False
        used = set()
        for q in out:
            for a in (q.arg1, q.arg2):
                if a:
                    used.add(a)
        keep = []
        for q in out:
            if q.op in ARITH | {"copy", "uminus"} and _is_temp(q.result) and q.result not in used:
                log.append(f"dead code: removed '{q.text().strip()}' ({q.result} is never used)")
                changed = True
                continue
            keep.append(q)
        out = keep
    return out, log, dags


def _value_number_block(seg: List[Quad], log: List[str], bid: str):
    vn_of: Dict[str, int] = {}          # name -> value number
    const_of: Dict[int, str] = {}       # value number -> constant text
    holder: Dict[int, str] = {}         # value number -> a name currently holding it
    expr_vn: Dict[tuple, int] = {}      # (op, vn1, vn2) -> value number
    nodes: List[dict] = []              # DAG nodes
    counter = [0]
    out: List[Quad] = []
    numeric: set = set()                # value numbers known to be numbers (or errors)

    def is_num(v: int) -> bool:
        return v in numeric or (v in const_of and _num(const_of[v]) is not None)

    def new_vn(label: str, kind: str, kids=()) -> int:
        counter[0] += 1
        nodes.append({"id": counter[0], "label": label, "kind": kind, "kids": list(kids),
                      "names": []})
        return counter[0]

    def vn(x: str) -> int:
        if x not in vn_of:
            v = new_vn(x, "leaf")
            vn_of[x] = v
            holder.setdefault(v, x)
            if _is_const(x):
                const_of[v] = x
        return vn_of[x]

    def name_for(v: int) -> str:
        return const_of.get(v) or holder.get(v)

    def attach(name: str, v: int):
        old = vn_of.get(name)
        if old is not None and holder.get(old) == name:
            holder.pop(old, None)
            for n in nodes:
                if n["id"] == old and name in n["names"]:
                    n["names"].remove(name)
        vn_of[name] = v
        if v not in holder or holder[v] not in vn_of or vn_of.get(holder[v]) != v:
            holder[v] = name
        for n in nodes:
            if n["id"] == v and name not in n["names"]:
                n["names"].append(name)

    for q in seg:
        if q.op in ("label", "goto", "ifFalse", "param", "call"):
            a = q.arg1
            if q.op in ("ifFalse", "param") and a in vn_of:
                rep = name_for(vn_of[a])
                if rep and rep != a:
                    log.append(f"{bid}: copy propagation in '{q.text().strip()}': {a} → {rep}")
                    q = Quad(q.op, rep, q.arg2, q.result)
            if q.op == "call":
                v = new_vn(f"call {q.arg1}", "op")
                attach(q.result, v)
            out.append(q)
            continue
        if q.op == "copy":
            v = vn(q.arg1)
            rep = name_for(v)
            if rep != q.arg1:
                log.append(f"{bid}: copy propagation: {q.result} = {q.arg1} → {q.result} = {rep}")
            out.append(Quad("copy", rep, "", q.result))
            attach(q.result, v)
            continue
        if q.op == "uminus":
            v1 = vn(q.arg1)
            c = const_of.get(v1)
            if c is not None and _num(c) is not None:
                folded = _fmt_num(-_num(c))
                log.append(f"{bid}: constant folding: - {c} → {folded}")
                out.append(Quad("copy", folded, "", q.result))
                attach(q.result, vn(folded))
                continue
            key = ("uminus", v1, None)
            if key in expr_vn and name_for(expr_vn[key]):
                v = expr_vn[key]
                log.append(f"{bid}: common subexpression: {q.result} = - {q.arg1} reuses "
                           f"{name_for(v)}")
                out.append(Quad("copy", name_for(v), "", q.result))
                attach(q.result, v)
                continue
            v = new_vn("uminus", "op", [v1])
            numeric.add(v)
            expr_vn[key] = v
            out.append(Quad("uminus", name_for(v1), "", q.result))
            attach(q.result, v)
            continue

        # binary operator
        v1, v2 = vn(q.arg1), vn(q.arg2)
        a, b = name_for(v1), name_for(v2)
        if (a, b) != (q.arg1, q.arg2):
            log.append(f"{bid}: copy propagation in {q.result}: {q.arg1} {q.op} {q.arg2} → "
                       f"{a} {q.op} {b}")
        ca, cb = const_of.get(v1), const_of.get(v2)
        na, nb = _num(ca) if ca else None, _num(cb) if cb else None
        # constant folding
        if na is not None and nb is not None and q.op in "+-*/^":
            folded = _fold(q.op, na, nb)
            if folded is not None:
                log.append(f"{bid}: constant folding: {ca} {q.op} {cb} → {folded}")
                out.append(Quad("copy", folded, "", q.result))
                attach(q.result, vn(folded))
                continue
        # algebraic identities, only when the kept operand is known to be a number:
        # t + 0 must stay when t is text ("x"+0 is #VALUE!) or a boolean (TRUE+0 is 1)
        ident = None
        if q.op == "*" and nb == 1 and is_num(v1):
            ident = a
        elif q.op == "*" and na == 1 and is_num(v2):
            ident = b
        elif q.op in "+-" and nb == 0 and is_num(v1):
            ident = a
        elif q.op == "+" and na == 0 and is_num(v2):
            ident = b
        elif q.op == "/" and nb == 1 and is_num(v1):
            ident = a
        if ident is not None:
            log.append(f"{bid}: algebraic identity: {a} {q.op} {b} → {ident}")
            out.append(Quad("copy", ident, "", q.result))
            attach(q.result, vn(ident))
            continue
        k1, k2 = (v1, v2)
        if q.op in COMMUTATIVE and k1 > k2:
            k1, k2 = k2, k1
        key = (q.op, k1, k2)
        if key in expr_vn and name_for(expr_vn[key]):
            v = expr_vn[key]
            log.append(f"{bid}: common subexpression: {q.result} = {a} {q.op} {b} reuses "
                       f"{name_for(v)}")
            out.append(Quad("copy", name_for(v), "", q.result))
            attach(q.result, v)
            continue
        v = new_vn(q.op, "op", [v1, v2])
        if q.op in ("+", "-", "*", "/", "^"):
            numeric.add(v)
        expr_vn[key] = v
        out.append(Quad(q.op, a, b, q.result))
        attach(q.result, v)

    # names attached to leaves that are their own initial value
    for n in nodes:
        if n["kind"] == "leaf" and n["label"] not in n["names"]:
            pass
    dag = {"block": bid, "nodes": nodes}
    return out, dag


# --------------------------------------------------------------------------- target code
_MNEMONIC = {"+": "ADD", "-": "SUB", "*": "MUL", "/": "DIV", "^": "POW", "&": "CAT",
             "=": "CEQ", "<>": "CNE", "<": "CLT", ">": "CGT", "<=": "CLE", ">=": "CGE"}


class CodeGen:
    """The 'simple code generator' (Aho et al., §8.6) for a load/store machine with
    `nregs` registers. Register descriptors track what each register holds; address
    descriptors track where the current value of each variable lives. getReg prefers
    a register already holding the operand, then an empty one, then spills."""

    def __init__(self, nregs: int = 4):
        nregs = max(2, int(nregs))
        self.n = nregs
        self.live: set = set()
        self.live_after: set = set()
        self.reg: Dict[str, List[str]] = {f"R{i}": [] for i in range(nregs)}
        self.where: Dict[str, set] = {}
        self.asm: List[str] = []
        self.stats = {"instructions": 0, "loads": 0, "stores": 0, "spills": 0}

    def out(self, s: str, kind: Optional[str] = None):
        self.asm.append(s)
        if not s.endswith(":"):
            self.stats["instructions"] += 1
        if kind:
            self.stats[kind] += 1

    def _in_reg(self, x: str) -> Optional[str]:
        for r, names in self.reg.items():
            if x in names:
                return r
        return None

    def _free(self, avoid=()) -> str:
        live = getattr(self, "live", set())
        for r, names in self.reg.items():
            if not names and r not in avoid:
                return r
        # a register holding only dead temporaries / constants can be reused for free
        for r, names in self.reg.items():
            if r not in avoid and all(_is_const(x) or (_is_temp(x) and x not in live)
                                      for x in names):
                self._clear(r)
                return r
        # choose a register whose contents are all safely in memory
        for r, names in self.reg.items():
            if r in avoid:
                continue
            if all("mem" in self.where.get(x, set()) or _is_const(x) for x in names):
                self._clear(r)
                return r
        r = next(r for r in self.reg if r not in avoid)
        for x in self.reg[r]:
            if not _is_const(x) and "mem" not in self.where.get(x, set()):
                self.out(f"    ST   {x}, {r}", "stores")
                self.stats["spills"] += 1
                self.where.setdefault(x, set()).add("mem")
        self._clear(r)
        return r

    def _dest(self, ra: str, rb: str) -> str:
        """Destination register for  x = a op b  (operands already in ra, rb)."""
        live = self.live_after

        def reusable(r):
            return all(_is_const(x) or (_is_temp(x) and x not in live) or
                       ("mem" in self.where.get(x, set()) and
                        not _is_temp(x)) for x in self.reg[r])
        if reusable(ra):
            self._clear(ra)
            return ra
        if any(r not in (ra, rb) for r in self.reg):
            return self._free(avoid=(ra, rb))
        for x in self.reg[ra]:                    # only two registers: spill a's holder
            if not _is_const(x) and x in live and "mem" not in self.where.get(x, set()):
                self.out(f"    ST   {x}, {ra}", "stores")
                self.stats["spills"] += 1
                self.where.setdefault(x, set()).add("mem")
        return ra

    def _clear(self, r: str):
        for x in self.reg[r]:
            self.where.get(x, set()).discard(r)
        self.reg[r] = []

    def load(self, x: str, avoid=()) -> str:
        r = self._in_reg(x)
        if r:
            return r
        r = self._free(avoid)
        self.out(f"    LD   {r}, {'#' + x if _is_const(x) else x}", "loads")
        self.reg[r] = [x]
        self.where.setdefault(x, set()).update({r} | (set() if _is_const(x) else {"mem"}))
        return r

    def assign(self, x: str, r: str):
        for rr, names in self.reg.items():
            if x in names:
                names.remove(x)
        for y in self.reg[r]:
            self.where.get(y, set()).discard(r)
        self.reg[r] = [x]
        self.where[x] = {r}

    def flush(self, keep_temps: set):
        """End of a basic block: store every live variable that is only in a register."""
        for r, names in self.reg.items():
            for x in names:
                if _is_const(x):
                    continue
                if (not _is_temp(x) or x in keep_temps) and "mem" not in self.where.get(x, set()):
                    self.out(f"    ST   {x}, {r}", "stores")
                    self.where[x].add("mem")
        for r in self.reg:
            self.reg[r] = []
        for x in self.where:
            self.where[x] = {"mem"} & self.where[x]

    def generate(self, code: List[Quad]) -> List[str]:
        blocks = basic_blocks(code)
        uses_by_block: Dict[str, set] = {}
        for b in blocks:
            s = set()
            for q in code[b["start"]:b["end"]]:
                s.update(x for x in (q.arg1, q.arg2) if _is_temp(x))
            uses_by_block[b["id"]] = s
        # next-use information: names read at or after each instruction
        used_after: List[set] = [set() for _ in range(len(code) + 1)]
        for i in range(len(code) - 1, -1, -1):
            used_after[i] = used_after[i + 1] | {x for x in (code[i].arg1, code[i].arg2) if x}
        for bi, b in enumerate(blocks):
            later = set()
            for other in blocks:
                if other["id"] != b["id"]:
                    later |= uses_by_block[other["id"]]
            for idx in range(b["start"], b["end"]):
                q = code[idx]
                self.live = used_after[idx]          # operands of q are still needed
                self.live_after = used_after[idx + 1]
                o = q.op
                if o == "label":
                    self.out(f"{q.result}:")
                elif o == "goto":
                    self.flush(later)
                    self.out(f"    BR   {q.result}")
                elif o == "ifFalse":
                    r = self.load(q.arg1)
                    self.flush(later)
                    self.out(f"    BRZ  {r}, {q.result}")
                elif o == "param":
                    r = self.load(q.arg1)
                    self.out(f"    PUSH {r}")
                elif o == "call":
                    r = self._free()
                    self.out(f"    CALL {q.arg1}, {q.arg2}")
                    self.out(f"    POP  {r}")
                    self.assign(q.result, r)
                elif o == "copy":
                    r = self.load(q.arg1)
                    self.reg[r].append(q.result)
                    self.where[q.result] = {r}
                elif o == "uminus":
                    ra = self.load(q.arg1)
                    rd = self._free(avoid=(ra,)) if self.reg[ra] != [q.arg1] else ra
                    self.out(f"    NEG  {rd}, {ra}")
                    self.assign(q.result, rd)
                else:
                    ra = self.load(q.arg1)
                    rb = self.load(q.arg2, avoid=(ra,))
                    rd = self._dest(ra, rb)
                    self.out(f"    {_MNEMONIC.get(o, o.upper()):<4} {rd}, {ra}, {rb}")
                    self.assign(q.result, rd)
            if bi == len(blocks) - 1 or code[b["end"] - 1].op not in ("goto", "ifFalse"):
                self.flush(later)
        return self.asm


def target_code(code: List[Quad], nregs: int = 4) -> Dict:
    g = CodeGen(nregs)
    asm = g.generate(code)
    return {"asm": asm, "stats": g.stats, "registers": nregs}


# --------------------------------------------------------------------------- report
def tac_report(formula: str, target: str = "result", symbols=None, sheet: str = "",
               nregs: int = 4) -> Dict:
    code = translate(formula, target, symbols, sheet)
    opt, log, dags = optimize_tac([Quad(q.op, q.arg1, q.arg2, q.result) for q in code])
    return {
        "quadruples": [dict(i=i, **q.to_dict()) for i, q in enumerate(code)],
        "triples": triples(code),
        "indirect": indirect_triples(code),
        "blocks": basic_blocks(code),
        "optimized": [q.to_dict() for q in opt],
        "log": log,
        "dags": dags,
        "target": target_code(opt, nregs),
        "target_unoptimized": target_code(code, nregs)["stats"],
        "counts": {"before": len([q for q in code if q.op != "label"]),
                   "after": len([q for q in opt if q.op != "label"])},
    }


WORKBOOK_TAC_CELLS = 120     # the teaching view: enough to show every optimization


def workbook_tac_report(wb, symbols, graph, nregs: int = 8) -> Dict:
    total = sum(1 for _ in wb.formula_cells())
    code = translate_workbook(wb, symbols, graph, WORKBOOK_TAC_CELLS)
    opt, log, _ = optimize_tac([Quad(q.op, q.arg1, q.arg2, q.result) for q in code])
    tc = target_code(opt, nregs)
    return {
        "before": len([q for q in code if q.op != "label"]),
        "after": len([q for q in opt if q.op != "label"]),
        "blocks": len(basic_blocks(code)),
        "temps": len({q.result for q in code if _is_temp(q.result)}),
        "log": log[:200], "log_count": len(log),
        "code": [q.text() for q in code[:400]],
        "optimized": [q.text() for q in opt[:400]],
        "asm": tc["asm"][:500], "asm_stats": tc["stats"],
        "pass_counts": _count_passes(log),
        "cells": min(total, WORKBOOK_TAC_CELLS), "total_cells": total,
        "truncated": total > WORKBOOK_TAC_CELLS,
    }


def _count_passes(log: List[str]) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for entry in log:
        for k in ("constant folding", "common subexpression", "copy propagation",
                  "algebraic identity", "dead code"):
            if k in entry:
                out[k] = out.get(k, 0) + 1
    return out


# --------------------------------------------------------------------------- TAC interpreter
def _first_error(*vs):
    """An operand that is an error value propagates before any type coercion (in the
    engines the failing sub-expression raises before the operator runs)."""
    from . import runtime as rt
    for v in vs:
        if isinstance(v, rt.XLError):
            raise v
    return vs if len(vs) > 1 else vs[0]


def _truthy_ok(v) -> bool:
    try:
        from . import runtime as rt
        rt.truthy(v)
        return True
    except Exception:
        return False


def run_tac(code: List[Quad], env: Dict[str, float]) -> Dict[str, object]:
    """Execute three-address code (arithmetic, comparisons, IF control flow).
    Used by the tests to prove the TAC optimizer preserves meaning."""
    from . import runtime as rt
    vals: Dict[str, object] = dict(env)
    labels = {q.result: i for i, q in enumerate(code) if q.op == "label"}
    params: List[object] = []

    def val(x):
        if x in vals:
            return vals[x]
        if _num(x) is not None:
            return float(x)
        if x in ("TRUE", "FALSE"):
            return x == "TRUE"
        if x.startswith('"'):
            return x.strip('"')
        return None

    def is_ref(x):                       # a cell / named-range operand (not a temp or constant)
        return bool(x) and not _is_temp(x) and not _is_const(x) and x != "BLANK"

    def if_end(i):
        """IF's code shape is  ifFalse c L_else; ...; copy a res; goto L_end; L_else: ...
        Returns (res, index of L_end) so that an error condition can skip both branches."""
        j = labels[code[i].result]
        g = code[j - 1]
        return (code[j - 2].result if j >= 2 and code[j - 2].op == "copy" else None,
                labels.get(g.result) if g.op == "goto" else None)

    pc, steps = 0, 0
    while pc < len(code) and steps < 100000:
        q = code[pc]
        steps += 1
        pc += 1
        try:
            if q.op == "label":
                continue
            if q.op == "goto":
                pc = labels[q.result]
            elif q.op == "ifFalse":
                c = val(q.arg1)
                if isinstance(c, rt.XLError) or not _truthy_ok(c):
                    res, end = if_end(pc - 1)       # IF(error, ...) is that error
                    if res is not None and end is not None:
                        vals[res] = c if isinstance(c, rt.XLError) else rt.VALUE
                        pc = end
                        continue
                if not rt.truthy(c):
                    pc = labels[q.result]
            elif q.op == "copy":
                vals[q.result] = val(q.arg1)
            elif q.op == "uminus":
                vals[q.result] = rt.neg(_first_error(val(q.arg1)))
            elif q.op == "param":
                params.append((val(q.arg1), is_ref(q.arg1)))
            elif q.op == "call":
                n = int(q.arg2)
                got, params = params[len(params) - n:], params[:len(params) - n]
                name = q.arg1
                if name in rt.LAZY:              # operands were computed eagerly (errors kept
                    def thunk(v):                # as values), so laziness changes nothing
                        def f():
                            if isinstance(v, rt.XLError):
                                raise v
                            return v
                        return f
                    vals[q.result] = rt.call_lazy(name, [thunk(v) for v, _ in got])
                elif name not in rt.ERROR_TOLERANT and rt.FUNCS.get(name) and \
                        any(isinstance(v, rt.XLError) for v, _ in got) and \
                        arity_ok(name, len(got)):
                    _first_error(*[v for v, _ in got])   # an erroring argument fails first
                elif name in rt.REF_AGG:         # SUM(A1): a reference skips text, like the engines
                    vals[q.result] = rt.call(name, [rt.Range([[v]]) if ref and not
                                                    isinstance(v, rt.Range) else v
                                                    for v, ref in got])
                else:
                    vals[q.result] = rt.call(name, [v for v, _ in got])
            elif q.op in ("=", "<>", "<", ">", "<=", ">="):
                vals[q.result] = rt.compare(q.op, *_first_error(val(q.arg1), val(q.arg2)))
            else:
                vals[q.result] = rt.BINOPS[q.op](*_first_error(val(q.arg1), val(q.arg2)))
        except rt.XLError as e:
            vals[q.result] = e
    return vals
