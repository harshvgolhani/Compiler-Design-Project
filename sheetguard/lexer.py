"""Phase 1 - Lexical analysis.

A hand-written scanner (a DFA expressed as code) that turns a formula string
such as ``=SUM(Sales!B2:B9)*(1+TaxRate)`` into a token stream:

    FUNC('SUM') LPAREN SHEET('Sales') CELL('B2') COLON CELL('B9') RPAREN
    OP('*') LPAREN NUMBER('1') OP('+') NAME('TaxRate') RPAREN EOF

Classifying identifiers is the interesting part, because the same characters
can mean different things depending on what follows them (one-character
lookahead, exactly like a real lexer):

    LOG10(   -> FUNC   (followed by '(')
    LOG10    -> CELL   (column LOG, row 10 - valid in Excel!)
    Sales!   -> SHEET  (followed by '!')
    A:C      -> COL_RANGE
    TRUE     -> BOOL
    TaxRate  -> NAME   (a defined name, resolved later by the symbol table)
"""
from __future__ import annotations

import re

from .errors import LexError
from .refs import parse_cell
from .tokens import ERROR_LITERALS, ONE_CHAR_OPS, TWO_CHAR_OPS, T, Token

_IDENT_START = re.compile(r"[A-Za-z_$\\]")
_IDENT_CHAR = re.compile(r"[A-Za-z0-9_.$\\]")
_COL_RANGE = re.compile(r"\$?[A-Za-z]{1,3}:\$?[A-Za-z]{1,3}(?![A-Za-z0-9_(])")
_ROW_RANGE = re.compile(r"\$?[0-9]+:\$?[0-9]+(?![0-9.A-Za-z])")


class Lexer:
    def __init__(self, source: str):
        self.src = source
        self.i = 1 if source.startswith("=") else 0
        self.tokens: list[Token] = []

    # -- helpers ---------------------------------------------------------
    def peek(self, k: int = 0) -> str:
        j = self.i + k
        return self.src[j] if j < len(self.src) else ""

    def emit(self, type_: T, value: str, start: int) -> None:
        self.tokens.append(Token(type_, value, start, self.i))

    def error(self, msg: str, start: int, end: int | None = None) -> LexError:
        return LexError(msg, start, self.src, end)

    # -- main loop -------------------------------------------------------
    def tokenize(self) -> list[Token]:
        src = self.src
        while self.i < len(src):
            c = src[self.i]
            start = self.i

            if c in " \t\r\n":
                self.i += 1
                continue

            # whole-row range  2:5  (must be checked before plain numbers)
            m = _ROW_RANGE.match(src, self.i)
            if m and self._prev_allows_operand():
                self.i = m.end()
                self.emit(T.ROW_RANGE, m.group(0), start)
                continue

            if c.isdigit() or (c == "." and self.peek(1).isdigit()):
                self._number()
                continue

            if c == '"':
                self._string()
                continue

            if c == "#":
                self._error_literal()
                continue

            if c == "'":
                self._quoted_sheet()
                continue

            # whole-column range  A:C
            m = _COL_RANGE.match(src, self.i)
            if m and self._prev_allows_operand():
                self.i = m.end()
                self.emit(T.COL_RANGE, m.group(0).upper(), start)
                continue

            if _IDENT_START.match(c):
                self._identifier()
                continue

            two = src[self.i:self.i + 2]
            if two in TWO_CHAR_OPS:
                self.i += 2
                self.emit(T.OP, two, start)
                continue
            if c in ONE_CHAR_OPS:
                self.i += 1
                self.emit(T.OP, c, start)
                continue

            simple = {"(": T.LPAREN, ")": T.RPAREN, ",": T.COMMA, ";": T.SEMI,
                      ":": T.COLON, "{": T.LBRACE, "}": T.RBRACE}
            if c in simple:
                self.i += 1
                self.emit(simple[c], c, start)
                continue

            raise self.error(f"unexpected character {c!r}", start)

        self.tokens.append(Token(T.EOF, "", len(src), len(src)))
        return self.tokens

    # -- token recognisers ----------------------------------------------
    def _prev_allows_operand(self) -> bool:
        """A range like 2:5 or A:C may only start where an operand is expected."""
        if not self.tokens:
            return True
        prev = self.tokens[-1].type
        return prev in (T.OP, T.LPAREN, T.COMMA, T.SEMI, T.SHEET, T.LBRACE)

    def _number(self) -> None:
        start = self.i
        src = self.src
        while self.peek().isdigit():
            self.i += 1
        if self.peek() == ".":
            self.i += 1
            while self.peek().isdigit():
                self.i += 1
        if self.peek() in ("e", "E") and (self.peek(1).isdigit() or
                                          (self.peek(1) in "+-" and self.peek(2).isdigit())):
            self.i += 2
            while self.peek().isdigit():
                self.i += 1
        if self.peek() and _IDENT_CHAR.match(self.peek()) and self.peek() != ".":
            # e.g. "12abc" - a number glued to letters is malformed
            j = self.i
            while j < len(src) and _IDENT_CHAR.match(src[j]):
                j += 1
            raise self.error(f"malformed number {src[start:j]!r}", start, j)
        self.emit(T.NUMBER, src[start:self.i], start)

    def _string(self) -> None:
        start = self.i
        self.i += 1
        buf = []
        while True:
            if self.i >= len(self.src):
                raise self.error("unterminated string literal (missing closing \")", start,
                                 len(self.src))
            ch = self.src[self.i]
            if ch == '"':
                if self.peek(1) == '"':      # "" is an escaped quote
                    buf.append('"')
                    self.i += 2
                    continue
                self.i += 1
                break
            buf.append(ch)
            self.i += 1
        self.emit(T.STRING, "".join(buf), start)

    def _error_literal(self) -> None:
        start = self.i
        rest = self.src[self.i:].upper()
        for lit in ERROR_LITERALS:          # longest literals are listed first
            if rest.startswith(lit):
                self.i += len(lit)
                self.emit(T.ERROR, lit, start)
                return
        raise self.error("unknown error literal", start, start + 1)

    def _quoted_sheet(self) -> None:
        start = self.i
        self.i += 1
        buf = []
        while True:
            if self.i >= len(self.src):
                raise self.error("unterminated quoted sheet name", start, len(self.src))
            ch = self.src[self.i]
            if ch == "'":
                if self.peek(1) == "'":
                    buf.append("'")
                    self.i += 2
                    continue
                self.i += 1
                break
            buf.append(ch)
            self.i += 1
        if self.peek() != "!":
            raise self.error("quoted sheet name must be followed by '!'", start, self.i)
        self.i += 1
        self.emit(T.SHEET, "".join(buf), start)

    def _identifier(self) -> None:
        start = self.i
        while self.peek() and _IDENT_CHAR.match(self.peek()):
            self.i += 1
        word = self.src[start:self.i]

        # Sheet prefix: Sales!
        if self.peek() == "!":
            self.i += 1
            self.emit(T.SHEET, word, start)
            return

        # Function call: lookahead (skipping blanks) for '('
        j = self.i
        while j < len(self.src) and self.src[j] in " \t":
            j += 1
        if j < len(self.src) and self.src[j] == "(" and "$" not in word:
            name = word.upper()
            for prefix in ("_XLFN._XLWS.", "_XLFN.", "_XLWS."):
                if name.startswith(prefix):
                    name = name[len(prefix):]
            self.emit(T.FUNC, name, start)
            return

        if parse_cell(word):
            self.emit(T.CELL, word.upper(), start)
            return

        if word.upper() in ("TRUE", "FALSE"):
            self.emit(T.BOOL, word.upper(), start)
            return

        if "$" in word:
            raise self.error(f"malformed cell reference {word!r}", start, self.i)

        self.emit(T.NAME, word, start)


def tokenize(source: str) -> list[Token]:
    """Convenience wrapper: tokenize a formula string (leading '=' optional)."""
    return Lexer(source).tokenize()
