"""Token definitions - the alphabet of the spreadsheet formula language."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class T(str, Enum):
    """Token classes recognised by the lexer."""

    NUMBER = "NUMBER"          # 42  3.14  1e-3  .5
    STRING = "STRING"          # "hello"  (with "" as escaped quote)
    BOOL = "BOOL"              # TRUE  FALSE
    ERROR = "ERROR"            # #DIV/0!  #REF!  #N/A ...
    CELL = "CELL"              # A1  $B$7
    COL_RANGE = "COL_RANGE"    # A:C   (whole columns)
    ROW_RANGE = "ROW_RANGE"    # 2:5   (whole rows)
    SHEET = "SHEET"            # Sales!   'Q1 Data'!
    FUNC = "FUNC"              # SUM(   - identifier immediately followed by '('
    NAME = "NAME"              # TaxRate  (named range / defined name)
    OP = "OP"                  # + - * / ^ & = <> < > <= >= %
    LPAREN = "LPAREN"
    RPAREN = "RPAREN"
    COMMA = "COMMA"
    SEMI = "SEMI"
    COLON = "COLON"
    LBRACE = "LBRACE"
    RBRACE = "RBRACE"
    EOF = "EOF"


@dataclass(frozen=True)
class Token:
    type: T
    value: str
    pos: int      # start offset in the original formula text
    end: int      # one-past-the-end offset

    def to_dict(self) -> dict:
        return {"type": self.type.value, "value": self.value, "pos": self.pos, "end": self.end}

    def __repr__(self) -> str:
        return f"{self.type.value}({self.value!r})@{self.pos}"


ERROR_LITERALS = (
    "#GETTING_DATA", "#DIV/0!", "#VALUE!", "#SPILL!", "#ERROR!", "#CALC!",
    "#NAME?", "#NULL!", "#NUM!", "#REF!", "#N/A",
)

TWO_CHAR_OPS = ("<=", ">=", "<>")
ONE_CHAR_OPS = "+-*/^&=<>%"
