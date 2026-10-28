"""Phase 2 - Syntax analysis: a hand-written recursive-descent parser.

Grammar (EBNF) - one function per non-terminal, lowest precedence first:

    formula     ::= '=' expr EOF
    expr        ::= comparison
    comparison  ::= concat   ( ('=' | '<>' | '<' | '>' | '<=' | '>=') concat )*
    concat      ::= additive ( '&' additive )*
    additive    ::= term     ( ('+' | '-') term )*
    term        ::= power    ( ('*' | '/') power )*
    power       ::= unary    ( '^' unary )*            (left-assoc, like Excel)
    unary       ::= ('-' | '+') unary | postfix
    postfix     ::= primary '%'*
    primary     ::= NUMBER | STRING | BOOL | ERROR
                  | reference
                  | FUNC '(' [ args ] ')'
                  | NAME
                  | '(' expr ')'
                  | '{' array '}'
    reference   ::= [SHEET] CELL [ ':' [SHEET] CELL ]
                  | [SHEET] COL_RANGE | [SHEET] ROW_RANGE
    args        ::= arg ( (',' | ';') arg )*            arg may be empty
    array       ::= row ( ';' row )*      row ::= const ( ',' const )*

Note the spreadsheet quirk faithfully reproduced here: unary minus binds
tighter than '^', so  =-2^2  evaluates to 4 (not -4 as in mathematics).
"""
from __future__ import annotations

from .ast_nodes import (ArrayLit, BinOp, Bool, CellRef, ErrorLit, FuncCall, Missing,
                        NameRef, Node, Number, Percent, RangeRef, String, UnaryOp)
from .errors import ParseError
from .lexer import Lexer
from .refs import parse_cell, parse_col
from .tokens import T, Token

COMPARISON_OPS = ("=", "<>", "<", ">", "<=", ">=")

_DESCRIBE = {
    T.EOF: "end of formula", T.RPAREN: "')'", T.LPAREN: "'('", T.COMMA: "','",
    T.SEMI: "';'", T.COLON: "':'", T.RBRACE: "'}'", T.LBRACE: "'{'",
}


def _describe(tok: Token) -> str:
    if tok.type in _DESCRIBE:
        return _DESCRIBE[tok.type]
    return f"{tok.type.value.lower()} {tok.value!r}"


MAX_NESTING = 100            # brackets + function calls; Excel's own limit is 64


class Parser:
    def __init__(self, source: str, tokens: list[Token] | None = None):
        self.src = source
        self.toks = tokens if tokens is not None else Lexer(source).tokenize()
        self.i = 0
        self.depth = self.max_depth = 0

    # -- token-stream helpers -------------------------------------------
    @property
    def cur(self) -> Token:
        return self.toks[self.i]

    def peek(self, k: int = 1) -> Token:
        j = min(self.i + k, len(self.toks) - 1)
        return self.toks[j]

    def advance(self) -> Token:
        tok = self.toks[self.i]
        if tok.type is not T.EOF:
            self.i += 1
        return tok

    def at_op(self, *ops: str) -> bool:
        return self.cur.type is T.OP and self.cur.value in ops

    def expect(self, type_: T, what: str) -> Token:
        if self.cur.type is not type_:
            raise self.error(f"expected {what}, found {_describe(self.cur)}", [what])
        return self.advance()

    def error(self, msg: str, expected=None, tok: Token | None = None) -> ParseError:
        tok = tok or self.cur
        end = tok.end if tok.end > tok.pos else tok.pos + 1
        return ParseError(msg, tok.pos, self.src, end, expected)

    # -- entry point -----------------------------------------------------
    def parse(self) -> Node:
        if self.cur.type is T.EOF:
            raise self.error("empty formula - expected an expression after '='", ["expression"])
        try:
            node = self.expr()
        except RecursionError:
            raise self.error(f"formula is nested too deeply for this Python stack (about "
                             f"{self.max_depth - 1} levels)") from None
        if self.cur.type is not T.EOF:
            if self.cur.type is T.RPAREN:
                raise self.error("unbalanced ')' - no matching '('")
            raise self.error(f"unexpected {_describe(self.cur)} after complete expression - "
                             f"missing operator?", ["operator", "end of formula"])
        return node

    # -- precedence climbing, one level per grammar rule -----------------
    def expr(self) -> Node:
        self.depth += 1
        self.max_depth = max(self.max_depth, self.depth)
        if self.depth > MAX_NESTING + 1:          # the outermost expression is level 0
            raise self.error(f"formula is nested too deeply (more than {MAX_NESTING} levels of "
                             f"brackets / function calls; Excel allows 64)")
        try:
            return self.comparison()
        finally:
            self.depth -= 1

    def _binary_level(self, ops, next_level) -> Node:
        left = next_level()
        while self.at_op(*ops):
            op = self.advance().value
            right = next_level()
            left = BinOp(op, left, right).at(left.span[0], right.span[1])
        return left

    def comparison(self) -> Node:
        return self._binary_level(COMPARISON_OPS, self.concat)

    def concat(self) -> Node:
        return self._binary_level(("&",), self.additive)

    def additive(self) -> Node:
        return self._binary_level(("+", "-"), self.term)

    def term(self) -> Node:
        return self._binary_level(("*", "/"), self.power)

    def power(self) -> Node:
        return self._binary_level(("^",), self.unary)

    def unary(self) -> Node:
        # unary  ->  ('-' | '+') unary | postfix     (iterative: --------1 needs no recursion)
        signs = []
        while self.at_op("-", "+"):
            signs.append(self.advance())
        node = self.postfix()
        for tok in reversed(signs):
            node = UnaryOp(tok.value, node).at(tok.pos, node.span[1])
        return node

    def postfix(self) -> Node:
        node = self.primary()
        while self.at_op("%"):
            tok = self.advance()
            node = Percent(node).at(node.span[0], tok.end)
        return node

    # -- primary expressions ---------------------------------------------
    def primary(self) -> Node:
        tok = self.cur
        t = tok.type

        if t is T.NUMBER:
            self.advance()
            return Number(float(tok.value), tok.value).at(tok.pos, tok.end)
        if t is T.STRING:
            self.advance()
            return String(tok.value).at(tok.pos, tok.end)
        if t is T.BOOL:
            self.advance()
            return Bool(tok.value == "TRUE").at(tok.pos, tok.end)
        if t is T.ERROR:
            self.advance()
            return ErrorLit(tok.value).at(tok.pos, tok.end)
        if t in (T.SHEET, T.CELL, T.COL_RANGE, T.ROW_RANGE):
            return self.reference()
        if t is T.FUNC:
            return self.call()
        if t is T.NAME:
            self.advance()
            return NameRef(tok.value).at(tok.pos, tok.end)
        if t is T.LPAREN:
            self.advance()
            inner = self.expr()
            close = self.expect(T.RPAREN, "')' to close '('")
            inner.span = (tok.pos, close.end)
            return inner
        if t is T.LBRACE:
            return self.array()
        if t is T.EOF:
            raise self.error("formula ends unexpectedly - expected an operand", ["operand"])
        if t is T.RPAREN:
            raise self.error("expected an operand before ')'", ["operand"])
        if t is T.OP:
            raise self.error(f"operator {tok.value!r} is missing its left operand", ["operand"])
        raise self.error(f"unexpected {_describe(tok)}", ["operand"])

    def reference(self) -> Node:
        start = self.cur.pos
        sheet = None
        if self.cur.type is T.SHEET:
            sheet = self.advance().value
            if self.cur.type not in (T.CELL, T.COL_RANGE, T.ROW_RANGE):
                raise self.error(f"expected a cell or range after sheet '{sheet}!'",
                                 ["cell reference"])
        tok = self.advance()

        if tok.type is T.COL_RANGE:
            a, b = tok.value.split(":")
            (c1, ac1), (c2, ac2) = parse_col(a), parse_col(b)
            return RangeRef(c1, None, c2, None, ac1, False, ac2, False, sheet).at(start, tok.end)

        if tok.type is T.ROW_RANGE:
            a, b = tok.value.split(":")
            r1, r2 = int(a.lstrip("$")), int(b.lstrip("$"))
            return RangeRef(None, r1, None, r2, False, a.startswith("$"), False,
                            b.startswith("$"), sheet).at(start, tok.end)

        col, row, ac, ar = parse_cell(tok.value)
        if self.cur.type is T.COLON:
            colon = self.advance()
            if self.cur.type is T.SHEET:          # Sheet1!A1:Sheet1!B5
                sheet2 = self.advance().value
                if sheet and sheet2 != sheet:
                    raise self.error("a range cannot span two different sheets",
                                     tok=self.toks[self.i - 1])
            if self.cur.type is not T.CELL:
                raise self.error("expected a cell reference after ':' to complete the range",
                                 ["cell reference"], tok=self.cur if self.cur.type is not T.EOF
                                 else colon)
            end_tok = self.advance()
            col2, row2, ac2, ar2 = parse_cell(end_tok.value)
            return RangeRef(col, row, col2, row2, ac, ar, ac2, ar2, sheet).at(start, end_tok.end)
        return CellRef(col, row, ac, ar, sheet).at(start, tok.end)

    def call(self) -> Node:
        name_tok = self.advance()
        self.expect(T.LPAREN, "'(' after function name")
        args: list[Node] = []
        if self.cur.type is T.RPAREN:
            close = self.advance()
            return FuncCall(name_tok.value, args).at(name_tok.pos, close.end)
        while True:
            if self.cur.type in (T.COMMA, T.SEMI, T.RPAREN):
                args.append(Missing().at(self.cur.pos, self.cur.pos))
            else:
                args.append(self.expr())
            if self.cur.type in (T.COMMA, T.SEMI):
                self.advance()
                continue
            if self.cur.type is T.RPAREN:
                close = self.advance()
                return FuncCall(name_tok.value, args).at(name_tok.pos, close.end)
            if self.cur.type is T.EOF:
                raise self.error(f"missing ')' to close {name_tok.value}(", ["')'"],
                                 tok=self.cur)
            raise self.error(f"expected ',' or ')' in arguments of {name_tok.value}, found "
                             f"{_describe(self.cur)}", ["','", "')'"])

    def array(self) -> Node:
        open_tok = self.advance()
        rows = [[]]
        while True:
            neg = False
            if self.at_op("-"):
                self.advance()
                neg = True
            tok = self.cur
            if tok.type is T.NUMBER:
                self.advance()
                v = float(tok.value)
                rows[-1].append(Number(-v if neg else v, ("-" if neg else "") + tok.value)
                                .at(tok.pos, tok.end))
            elif tok.type is T.STRING and not neg:
                self.advance()
                rows[-1].append(String(tok.value).at(tok.pos, tok.end))
            elif tok.type is T.BOOL and not neg:
                self.advance()
                rows[-1].append(Bool(tok.value == "TRUE").at(tok.pos, tok.end))
            elif tok.type is T.ERROR and not neg:
                self.advance()
                rows[-1].append(ErrorLit(tok.value).at(tok.pos, tok.end))
            else:
                raise self.error("array constants may only contain numbers, text, "
                                 "booleans or errors", ["constant"])
            if self.cur.type is T.COMMA:
                self.advance()
            elif self.cur.type is T.SEMI:
                self.advance()
                rows.append([])
            elif self.cur.type is T.RBRACE:
                close = self.advance()
                break
            else:
                raise self.error("expected ',', ';' or '}' in array constant",
                                 ["','", "';'", "'}'"])
        widths = {len(r) for r in rows}
        if len(widths) != 1:
            raise self.error("array constant rows have different lengths", tok=open_tok)
        return ArrayLit(rows).at(open_tok.pos, close.end)


def parse(formula: str) -> Node:
    """Parse a formula string (with or without the leading '=') into an AST."""
    return Parser(formula).parse()


def is_formula(text) -> bool:
    return isinstance(text, str) and text.startswith("=") and len(text) > 1


def repair(formula: str, error=None):
    """Phrase-level error recovery: try the smallest edits around the error position
    (insert / delete one symbol) and return the first repaired formula that parses.

        =SUM(A1:A3          ->  =SUM(A1:A3)      (insert ')')
        =A1+*2              ->  =A1+2            (delete '*')
        =(A1+2))            ->  =(A1+2)          (delete ')')
        ="abc               ->  ="abc"           (close string)
        =COUNTIF(A:A,"S)    ->  =COUNTIF(A:A,"S")  (close string before the ')')
    Returns None when no single local edit produces a valid formula.
    """
    from .errors import SheetGuardError
    if not formula.startswith("="):
        formula = "=" + formula
    pos = getattr(error, "pos", len(formula))
    end = getattr(error, "end", pos + 1)
    depth = formula.count("(") - formula.count(")")
    cands = []
    if depth > 0:
        cands.append(formula + ")" * depth)
    if formula.count('"') % 2 == 1:
        # unterminated string: the quote was most likely forgotten *before* the closing
        # parentheses, e.g. =COUNTIF(J2:J11,"S)  ->  =COUNTIF(J2:J11,"S")
        tail = len(formula) - len(formula.rstrip(") "))
        if tail:
            cands.append(formula[:len(formula) - tail] + '"' + formula[len(formula) - tail:])
    cands += [formula + '"', formula + '")']
    cands.append(formula[:pos] + formula[end:])                 # delete offending token
    cands.append(formula[:pos] + formula[pos + 1:])             # delete one character
    cands.append(formula[:pos] + ")" + formula[pos:])           # insert ')'
    cands.append(formula.rstrip("+-*/^&=<>,"))                  # drop dangling operator
    for c in cands:
        if c == formula or len(c) < 2:
            continue
        try:
            Parser(c).parse()
            return c
        except SheetGuardError:
            continue
    return None
