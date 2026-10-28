"""Built-in function catalogue = the 'standard library' symbol table.

Every function has a signature used by the semantic analyser:
  * arity            min / max number of arguments (max None = variadic)
  * param kinds      what each argument should be  (num, text, bool, any, range, crit)
  * return kind      num | text | bool | date | any | unit
                     'unit' means "same unit as the numeric arguments"
                     (SUM of Currency is Currency, AVERAGE of Percent is Percent)
  * volatile         re-evaluated on every recalculation (NOW, RAND...) -
                     the optimizer must never constant-fold these.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional


@dataclass(frozen=True)
class Signature:
    name: str
    min_args: int
    max_args: Optional[int]
    params: List[str] = field(default_factory=list)   # last one repeats if variadic
    returns: str = "num"
    category: str = "Math"
    description: str = ""
    volatile: bool = False

    def param_kind(self, i: int) -> str:
        if not self.params:
            return "any"
        return self.params[i] if i < len(self.params) else self.params[-1]

    def arity_text(self) -> str:
        if self.max_args is None:
            return f"at least {self.min_args}"
        if self.min_args == self.max_args:
            return f"exactly {self.min_args}"
        return f"{self.min_args} to {self.max_args}"


def _s(name, lo, hi, params, ret, cat, desc, volatile=False):
    return Signature(name, lo, hi, params, ret, cat, desc, volatile)


_CATALOG = [
    # --- Math & aggregation
    _s("SUM", 1, None, ["num"], "unit", "Math", "Adds all numbers in the arguments"),
    _s("PRODUCT", 1, None, ["num"], "num", "Math", "Multiplies all numbers"),
    _s("AVERAGE", 1, None, ["num"], "unit", "Statistical", "Arithmetic mean"),
    _s("MEDIAN", 1, None, ["num"], "unit", "Statistical", "Median value"),
    _s("MIN", 1, None, ["num"], "unit", "Statistical", "Smallest value"),
    _s("MAX", 1, None, ["num"], "unit", "Statistical", "Largest value"),
    _s("COUNT", 1, None, ["any"], "num", "Statistical", "Counts numeric cells"),
    _s("COUNTA", 1, None, ["any"], "num", "Statistical", "Counts non-empty cells"),
    _s("COUNTBLANK", 1, 1, ["range"], "num", "Statistical", "Counts empty cells"),
    _s("STDEV", 1, None, ["num"], "unit", "Statistical", "Sample standard deviation"),
    _s("STDEV.S", 1, None, ["num"], "unit", "Statistical", "Sample standard deviation"),
    _s("VAR", 1, None, ["num"], "num", "Statistical", "Sample variance"),
    _s("ROUND", 2, 2, ["num", "num"], "unit", "Math", "Rounds to N digits"),
    _s("ROUNDUP", 2, 2, ["num", "num"], "unit", "Math", "Rounds away from zero"),
    _s("ROUNDDOWN", 2, 2, ["num", "num"], "unit", "Math", "Rounds towards zero"),
    _s("INT", 1, 1, ["num"], "unit", "Math", "Rounds down to an integer"),
    _s("ABS", 1, 1, ["num"], "unit", "Math", "Absolute value"),
    _s("SQRT", 1, 1, ["num"], "num", "Math", "Square root"),
    _s("POWER", 2, 2, ["num", "num"], "num", "Math", "Raises to a power"),
    _s("MOD", 2, 2, ["num", "num"], "num", "Math", "Remainder after division"),
    _s("EXP", 1, 1, ["num"], "num", "Math", "e raised to a power"),
    _s("LN", 1, 1, ["num"], "num", "Math", "Natural logarithm"),
    _s("LOG", 1, 2, ["num", "num"], "num", "Math", "Logarithm to a base"),
    _s("LOG10", 1, 1, ["num"], "num", "Math", "Base-10 logarithm"),
    _s("PI", 0, 0, [], "num", "Math", "The constant pi"),
    _s("CEILING", 1, 2, ["num", "num"], "unit", "Math", "Rounds up to a multiple"),
    _s("FLOOR", 1, 2, ["num", "num"], "unit", "Math", "Rounds down to a multiple"),
    _s("SIGN", 1, 1, ["num"], "num", "Math", "Sign of a number"),
    _s("SUMPRODUCT", 1, None, ["range"], "num", "Math", "Sum of element-wise products"),
    _s("SUMIF", 2, 3, ["range", "crit", "range"], "unit", "Math", "Conditional sum"),
    _s("SUMIFS", 3, None, ["range", "range", "crit"], "unit", "Math", "Multi-condition sum"),
    _s("COUNTIF", 2, 2, ["range", "crit"], "num", "Statistical", "Conditional count"),
    _s("COUNTIFS", 2, None, ["range", "crit"], "num", "Statistical", "Multi-condition count"),
    _s("AVERAGEIF", 2, 3, ["range", "crit", "range"], "unit", "Statistical",
       "Conditional average"),
    _s("MAXIFS", 3, None, ["range", "range", "crit"], "unit", "Statistical",
       "Conditional maximum"),
    _s("MINIFS", 3, None, ["range", "range", "crit"], "unit", "Statistical",
       "Conditional minimum"),
    _s("RAND", 0, 0, [], "num", "Math", "Random number in [0,1)", volatile=True),
    _s("RANDBETWEEN", 2, 2, ["num", "num"], "num", "Math", "Random integer", volatile=True),
    # --- Logical
    _s("IF", 2, 3, ["bool", "any", "any"], "any", "Logical", "Conditional choice"),
    _s("IFS", 2, None, ["bool", "any"], "any", "Logical", "First true condition wins"),
    _s("IFERROR", 2, 2, ["any", "any"], "any", "Logical", "Fallback value on error"),
    _s("IFNA", 2, 2, ["any", "any"], "any", "Logical", "Fallback value on #N/A"),
    _s("AND", 1, None, ["bool"], "bool", "Logical", "True if all are true"),
    _s("OR", 1, None, ["bool"], "bool", "Logical", "True if any is true"),
    _s("XOR", 1, None, ["bool"], "bool", "Logical", "Exclusive or"),
    _s("NOT", 1, 1, ["bool"], "bool", "Logical", "Logical negation"),
    _s("TRUE", 0, 0, [], "bool", "Logical", "The value TRUE"),
    _s("FALSE", 0, 0, [], "bool", "Logical", "The value FALSE"),
    _s("SWITCH", 3, None, ["any"], "any", "Logical", "Match against a list of cases"),
    # --- Information
    _s("ISBLANK", 1, 1, ["any"], "bool", "Info", "Is the cell empty?"),
    _s("ISNUMBER", 1, 1, ["any"], "bool", "Info", "Is the value a number?"),
    _s("ISTEXT", 1, 1, ["any"], "bool", "Info", "Is the value text?"),
    _s("ISERROR", 1, 1, ["any"], "bool", "Info", "Is the value an error?"),
    _s("ISNA", 1, 1, ["any"], "bool", "Info", "Is the value #N/A?"),
    _s("NA", 0, 0, [], "any", "Info", "Returns #N/A"),
    # --- Lookup
    _s("VLOOKUP", 3, 4, ["any", "range", "num", "bool"], "any", "Lookup", "Vertical lookup"),
    _s("HLOOKUP", 3, 4, ["any", "range", "num", "bool"], "any", "Lookup", "Horizontal lookup"),
    _s("XLOOKUP", 3, 6, ["any", "range", "range", "any", "num", "num"], "any", "Lookup",
       "Modern lookup"),
    _s("INDEX", 2, 3, ["range", "num", "num"], "any", "Lookup", "Value at a position"),
    _s("MATCH", 2, 3, ["any", "range", "num"], "num", "Lookup", "Position of a value"),
    _s("CHOOSE", 2, None, ["num", "any"], "any", "Lookup", "Pick from a list by index"),
    _s("ROW", 0, 1, ["range"], "num", "Lookup", "Row number"),
    _s("COLUMN", 0, 1, ["range"], "num", "Lookup", "Column number"),
    _s("ROWS", 1, 1, ["range"], "num", "Lookup", "Number of rows"),
    _s("COLUMNS", 1, 1, ["range"], "num", "Lookup", "Number of columns"),
    _s("OFFSET", 3, 5, ["range", "num", "num", "num", "num"], "any", "Lookup",
       "Shifted reference", volatile=True),
    _s("INDIRECT", 1, 2, ["text", "bool"], "any", "Lookup", "Reference from text",
       volatile=True),
    # --- Text
    _s("CONCATENATE", 1, None, ["text"], "text", "Text", "Joins text"),
    _s("CONCAT", 1, None, ["text"], "text", "Text", "Joins text"),
    _s("TEXTJOIN", 3, None, ["text", "bool", "text"], "text", "Text", "Joins with delimiter"),
    _s("LEN", 1, 1, ["text"], "num", "Text", "Length of text"),
    _s("LEFT", 1, 2, ["text", "num"], "text", "Text", "Leftmost characters"),
    _s("RIGHT", 1, 2, ["text", "num"], "text", "Text", "Rightmost characters"),
    _s("MID", 3, 3, ["text", "num", "num"], "text", "Text", "Characters from the middle"),
    _s("UPPER", 1, 1, ["text"], "text", "Text", "Upper-case"),
    _s("LOWER", 1, 1, ["text"], "text", "Text", "Lower-case"),
    _s("PROPER", 1, 1, ["text"], "text", "Text", "Title-case"),
    _s("TRIM", 1, 1, ["text"], "text", "Text", "Removes extra spaces"),
    _s("TEXT", 2, 2, ["num", "text"], "text", "Text", "Formats a number as text"),
    _s("VALUE", 1, 1, ["text"], "num", "Text", "Converts text to a number"),
    _s("FIND", 2, 3, ["text", "text", "num"], "num", "Text", "Position of text (case-sensitive)"),
    _s("SEARCH", 2, 3, ["text", "text", "num"], "num", "Text", "Position of text"),
    _s("SUBSTITUTE", 3, 4, ["text", "text", "text", "num"], "text", "Text", "Replaces text"),
    _s("REPT", 2, 2, ["text", "num"], "text", "Text", "Repeats text"),
    # --- Date & time
    _s("TODAY", 0, 0, [], "date", "Date", "Today's date", volatile=True),
    _s("NOW", 0, 0, [], "date", "Date", "Current date and time", volatile=True),
    _s("DATE", 3, 3, ["num", "num", "num"], "date", "Date", "Builds a date"),
    _s("YEAR", 1, 1, ["date"], "num", "Date", "Year of a date"),
    _s("MONTH", 1, 1, ["date"], "num", "Date", "Month of a date"),
    _s("DAY", 1, 1, ["date"], "num", "Date", "Day of a date"),
    _s("WEEKDAY", 1, 2, ["date", "num"], "num", "Date", "Day of week"),
    _s("EDATE", 2, 2, ["date", "num"], "date", "Date", "Date N months away"),
    _s("EOMONTH", 2, 2, ["date", "num"], "date", "Date", "End of month N months away"),
    _s("DAYS", 2, 2, ["date", "date"], "num", "Date", "Days between dates"),
    _s("DATEDIF", 3, 3, ["date", "date", "text"], "num", "Date", "Difference between dates"),
    # --- Financial
    _s("PMT", 3, 5, ["num", "num", "num", "num", "num"], "num", "Financial", "Loan payment"),
    _s("FV", 3, 5, ["num", "num", "num", "num", "num"], "num", "Financial", "Future value"),
    _s("PV", 3, 5, ["num", "num", "num", "num", "num"], "num", "Financial", "Present value"),
    _s("NPV", 2, None, ["num"], "num", "Financial", "Net present value"),
]

# evaluated since v1.3
_CATALOG += [
    _s("TRUNC", 1, 2, ["num", "num"], "unit", "Math", "Truncates toward zero to N digits"),
    _s("EVEN", 1, 1, ["num"], "num", "Math", "Rounds away from zero to an even integer"),
    _s("ODD", 1, 1, ["num"], "num", "Math", "Rounds away from zero to an odd integer"),
    _s("FACT", 1, 1, ["num"], "num", "Math", "Factorial"),
    _s("COMBIN", 2, 2, ["num", "num"], "num", "Math", "Number of combinations"),
    _s("GCD", 1, None, ["num"], "num", "Math", "Greatest common divisor"),
    _s("LCM", 1, None, ["num"], "num", "Math", "Least common multiple"),
    _s("MROUND", 2, 2, ["num", "num"], "unit", "Math", "Rounds to a multiple"),
    _s("QUOTIENT", 2, 2, ["num", "num"], "num", "Math", "Integer part of a division"),
    _s("SUMSQ", 1, None, ["num"], "num", "Math", "Sum of squares"),
    _s("LARGE", 2, 2, ["range", "num"], "unit", "Statistical", "k-th largest value"),
    _s("SMALL", 2, 2, ["range", "num"], "unit", "Statistical", "k-th smallest value"),
    _s("EXACT", 2, 2, ["text", "text"], "bool", "Text", "Case-sensitive comparison"),
    _s("CHAR", 1, 1, ["num"], "text", "Text", "Character for a code point"),
    _s("CODE", 1, 1, ["text"], "num", "Text", "Code point of the first character"),
    _s("CLEAN", 1, 1, ["text"], "text", "Text", "Removes non-printable characters"),
    _s("N", 1, 1, ["any"], "num", "Info", "Value converted to a number"),
    _s("T", 1, 1, ["any"], "text", "Info", "Text if the value is text, else empty"),
    _s("ISODD", 1, 1, ["num"], "bool", "Info", "TRUE if odd"),
    _s("ISEVEN", 1, 1, ["num"], "bool", "Info", "TRUE if even"),
    _s("ISLOGICAL", 1, 1, ["any"], "bool", "Info", "TRUE if a boolean"),
    _s("HOUR", 1, 1, ["date"], "num", "Date", "Hour of a time value"),
    _s("MINUTE", 1, 1, ["date"], "num", "Date", "Minute of a time value"),
    _s("SECOND", 1, 1, ["date"], "num", "Date", "Second of a time value"),
    _s("TIME", 3, 3, ["num", "num", "num"], "num", "Date", "Time value from h, m, s"),
    _s("STDEV.P", 1, None, ["num"], "unit", "Statistical", "Population standard deviation"),
    _s("STDEVP", 1, None, ["num"], "unit", "Statistical", "Population standard deviation"),
    _s("VAR.S", 1, None, ["num"], "num", "Statistical", "Sample variance"),
    _s("VAR.P", 1, None, ["num"], "num", "Statistical", "Population variance"),
    _s("VARP", 1, None, ["num"], "num", "Statistical", "Population variance"),
    _s("COUNTUNIQUE", 1, None, ["any"], "num", "Statistical", "Number of distinct values"),
    _s("MODE", 1, None, ["num"], "unit", "Statistical", "Most frequent value"),
    _s("RANK", 2, 3, ["num", "range", "num"], "num", "Statistical", "Rank of a value in a list"),
    _s("AVERAGEIFS", 3, None, ["range", "range", "crit"], "unit", "Statistical",
       "Average with several conditions"),
    _s("PERCENTILE", 2, 2, ["range", "num"], "unit", "Statistical", "k-th percentile"),
]

# Google-Sheets / dynamic-array functions SheetGuard recognises (no "unknown function"
# false positives) but does not type-check in detail or evaluate.
_RECOGNISED_ONLY = [
    "ARRAYFORMULA", "FILTER", "UNIQUE", "SORT", "SORTN", "QUERY", "IMPORTRANGE", "IMPORTDATA",
    "IMPORTHTML", "IMPORTXML", "GOOGLEFINANCE", "GOOGLETRANSLATE", "SPLIT", "JOIN",
    "REGEXMATCH", "REGEXEXTRACT", "REGEXREPLACE", "SPARKLINE", "IMAGE", "HYPERLINK", "FLATTEN",
    "SEQUENCE", "TRANSPOSE", "LET", "LAMBDA", "MAP", "REDUCE", "BYROW", "BYCOL", "MAKEARRAY",
    "SUBTOTAL",      "QUARTILE", "CORREL",
    "FORECAST", "TREND", "GROWTH", "IRR", "XIRR", "XNPV", "RATE", "NPER",  
        "TYPE",    "ISREF", "ISFORMULA",
    "ISERR", "ERROR.TYPE",  "WORKDAY", "NETWORKDAYS",   
     "DATEVALUE", "TIMEVALUE", "WEEKNUM", "ISOWEEKNUM", "YEARFRAC",   
          "RANDARRAY",  
         "AVERAGEA", "MAXA", "MINA", "ADDRESS",
    "LOOKUP", "XMATCH", "FIXED", "DOLLAR", "NUMBERVALUE", "UNICHAR", "UNICODE", "TEXTBEFORE",
    "TEXTAFTER", "TEXTSPLIT", "VSTACK", "HSTACK", "TOCOL", "TOROW", "CHOOSECOLS", "CHOOSEROWS",
    "TAKE", "DROP", "EXPAND", "WRAPROWS", "WRAPCOLS",
]
for _n in _RECOGNISED_ONLY:
    _CATALOG.append(_s(_n, 0, None, ["any"], "any", "Other", "Recognised (not evaluated)"))

FUNCTIONS = {sig.name: sig for sig in _CATALOG}


def lookup(name: str) -> Optional[Signature]:
    return FUNCTIONS.get(name.upper())


def arity_ok(name: str, n: int) -> bool:
    """True if a call with n arguments is well-formed (unknown functions: not our concern here).

    Google Sheets evaluates a call with the wrong number of arguments to #N/A, so every
    back-end checks this first and produces #N/A instead of crashing on a missing argument.
    """
    sig = FUNCTIONS.get(name.upper())
    if sig is None:
        return True
    return n >= sig.min_args and (sig.max_args is None or n <= sig.max_args)


def suggest(name: str, n: int = 1) -> list[str]:
    """'Did you mean ...?' - nearest catalogue names by edit distance."""
    import difflib
    return difflib.get_close_matches(name.upper(), FUNCTIONS.keys(), n=n, cutoff=0.6)
