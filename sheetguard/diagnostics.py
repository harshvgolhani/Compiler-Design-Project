"""Uniform diagnostics emitted by every compiler phase.

A diagnostic is what the Verification Report shows: where, what, why, and
how to fix it. Codes are grouped by phase, like compiler warning numbers:

    SG0xx  lexical / syntax
    SG1xx  name resolution (symbol table)
    SG2xx  dependency graph
    SG3xx  type & dimension checking
    SG4xx  smells / likely bugs
    SG5xx  optimizer notes
    SG6xx  dynamic analysis (fuzz testing)
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import List, Optional

SEVERITY_ORDER = {"error": 0, "warning": 1, "info": 2}

CODES = {
    "SG001": "Lexical error",
    "SG002": "Syntax error",
    "SG101": "Unknown function",
    "SG102": "Wrong number of arguments",
    "SG103": "Undefined name",
    "SG104": "Reference to a missing sheet",
    "SG105": "Reference to an empty cell",
    "SG106": "Function not evaluated",
    "SG201": "Circular reference",
    "SG301": "Type mismatch",
    "SG302": "Unit mismatch",
    "SG303": "Dimension mismatch",
    "SG304": "Division by zero",
    "SG305": "Implicit text-to-number coercion",
    "SG401": "Inconsistent formula",
    "SG402": "Off-by-one range",
    "SG403": "Hard-coded constant",
    "SG404": "Formula overwritten by a constant",
    "SG405": "Double counting",
    "SG406": "Absolute/relative reference mix-up",
    "SG501": "Constant expression",
    "SG502": "Dead cell",
    "SG601": "Latent runtime error",
}


@dataclass
class Diagnostic:
    code: str
    severity: str                 # error | warning | info
    cell: Optional[str]           # qualified key, e.g. "Sales!B7" (None = workbook-wide)
    message: str                  # one-line summary
    explanation: str = ""         # plain-English "why this is a problem"
    fix: str = ""                 # plain-English "how to fix"
    suggested_formula: Optional[str] = None   # machine-applicable fix, if any
    related: List[str] = field(default_factory=list)   # other cells involved
    phase: str = ""

    @property
    def title(self) -> str:
        return CODES.get(self.code, self.code)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["title"] = self.title
        return d

    def __str__(self) -> str:
        loc = self.cell or "<workbook>"
        return f"{loc}: {self.severity} {self.code} [{self.title}] {self.message}"


def sort_diagnostics(diags: List[Diagnostic]) -> List[Diagnostic]:
    from .refs import split_key

    def key(d: Diagnostic):
        if d.cell:
            s, c, r = split_key(d.cell)
            loc = (s, r, c)
        else:
            loc = ("", 0, 0)
        return (SEVERITY_ORDER.get(d.severity, 9), loc, d.code)

    return sorted(diags, key=key)
