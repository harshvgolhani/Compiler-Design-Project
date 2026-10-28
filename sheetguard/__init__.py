"""SheetGuard - a compiler-based verification engine for spreadsheet formulas.

Pipeline:  lexer -> parser (AST) -> symbol table -> dependency DAG
           -> type checker -> smell detector -> optimizer -> code generator
"""
__version__ = "1.3.0"

from .lexer import tokenize          # noqa: E402,F401
from .parser import parse            # noqa: E402,F401
from .workbook import Workbook       # noqa: E402,F401
from .pipeline import analyze        # noqa: E402,F401
