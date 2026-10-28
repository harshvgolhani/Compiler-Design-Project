"""The workbook model - SheetGuard's view of the 'source program'.

A workbook is a set of sheets; a sheet is a sparse grid of cells; a cell is
either a constant (an *input*) or a formula (a *statement*). Loaders accept:

  * the JSON the Google Sheets add-on sends     Workbook.from_json(...)
  * a plain Python dict (tests, quick demos)     Workbook.from_dict(...)
  * an .xlsx file (needs openpyxl)               Workbook.from_xlsx(path)
  * a .csv file                                  Workbook.from_csv(path)
"""
from __future__ import annotations

import csv
import datetime as _dt
import json
import math
import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, List, Optional, Tuple

from .ast_nodes import Node
from .diagnostics import Diagnostic
from .errors import SheetGuardError
from .lexer import Lexer
from .parser import Parser, is_formula
from .refs import a1, make_key, parse_cell
from .tokens import ERROR_LITERALS, Token


@dataclass(eq=False)
class Cell:
    sheet: str
    col: int
    row: int
    raw: Any = None                 # what the user typed: formula text or a constant
    value: Any = None               # last computed / displayed value (if known)
    fmt: str = ""                   # number format, e.g. '$#,##0.00', '0%', 'yyyy-mm-dd'
    tokens: Optional[List[Token]] = None
    ast: Optional[Node] = None
    error: Optional[SheetGuardError] = None

    @property
    def address(self) -> str:
        return a1(self.col, self.row)

    @property
    def key(self) -> str:
        return make_key(self.sheet, self.col, self.row)

    @property
    def is_formula(self) -> bool:
        return is_formula(self.raw)

    @property
    def formula(self) -> Optional[str]:
        return self.raw if self.is_formula else None

    def compile(self) -> None:
        """Run phases 1-2 (lex + parse) on this cell's formula."""
        self.tokens = self.ast = self.error = None
        if not self.is_formula:
            return
        try:
            self.tokens = Lexer(self.raw).tokenize()
            self.ast = Parser(self.raw, self.tokens).parse()
        except SheetGuardError as e:
            self.error = e
            self.ast = None

    def to_dict(self) -> dict:
        return {"key": self.key, "address": self.address, "raw": self.raw,
                "value": _jsonable(self.value), "fmt": self.fmt}


def _jsonable(v):
    if isinstance(v, float) and (v != v or v in (float("inf"), float("-inf"))):
        return str(v)
    if hasattr(v, "isoformat"):
        return v.isoformat()
    return v


MAX_CELLS = 250_000          # beyond this a workbook is rejected with a clear message


class WorkbookLoadError(ValueError):
    """A file or payload that cannot be read as a workbook (shown to the user as-is)."""


def _plain(v: Any) -> Any:
    """Cell content from a reader -> str / float / int / bool / None."""
    if v is None or isinstance(v, (str, bool, int)):
        return v
    if isinstance(v, float):
        return v if math.isfinite(v) else str(v)
    if isinstance(v, (_dt.datetime, _dt.date)):
        from .runtime import date_to_serial
        if not isinstance(v, _dt.datetime):
            return date_to_serial(v)
        return date_to_serial(v.replace(tzinfo=None))
    if isinstance(v, _dt.time):
        return (v.hour * 3600 + v.minute * 60 + v.second) / 86400
    if isinstance(v, _dt.timedelta):
        return v.total_seconds() / 86400
    text = getattr(v, "text", None)                 # openpyxl ArrayFormula / DataTableFormula
    if isinstance(text, str):
        # a legacy Ctrl+Shift+Enter array formula: Sheets shows it as ARRAYFORMULA(...)
        return "=ARRAYFORMULA(" + text.lstrip("=") + ")"
    return str(v)


class Sheet:
    def __init__(self, name: str):
        self.name = name
        self.cells: Dict[Tuple[int, int], Cell] = {}
        self._extent: Dict[tuple, int] = {}

    def bounds(self, ref) -> Tuple[int, int, int, int]:
        """(c1, c2, r1, r2) of a resolved range. Whole columns (A:A) end at the last used row
        *of those columns*, whole rows (1:1) at the last used column of those rows, so one
        stray value at XFD1048576 does not turn every A:A into a million cells."""
        c1, c2, r1, r2 = ref.col1, ref.col2, ref.row1, ref.row2
        if r1 is None or r2 is None:
            lo, hi = (c1 or 1), (c2 or 1 << 30)
            key = ("c", lo, hi)
            if key not in self._extent:
                self._extent[key] = max((r for (c, r) in self.cells if lo <= c <= hi), default=1)
            r1, r2 = 1, self._extent[key]
        if c1 is None or c2 is None:
            lo, hi = (r1 or 1), (r2 or 1 << 30)
            key = ("r", lo, hi)
            if key not in self._extent:
                self._extent[key] = max((c for (c, r) in self.cells if lo <= r <= hi), default=1)
            c1, c2 = 1, self._extent[key]
        return c1, c2, r1, r2

    def set(self, col: int, row: int, raw: Any, value: Any = None, fmt: str = "") -> Cell:
        self._extent.clear()
        if raw is None or raw == "":
            self.cells.pop((col, row), None)
            return None
        cell = Cell(self.name, col, row, raw, value if value is not None else
                    (None if is_formula(raw) else raw), fmt or "")
        self.cells[(col, row)] = cell
        return cell

    def get(self, col: int, row: int) -> Optional[Cell]:
        return self.cells.get((col, row))

    def __getitem__(self, a1_ref: str) -> Optional[Cell]:
        col, row, _, _ = parse_cell(a1_ref)
        return self.get(col, row)

    def __iter__(self) -> Iterator[Cell]:
        return iter(sorted(self.cells.values(), key=lambda c: (c.row, c.col)))

    @property
    def max_row(self) -> int:
        return max((r for _, r in self.cells), default=0)

    @property
    def max_col(self) -> int:
        return max((c for c, _ in self.cells), default=0)

    def grid(self, what: str = "raw") -> List[List[Any]]:
        ncols = self.max_col                       # computed once: O(cells), not O(rows x cells)
        out = [[None] * ncols for _ in range(self.max_row)]
        for (c, r), cell in self.cells.items():
            out[r - 1][c - 1] = getattr(cell, what)
        return out


class Workbook:
    def __init__(self, name: str = "Workbook"):
        self.name = name
        self.sheets: Dict[str, Sheet] = {}
        # defined names: name -> (definition text, scope) ; scope None = workbook-wide
        self.names: Dict[Tuple[str, Optional[str]], str] = {}

    # -- construction ----------------------------------------------------
    def add_sheet(self, name: str) -> Sheet:
        if name not in self.sheets:
            self.sheets[name] = Sheet(name)
        return self.sheets[name]

    def define_name(self, name: str, definition: str, scope: Optional[str] = None) -> None:
        if not isinstance(name, str) or not name.strip() or not isinstance(definition, str):
            return
        self.names[(name.strip(), scope)] = definition.strip().lstrip("=")

    def cell(self, key: str) -> Optional[Cell]:
        sheet, _, ref = key.rpartition("!")
        sh = self.sheets.get(sheet)
        if sh is None:
            return None
        try:
            return sh[ref]
        except (SheetGuardError, ValueError, KeyError, TypeError):
            return None

    def cells(self) -> Iterator[Cell]:
        for sh in self.sheets.values():
            yield from sh

    def formula_cells(self) -> Iterator[Cell]:
        return (c for c in self.cells() if c.is_formula)

    # -- phases 1 & 2 over the whole workbook ------------------------------
    def compile(self) -> List[Diagnostic]:
        diags = []
        for cell in self.formula_cells():
            cell.compile()
            if cell.error is not None:
                code = "SG001" if cell.error.phase == "lexical" else "SG002"
                from .parser import repair
                fixed = repair(cell.raw, cell.error)
                diags.append(Diagnostic(
                    code, "error", cell.key, cell.error.message,
                    explanation="The formula cannot be understood by the compiler, so the "
                                "spreadsheet will show an error or a wrong value here.\n"
                                + cell.error.pretty(),
                    fix=(f"Error recovery found a minimal repair: {fixed}" if fixed else
                         "Correct the formula at the position marked by the caret (^)."),
                    suggested_formula=fixed, phase=cell.error.phase))
        return diags

    def stats(self) -> dict:
        cells = list(self.cells())
        return {
            "sheets": len(self.sheets),
            "cells": len(cells),
            "formulas": sum(1 for c in cells if c.is_formula),
            "constants": sum(1 for c in cells if not c.is_formula),
            "names": len(self.names),
        }

    # -- loaders -----------------------------------------------------------
    @classmethod
    def from_dict(cls, data: Dict[str, Dict[str, Any]], names: Dict[str, str] | None = None,
                  formats: Dict[str, str] | None = None, name: str = "Workbook") -> "Workbook":
        """{'Sheet1': {'A1': 10, 'A2': '=A1*2'}} - the quickest way to build a test workbook."""
        wb = cls(name)
        formats = formats or {}
        for sheet_name, cells in data.items():
            sh = wb.add_sheet(sheet_name)
            for ref, raw in cells.items():
                col, row, _, _ = parse_cell(ref)
                sh.set(col, row, _coerce_literal(raw), fmt=formats.get(f"{sheet_name}!{ref}", ""))
        for n, d in (names or {}).items():
            wb.define_name(n, d)
        return wb

    @classmethod
    def from_json(cls, payload: Any) -> "Workbook":
        """Load the structure produced by the Google Apps Script add-on:

        {"name": "...", "sheets": [{"name": "Sales", "formulas": [[...]],
          "values": [[...]], "formats": [[...]]}], "namedRanges": [{"name": "TaxRate",
          "range": "Inputs!B2"}]}
        """
        if isinstance(payload, (str, bytes)):
            try:
                payload = json.loads(payload)
            except ValueError as e:
                raise WorkbookLoadError(f"not valid JSON: {e}") from None
        if not isinstance(payload, dict) or not isinstance(payload.get("sheets", []), list):
            raise WorkbookLoadError('a workbook must be a JSON object with a "sheets" list '
                                    '(as written by `python -m sheetguard demo-files`)')
        wb = cls(str(payload.get("name") or "Workbook"))
        total = 0
        for i, sh_data in enumerate(payload.get("sheets", [])):
            if not isinstance(sh_data, dict):
                raise WorkbookLoadError(f"sheet #{i + 1} is not an object")
            sh = wb.add_sheet(str(sh_data.get("name") or f"Sheet{i + 1}"))
            formulas = _rows(sh_data.get("formulas"))
            values = _rows(sh_data.get("values"))
            formats = _rows(sh_data.get("formats"))
            total += sum(len(r) for r in values) + sum(len(r) for r in formulas)
            if total > 2 * MAX_CELLS * 4:
                raise WorkbookLoadError(f"workbook too large (more than {MAX_CELLS:,} cells)")
            for entry in sh_data.get("cells") or []:        # sparse form: [row, col, raw, value, fmt]
                if not isinstance(entry, list) or len(entry) < 3:
                    continue
                try:
                    r, c = int(entry[0]), int(entry[1])
                except (TypeError, ValueError):
                    continue
                if r < 1 or c < 1:
                    continue
                raw = _plain(entry[2])
                raw = raw if is_formula(raw) else _json_literal(raw)
                if raw is None or raw == "":
                    continue
                val = _plain(entry[3]) if len(entry) > 3 else None
                fmt = entry[4] if len(entry) > 4 and isinstance(entry[4], str) else ""
                sh.set(c, r, raw, value=None if val in (None, "") else _json_literal(val), fmt=fmt)
            nrows = max(len(formulas), len(values))
            for r in range(nrows):
                frow = formulas[r] if r < len(formulas) else []
                vrow = values[r] if r < len(values) else []
                mrow = formats[r] if r < len(formats) else []
                for c in range(max(len(frow), len(vrow))):
                    f = frow[c] if c < len(frow) else ""
                    v = _plain(vrow[c]) if c < len(vrow) else ""
                    fmt = mrow[c] if c < len(mrow) else ""
                    raw = f if is_formula(f) else _json_literal(v)
                    if raw is None or raw == "":
                        continue
                    sh.set(c + 1, r + 1, raw, value=None if v == "" else _json_literal(v),
                           fmt=fmt if isinstance(fmt, str) else "")
        for nr in payload.get("namedRanges") or []:
            if isinstance(nr, dict):
                wb.define_name(nr.get("name"), nr.get("range"), nr.get("scope"))
        wb.check_size()
        return wb

    @classmethod
    def from_xlsx(cls, path: str) -> "Workbook":
        try:
            import openpyxl
        except ImportError as e:  # pragma: no cover
            raise RuntimeError("Reading .xlsx needs openpyxl:  pip install openpyxl") from e
        import warnings
        import zipfile
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")            # openpyxl's "unknown extension" noise
                wb_f = openpyxl.load_workbook(path, data_only=False)
                wb_v = openpyxl.load_workbook(path, data_only=True, read_only=True)
        except (zipfile.BadZipFile, KeyError, OSError) as e:
            if isinstance(e, FileNotFoundError):
                raise
            raise WorkbookLoadError(f"{os.path.basename(path)} is not a valid .xlsx file "
                                    f"({type(e).__name__}). If it is an old .xls file, open it "
                                    f"and save it as .xlsx first.") from None
        wb = cls(os.path.splitext(os.path.basename(path))[0])
        try:
            for ws in wb_f.worksheets:
                sh = wb.add_sheet(ws.title)
                cached = {}
                try:
                    for row in wb_v[ws.title].iter_rows():
                        for c in row:
                            if getattr(c, "value", None) is not None:
                                cached[(c.column, c.row)] = c.value
                except Exception:                  # cached values are optional
                    cached = {}
                for row in ws.iter_rows():
                    for c in row:
                        raw = _plain(c.value)
                        if raw is None or raw == "":
                            continue
                        if isinstance(raw, str) and raw.upper() in ("=TRUE()", "=FALSE()"):
                            raw = raw.upper() == "=TRUE()"        # LibreOffice writes booleans so
                        if len(sh.cells) >= MAX_CELLS:
                            raise WorkbookLoadError(f"sheet {ws.title!r} has more than "
                                                    f"{MAX_CELLS:,} cells")
                        sh.set(c.column, c.row, raw, value=_plain(cached.get((c.column, c.row))),
                               fmt=c.number_format if isinstance(c.number_format, str) else "")
                for n, dn in _defined_names(ws):
                    wb.define_name(n, dn, ws.title)
            for n, dn in _defined_names(wb_f):
                wb.define_name(n, dn)
        finally:
            try:
                wb_v.close()
            except Exception:
                pass
        wb.check_size()
        return wb

    @classmethod
    def from_csv(cls, path: str, sheet_name: str = "Sheet1") -> "Workbook":
        wb = cls(os.path.splitext(os.path.basename(path))[0])
        sh = wb.add_sheet(sheet_name)
        with open(path, "rb") as fh:
            data = fh.read()
        for enc in ("utf-8-sig", "cp1252", "latin-1"):
            try:
                text = data.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        try:
            dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t|")
        except csv.Error:
            dialect = csv.excel
        euro = getattr(dialect, "delimiter", ",") == ";"     # "3,5" means 3.5 in ;-separated CSV
        for r, row in enumerate(csv.reader(text.splitlines(), dialect), start=1):
            for c, cell_text in enumerate(row, start=1):
                if euro and _EURO_NUMBER.fullmatch(cell_text.strip()):
                    cell_text = cell_text.strip().replace(".", "").replace(",", ".")
                sh.set(c, r, _coerce_literal(cell_text))
                if len(sh.cells) > MAX_CELLS:
                    raise WorkbookLoadError(f"CSV has more than {MAX_CELLS:,} cells")
        return wb

    @classmethod
    def load(cls, path: str) -> "Workbook":
        if not os.path.exists(path):
            raise FileNotFoundError(f"file not found: {path}")
        if os.path.isdir(path):
            raise WorkbookLoadError(f"{path} is a folder, not a workbook file")
        low = path.lower()
        with open(path, "rb") as fh:
            head = fh.read(4)
        if low.endswith((".xlsx", ".xlsm")):
            if not head.startswith(b"PK"):
                raise WorkbookLoadError(f"{os.path.basename(path)} is not a valid .xlsx file. If it "
                                        f"is an old .xls file, open it and save it as .xlsx first.")
            return cls.from_xlsx(path)
        if low.endswith((".csv", ".tsv", ".txt")):
            return cls.from_csv(path)
        if low.endswith((".xls", ".ods", ".numbers")):
            raise WorkbookLoadError(f"{os.path.basename(path)}: this format is not supported. "
                                    f"Save it as .xlsx (or .csv) and try again.")
        if head.startswith(b"PK"):                    # a zip, i.e. an .xlsx with another name
            return cls.from_xlsx(path)
        with open(path, encoding="utf-8-sig") as fh:
            try:
                data = json.load(fh)
            except ValueError:
                raise WorkbookLoadError(f"{os.path.basename(path)} is not a workbook: use .xlsx, "
                                        f".csv or SheetGuard .json") from None
        wb = cls.from_json(data)
        if wb.name == "Workbook":
            wb.name = os.path.splitext(os.path.basename(path))[0]
        return wb

    def check_size(self) -> None:
        n = sum(len(sh.cells) for sh in self.sheets.values())
        if n > MAX_CELLS:
            raise WorkbookLoadError(f"workbook too large: {n:,} cells (limit {MAX_CELLS:,})")

    def to_json(self) -> dict:
        """Inverse of from_json (used to ship the demo workbook to the web UI)."""
        sheets = []
        for sh in self.sheets.values():
            if sh.max_row * sh.max_col > max(200_000, 4 * len(sh.cells)):
                # sparse sheet (e.g. one stray value at XFD1048576): never build a dense grid
                sheets.append({"name": sh.name, "cells": [
                    [c.row, c.col, _jsonable(c.raw),
                     None if c.value is None or c.is_formula and c.value == "" else _jsonable(c.value),
                     c.fmt or ""] for c in sh]})
                continue
            raw = sh.grid("raw")
            fmts = sh.grid("fmt")
            vals = sh.grid("value")
            sheets.append({
                "name": sh.name,
                "formulas": [[x if is_formula(x) else "" for x in row] for row in raw],
                "values": [[_jsonable(v) if v is not None else
                            ("" if is_formula(x) or x is None else x)
                            for v, x in zip(vrow, rrow)] for vrow, rrow in zip(vals, raw)],
                "formats": [[f or "" for f in row] for row in fmts],
            })
        return {"name": self.name, "sheets": sheets,
                "namedRanges": [{"name": n, "range": d, "scope": s}
                                for (n, s), d in self.names.items()]}


def _rows(x) -> list:
    if not isinstance(x, list):
        return []
    return [r if isinstance(r, list) else [r] for r in x]


def _defined_names(owner):
    """(name, definition) pairs from an openpyxl workbook or worksheet, any openpyxl version."""
    dn = getattr(owner, "defined_names", None)
    if dn is None:
        return []
    out = []
    try:
        items = dn.items() if hasattr(dn, "items") else [(d.name, d) for d in dn.definedName]
    except Exception:
        return []
    for name, d in items:
        text = getattr(d, "attr_text", None)
        if isinstance(text, str) and text and "#REF!" not in text and not name.startswith("_xlnm"):
            out.append((name, text))
    return out


_EURO_NUMBER = re.compile(r"-?\d{1,3}(\.\d{3})*,\d+|-?\d+,\d+")
_NUMBER_LITERAL = re.compile(r"[-+]?(\d[\d,]*\.?\d*|\.\d+)([eE][-+]?\d+)?")


def _json_literal(v: Any) -> Any:
    """A value from the JSON form (Apps Script getValues() / Workbook.to_json()) is already
    typed: numbers arrive as numbers, so a *string* "78" is a number stored as text and
    must stay text (otherwise SG305 can never fire for Google Sheets). Only the error
    spellings are normalised."""
    if not isinstance(v, str):
        return v
    s = v.strip()
    if s == "":
        return None
    if is_formula(s):
        return s
    if s.upper() in ERROR_LITERALS:
        return s.upper()
    return v


def _coerce_literal(v: Any) -> Any:
    """Turn the text a user typed into a typed constant ('12' -> 12.0, 'TRUE' -> True)."""
    if not isinstance(v, str):
        return v
    s = v.strip()
    if s == "":
        return None
    if is_formula(s):
        return s
    if s.upper() in ("TRUE", "FALSE"):
        return s.upper() == "TRUE"
    if s.upper() in ERROR_LITERALS:
        return s.upper()
    if not _NUMBER_LITERAL.fullmatch(s):              # no "nan", "inf", "1_000"
        return v
    try:
        f = float(s.replace(",", ""))
    except ValueError:
        return v
    if not math.isfinite(f):
        return v
    return int(f) if f.is_integer() and "." not in s and "e" not in s.lower() else f
