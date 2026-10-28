"""Compiler diagnostics: every phase reports errors through these classes.

Each error remembers the exact source position so we can print a caret
under the offending character, just like gcc / clang / javac do.
"""
from __future__ import annotations


class SheetGuardError(Exception):
    """Base class for every compile-time error SheetGuard can raise."""

    phase = "compiler"

    def __init__(self, message: str, pos: int = 0, source: str = "", end: int | None = None):
        super().__init__(message)
        self.message = message
        self.pos = pos
        self.end = end if end is not None else pos + 1
        self.source = source

    def pretty(self) -> str:
        """Return a gcc-style two-line message with a caret marker."""
        if not self.source:
            return f"{self.phase} error: {self.message}"
        width = max(1, min(self.end, len(self.source) + 1) - self.pos)
        caret = " " * self.pos + "^" + "~" * (width - 1)
        return f"{self.source}\n{caret}\n{self.phase} error (col {self.pos + 1}): {self.message}"

    def to_dict(self) -> dict:
        return {
            "phase": self.phase,
            "message": self.message,
            "pos": self.pos,
            "end": self.end,
            "pretty": self.pretty(),
        }

    def __str__(self) -> str:  # pragma: no cover - convenience
        return f"{self.phase} error at col {self.pos + 1}: {self.message}"


class LexError(SheetGuardError):
    """Raised by the lexer when it meets a character sequence it cannot tokenize."""

    phase = "lexical"


class ParseError(SheetGuardError):
    """Raised by the parser when the token stream violates the grammar."""

    phase = "syntax"

    def __init__(self, message: str, pos: int = 0, source: str = "", end: int | None = None,
                 expected: list[str] | None = None):
        super().__init__(message, pos, source, end)
        self.expected = expected or []

    def to_dict(self) -> dict:
        d = super().to_dict()
        d["expected"] = self.expected
        return d
