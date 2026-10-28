"""Formal grammar of the formula language + classical parser construction.

The production parser (parser.py) is hand-written recursive descent. This
module gives the *same language* a formal treatment, exactly as in the
syllabus, and builds two more parsers from the grammar mechanically:

  * an LL(1) grammar (left recursion removed, left-factored)
      -> FIRST and FOLLOW sets
      -> the predictive parsing table M[A, a]  (a conflict-free table proves LL(1))
      -> a table-driven predictive parser that prints its stack trace

  * an LR grammar (natural left-recursive form)
      -> LR(0) items, CLOSURE and GOTO, the canonical collection of item sets
      -> the SLR(1) ACTION / GOTO table (FOLLOW sets resolve reductions)
      -> a shift-reduce parser that prints its stack trace

Tests check that all three parsers accept and reject exactly the same formulas.
(Two checks are deliberately left to semantic analysis, as they are not
context-free: the rows of an array constant {1,2;3,4} must have the same length,
and both ends of Sheet1!A1:Sheet1!B2 must name the same sheet.)
Terminals are token classes produced by the lexer (see `terminal_of`).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, FrozenSet, List, Optional, Set, Tuple

from .errors import SheetGuardError
from .lexer import tokenize
from .tokens import T, Token

EPS = "ε"
END = "$"

Production = Tuple[str, Tuple[str, ...]]          # (head, body)

CMP_OPS = ("=", "<>", "<", ">", "<=", ">=")


# --------------------------------------------------------------------------- grammars
def _p(head: str, *bodies: str) -> List[Production]:
    out = []
    for b in bodies:
        syms = tuple(x for x in b.split() if x != EPS)
        out.append((head, syms))
    return out


def ll1_grammar() -> "Grammar":
    P: List[Production] = []
    P += _p("E", "C")
    P += _p("C", "K C'")
    P += _p("C'", *[f"{op} K C'" for op in CMP_OPS], EPS)
    P += _p("K", "A K'")
    P += _p("K'", "& A K'", EPS)
    P += _p("A", "T A'")
    P += _p("A'", "+ T A'", "- T A'", EPS)
    P += _p("T", "P T'")
    P += _p("T'", "* P T'", "/ P T'", EPS)
    P += _p("P", "U P'")
    P += _p("P'", "^ U P'", EPS)
    P += _p("U", "- U", "+ U", "F")
    P += _p("F", "X F'")
    P += _p("F'", "% F'", EPS)
    P += _p("X", "num", "str", "bool", "err", "name", "( E )", "func ( Args )", "R", "{ Arr }")
    P += _p("Args", "E ArgRest", "ArgRest")          # F(), F(a), F(,b), F(a,)
    P += _p("ArgRest", ", Arg ArgRest", EPS)
    P += _p("Arg", "E", EPS)                          # an omitted argument: IF(A1,,0)
    P += _p("R", "sheet R2", "R2")
    P += _p("R2", "cell R3", "colrange", "rowrange")
    P += _p("R3", ": R4", EPS)
    P += _p("R4", "cell", "sheet cell")               # Sheet1!A1:Sheet1!B2
    P += _p("Arr", "Const ArrRest")
    P += _p("ArrRest", ", Const ArrRest", "; Const ArrRest", EPS)
    P += _p("Const", "num", "- num", "str", "bool", "err")
    return Grammar(P, "E", "LL(1) form: left recursion removed, one non-terminal per precedence level")


def lr_grammar() -> "Grammar":
    P: List[Production] = []
    P += _p("E", *[f"E {op} K" for op in CMP_OPS], "K")
    P += _p("K", "K & A", "A")
    P += _p("A", "A + T", "A - T", "T")
    P += _p("T", "T * P", "T / P", "P")
    P += _p("P", "P ^ U", "U")
    P += _p("U", "- U", "+ U", "F")
    P += _p("F", "F %", "X")
    P += _p("X", "num", "str", "bool", "err", "name", "( E )", "func ( L )", "R", "{ Arr }")
    P += _p("L", "L , M", "M")                        # F() is one omitted argument M -> ε
    P += _p("M", "E", EPS)
    P += _p("R", "sheet R2", "R2")
    P += _p("R2", "cell", "cell : cell", "cell : sheet cell", "colrange", "rowrange")
    P += _p("Arr", "Arr , Const", "Arr ; Const", "Const")
    P += _p("Const", "num", "- num", "str", "bool", "err")
    return Grammar(P, "E", "LR form: natural left-recursive (left-associative) expression grammar")


class Grammar:
    def __init__(self, productions: List[Production], start: str, note: str = ""):
        self.P = productions
        self.start = start
        self.note = note
        self.nonterminals: List[str] = []
        for h, _ in productions:
            if h not in self.nonterminals:
                self.nonterminals.append(h)
        ts = []
        for _, body in productions:
            for s in body:
                if s not in self.nonterminals and s not in ts:
                    ts.append(s)
        self.terminals: List[str] = ts
        self._first: Optional[Dict[str, Set[str]]] = None
        self._follow: Optional[Dict[str, Set[str]]] = None

    def is_nt(self, s: str) -> bool:
        return s in self.nonterminals

    def bnf(self) -> List[str]:
        lines = []
        for nt in self.nonterminals:
            alts = [" ".join(b) if b else EPS for h, b in self.P if h == nt]
            lines.append(f"{nt:<8} → " + " | ".join(alts))
        return lines

    # ------------------------------------------------------------------ FIRST / FOLLOW
    def first_sets(self) -> Dict[str, Set[str]]:
        if self._first is not None:
            return self._first
        first: Dict[str, Set[str]] = {nt: set() for nt in self.nonterminals}
        changed = True
        while changed:                           # fixed-point iteration
            changed = False
            for head, body in self.P:
                f = self.first_of(body, first)
                if not f <= first[head]:
                    first[head] |= f
                    changed = True
        self._first = first
        return first

    def first_of(self, seq, first=None) -> Set[str]:
        first = first if first is not None else self.first_sets()
        out: Set[str] = set()
        for s in seq:
            if not self.is_nt(s):
                out.add(s)
                return out
            out |= first[s] - {EPS}
            if EPS not in first[s]:
                return out
        out.add(EPS)
        return out

    def follow_sets(self) -> Dict[str, Set[str]]:
        if self._follow is not None:
            return self._follow
        first = self.first_sets()
        follow: Dict[str, Set[str]] = {nt: set() for nt in self.nonterminals}
        follow[self.start].add(END)
        changed = True
        while changed:
            changed = False
            for head, body in self.P:
                for i, s in enumerate(body):
                    if not self.is_nt(s):
                        continue
                    rest = self.first_of(body[i + 1:], first)
                    add = (rest - {EPS}) | (follow[head] if EPS in rest else set())
                    if not add <= follow[s]:
                        follow[s] |= add
                        changed = True
        self._follow = follow
        return follow

    # ------------------------------------------------------------------ LL(1)
    def ll1_table(self):
        """M[A][a] = production index. Returns (table, conflicts)."""
        first, follow = self.first_sets(), self.follow_sets()
        table: Dict[str, Dict[str, int]] = {nt: {} for nt in self.nonterminals}
        conflicts = []
        for i, (head, body) in enumerate(self.P):
            f = self.first_of(body, first)
            targets = (f - {EPS}) | (follow[head] if EPS in f else set())
            for a in targets:
                if a in table[head] and table[head][a] != i:
                    conflicts.append((head, a, table[head][a], i))
                table[head][a] = i
        return table, conflicts

    def prod_str(self, i: int) -> str:
        h, b = self.P[i]
        return f"{h} → {' '.join(b) if b else EPS}"


# --------------------------------------------------------------------------- tokens -> terminals
_TERM = {T.NUMBER: "num", T.STRING: "str", T.BOOL: "bool", T.ERROR: "err", T.CELL: "cell",
         T.COL_RANGE: "colrange", T.ROW_RANGE: "rowrange", T.SHEET: "sheet", T.FUNC: "func",
         T.NAME: "name", T.LPAREN: "(", T.RPAREN: ")", T.COMMA: ",", T.SEMI: ";", T.COLON: ":",
         T.LBRACE: "{", T.RBRACE: "}", T.EOF: END}


def terminal_of(tok: Token) -> str:
    if tok.type is T.OP:
        return tok.value
    return _TERM[tok.type]


def terminals(formula: str) -> List[Tuple[str, str]]:
    """[(terminal, lexeme)] for a formula, ending with ('$', '')."""
    return [(terminal_of(t), t.value) for t in tokenize(formula)]


# --------------------------------------------------------------------------- LL(1) driver
@dataclass
class TraceRow:
    step: int
    stack: str
    input: str
    action: str

    def to_dict(self):
        return self.__dict__


def ll1_parse(formula: str, g: Optional[Grammar] = None, max_steps: int = 2000) -> Dict:
    """Table-driven predictive parse. Returns {'accepted', 'trace', 'derivation', 'error'}."""
    g = g or LL1
    table, _ = g.ll1_table()
    try:
        toks = terminals(formula)
    except SheetGuardError as e:
        return {"accepted": False, "trace": [], "error": f"lexical error: {e.message}"}
    stack = [END, g.start]
    i = 0
    trace: List[TraceRow] = []
    used: List[str] = []

    def show_input():
        return " ".join(t for t, _ in toks[i:])

    for step in range(1, max_steps):
        X, (a, lex) = stack[-1], toks[i]
        st = " ".join(stack)
        if X == END and a == END:
            trace.append(TraceRow(step, st, show_input(), "accept"))
            return {"accepted": True, "trace": [r.to_dict() for r in trace], "productions": used}
        if not g.is_nt(X):
            if X == a:
                trace.append(TraceRow(step, st, show_input(), f"match {a}" +
                                      (f" ({lex})" if lex and lex != a else "")))
                stack.pop()
                i += 1
                continue
            trace.append(TraceRow(step, st, show_input(), f"error: expected {X}, found {a}"))
            return {"accepted": False, "trace": [r.to_dict() for r in trace],
                    "error": f"expected '{X}' but found '{a}'"}
        p = table[X].get(a)
        if p is None:
            expected = sorted(table[X])
            trace.append(TraceRow(step, st, show_input(), f"error: M[{X}, {a}] is empty"))
            return {"accepted": False, "trace": [r.to_dict() for r in trace],
                    "error": f"no rule for {X} on '{a}' (expected one of {', '.join(expected)})"}
        trace.append(TraceRow(step, st, show_input(), g.prod_str(p)))
        used.append(g.prod_str(p))
        stack.pop()
        for s in reversed(g.P[p][1]):
            stack.append(s)
    return {"accepted": False, "trace": [r.to_dict() for r in trace], "error": "step limit"}


# --------------------------------------------------------------------------- SLR(1)
Item = Tuple[int, int]            # (production index in augmented grammar, dot position)


class SLR:
    """SLR(1) parser generator for a Grammar (builds the augmented grammar S' → S)."""

    def __init__(self, g: Grammar):
        self.g = Grammar([(g.start + "'", (g.start,))] + g.P, g.start + "'", g.note)
        self.base = g
        self.states: List[FrozenSet[Item]] = []
        self.transitions: Dict[Tuple[int, str], int] = {}
        self.action: Dict[int, Dict[str, Tuple[str, int]]] = {}
        self.goto: Dict[int, Dict[str, int]] = {}
        self.conflicts: List[Tuple[int, str, str, str]] = []
        self._build()

    def closure(self, items) -> FrozenSet[Item]:
        out = set(items)
        work = list(items)
        while work:
            p, d = work.pop()
            body = self.g.P[p][1]
            if d < len(body) and self.g.is_nt(body[d]):
                for q, (h, _) in enumerate(self.g.P):
                    if h == body[d] and (q, 0) not in out:
                        out.add((q, 0))
                        work.append((q, 0))
        return frozenset(out)

    def goto_set(self, items, X) -> FrozenSet[Item]:
        moved = [(p, d + 1) for p, d in items
                 if d < len(self.g.P[p][1]) and self.g.P[p][1][d] == X]
        return self.closure(moved) if moved else frozenset()

    def _build(self):
        start = self.closure([(0, 0)])
        self.states = [start]
        index = {start: 0}
        symbols = self.g.nonterminals + self.g.terminals
        k = 0
        while k < len(self.states):
            I = self.states[k]
            for X in symbols:
                J = self.goto_set(I, X)
                if not J:
                    continue
                if J not in index:
                    index[J] = len(self.states)
                    self.states.append(J)
                self.transitions[(k, X)] = index[J]
            k += 1
        follow = self.g.follow_sets()
        for s, I in enumerate(self.states):
            act: Dict[str, Tuple[str, int]] = {}
            self.goto[s] = {}

            def put(a, entry):
                if a in act and act[a] != entry:
                    self.conflicts.append((s, a, self._fmt(act[a]), self._fmt(entry)))
                    return
                act[a] = entry

            for p, d in sorted(I):
                head, body = self.g.P[p]
                if d < len(body):
                    a = body[d]
                    if not self.g.is_nt(a):
                        put(a, ("s", self.transitions[(s, a)]))
                elif p == 0:
                    put(END, ("acc", 0))
                else:
                    for a in follow[head]:
                        put(a, ("r", p))
            for nt in self.g.nonterminals:
                if (s, nt) in self.transitions:
                    self.goto[s][nt] = self.transitions[(s, nt)]
            self.action[s] = act

    @staticmethod
    def _fmt(e) -> str:
        return {"s": f"s{e[1]}", "r": f"r{e[1]}", "acc": "acc"}[e[0]]

    def item_str(self, item: Item) -> str:
        p, d = item
        h, b = self.g.P[p]
        syms = list(b)
        syms.insert(d, "•")
        return f"{h} → {' '.join(syms)}"

    def parse(self, formula: str, max_steps: int = 4000) -> Dict:
        try:
            toks = terminals(formula)
        except SheetGuardError as e:
            return {"accepted": False, "trace": [], "error": f"lexical error: {e.message}"}
        stack: List = [0]
        i = 0
        trace = []
        reductions = []
        for step in range(1, max_steps):
            s = stack[-1]
            a, lex = toks[i]
            st = " ".join(str(x) for x in stack)
            inp = " ".join(t for t, _ in toks[i:])
            entry = self.action[s].get(a)
            if entry is None:
                exp = sorted(self.action[s])
                trace.append({"step": step, "stack": st, "input": inp,
                              "action": f"error: ACTION[{s}, {a}] is empty"})
                return {"accepted": False, "trace": trace,
                        "error": f"unexpected '{a}' (expected one of {', '.join(exp)})"}
            kind, n = entry
            if kind == "s":
                trace.append({"step": step, "stack": st, "input": inp,
                              "action": f"shift {a}{' (' + lex + ')' if lex and lex != a else ''}"
                                        f" → state {n}"})
                stack += [a, n]
                i += 1
            elif kind == "r":
                head, body = self.g.P[n]
                if body:
                    del stack[-2 * len(body):]
                t = stack[-1]
                nxt = self.goto[t][head]
                prod = f"{head} → {' '.join(body) if body else EPS}"
                reductions.append(prod)
                trace.append({"step": step, "stack": st, "input": inp,
                              "action": f"reduce by {prod}; goto[{t}, {head}] = {nxt}"})
                stack += [head, nxt]
            else:
                trace.append({"step": step, "stack": st, "input": inp, "action": "accept"})
                return {"accepted": True, "trace": trace, "reductions": reductions}
        return {"accepted": False, "trace": trace, "error": "step limit"}

    def tables(self) -> Dict:
        terms = self.g.terminals + [END]
        nts = [nt for nt in self.g.nonterminals if nt != self.g.start]
        rows = []
        for s in range(len(self.states)):
            rows.append({"state": s,
                         "action": {a: self._fmt(self.action[s][a]) for a in terms
                                    if a in self.action[s]},
                         "goto": {A: self.goto[s][A] for A in nts if A in self.goto[s]},
                         "items": [self.item_str(it) for it in sorted(self.states[s])]})
        return {"terminals": terms, "nonterminals": nts, "rows": rows,
                "productions": [f"r{i}: {self.g.prod_str(i)}" for i in range(len(self.g.P))],
                "conflicts": [{"state": s, "symbol": a, "existing": x, "new": y}
                              for s, a, x, y in self.conflicts]}


# --------------------------------------------------------------------------- report
def _set_list(s):
    order = {EPS: 1, END: 2}
    return sorted(s, key=lambda x: (order.get(x, 0), x))


def grammar_report() -> Dict:
    g = LL1
    first, follow = g.first_sets(), g.follow_sets()
    table, conflicts = g.ll1_table()
    terms = g.terminals + [END]
    slr = SLR_PARSER
    st = slr.tables()
    return {
        "ll1": {
            "note": g.note,
            "bnf": g.bnf(),
            "productions": [g.prod_str(i) for i in range(len(g.P))],
            "nonterminals": g.nonterminals, "terminals": terms,
            "first": {nt: _set_list(first[nt]) for nt in g.nonterminals},
            "follow": {nt: _set_list(follow[nt]) for nt in g.nonterminals},
            "table": {nt: {a: table[nt][a] for a in terms if a in table[nt]}
                      for nt in g.nonterminals},
            "conflicts": [{"nonterminal": A, "terminal": a, "p1": g.prod_str(p),
                           "p2": g.prod_str(q)} for A, a, p, q in conflicts],
            "is_ll1": not conflicts,
        },
        "slr": {
            "note": slr.base.note,
            "bnf": slr.base.bnf(),
            "states": len(slr.states),
            "is_slr1": not slr.conflicts,
            **st,
        },
    }


LL1 = ll1_grammar()
SLR_PARSER = SLR(lr_grammar())
