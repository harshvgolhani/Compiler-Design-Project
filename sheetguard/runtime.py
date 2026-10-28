"""The runtime library: spreadsheet value semantics + built-in functions.

Shared by the interpreter (evaluator.py) and by the code the compiler emits
(codegen.py), so both execution paths give identical results.

Value model
-----------
    float / int   numbers (dates are serial day numbers, epoch 1899-12-30)
    str           text
    bool          TRUE / FALSE
    None          an empty cell
    XLError       #DIV/0!, #VALUE!, #REF!, #NAME?, #N/A, #NUM!
    Range         a 2-D block of values (list of rows)
"""
from __future__ import annotations

import datetime as _dt
import math
import random
import re
from typing import Any, Callable, List

from .functions import arity_ok


class XLError(Exception):
    """An error *value* (it is also raisable, to unwind evaluation quickly)."""

    __slots__ = ("code",)

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code

    def __repr__(self):
        return self.code

    __str__ = __repr__

    def __eq__(self, other):
        return isinstance(other, XLError) and other.code == self.code

    def __hash__(self):
        return hash(self.code)


DIV0 = XLError("#DIV/0!")
VALUE = XLError("#VALUE!")
REF = XLError("#REF!")
NAME = XLError("#NAME?")
NA = XLError("#N/A")
NUM = XLError("#NUM!")
CIRC = XLError("#CIRC!")       # SheetGuard's own marker for cells inside a cycle
PARSE = XLError("#ERROR!")     # formula could not be parsed (Google Sheets shows #ERROR!)

ERRORS = {e.code: e for e in (DIV0, VALUE, REF, NAME, NA, NUM, XLError("#NULL!"), CIRC, PARSE)}


def as_error(v):
    if isinstance(v, str) and v in ERRORS:
        return ERRORS[v]
    return v


class Range:
    __slots__ = ("rows",)

    def __init__(self, rows: List[List[Any]]):
        self.rows = rows

    def flat(self):
        for r in self.rows:
            yield from r

    @property
    def shape(self):
        return len(self.rows), (len(self.rows[0]) if self.rows else 0)

    def __repr__(self):
        return f"Range{self.shape}"


# --------------------------------------------------------------- coercions
_NUMTEXT = re.compile(r"^\s*([-+]?)\s*[$€£₹¥]?\s*([-+]?\d[\d,]*(\.\d*)?|\.\d+)([eE][-+]?\d+)?\s*(%?)\s*$")


def num(v) -> float:
    """Coerce a scalar to a number the way a spreadsheet does in arithmetic."""
    if isinstance(v, bool):
        return 1.0 if v else 0.0
    if isinstance(v, (int, float)):
        return v
    if v is None:
        return 0.0
    if isinstance(v, XLError):
        raise v
    if isinstance(v, str):
        m = _NUMTEXT.match(v)
        if m:
            x = float((m.group(2) + (m.group(4) or "")).replace(",", ""))
            x = -x if m.group(1) == "-" else x
            return x / 100 if m.group(5) else x
        raise VALUE
    if isinstance(v, Range):
        if v.shape == (1, 1):
            return num(v.rows[0][0])
        raise VALUE
    if isinstance(v, (_dt.date, _dt.datetime)):
        return date_to_serial(v)
    raise VALUE


def num_to_text(v: float) -> str:
    """A number as text the way a spreadsheet shows it in "&" / CONCATENATE: at most
    15 significant digits (=0.1+0.2&"" is "0.3", not "0.30000000000000004")."""
    if math.isnan(v) or math.isinf(v):
        raise NUM
    if v.is_integer() and abs(v) < 1e15:
        return str(int(v))
    from decimal import ROUND_HALF_UP, Decimal
    d = Decimal(v)                                     # exact binary value
    x = d.adjusted()
    q = d.quantize(Decimal(1).scaleb(x - 14), rounding=ROUND_HALF_UP)   # 15 significant digits
    if q.adjusted() != x:                              # rounded up to the next power of ten
        x = q.adjusted()
        q = d.quantize(Decimal(1).scaleb(x - 14), rounding=ROUND_HALF_UP)
    if x < -4 or x >= 15:
        mant, exp = format(q, "E").split("E")
        if "." in mant:
            mant = mant.rstrip("0").rstrip(".")
        return f"{mant}E{exp[0]}{exp[1:].lstrip('0').rjust(2, '0')}"
    s = format(q, "f")
    return s.rstrip("0").rstrip(".") if "." in s else s


def text(v) -> str:
    if isinstance(v, XLError):
        raise v
    if v is None:
        return ""
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, (int, float)):
        return num_to_text(float(v))
    if isinstance(v, Range):
        return text(v.rows[0][0]) if v.shape == (1, 1) else _raise(VALUE)
    return str(v)


def truthy(v) -> bool:
    if isinstance(v, XLError):
        raise v
    if isinstance(v, bool):
        return v
    if v is None:
        return False
    if isinstance(v, (int, float)):
        return v != 0
    if isinstance(v, str):
        u = v.upper()
        if u in ("TRUE", "FALSE"):
            return u == "TRUE"
        raise VALUE
    if isinstance(v, Range) and v.shape == (1, 1):
        return truthy(v.rows[0][0])
    raise VALUE


def _raise(e):
    raise e


def scalar(v):
    """Implicit intersection-lite: a 1×1 range used as a scalar."""
    if isinstance(v, Range):
        if v.shape == (1, 1):
            return v.rows[0][0]
        raise VALUE
    return v


# --------------------------------------------------------------- operators
def add(a, b):
    return num(scalar(a)) + num(scalar(b))


def sub(a, b):
    return num(scalar(a)) - num(scalar(b))


def mul(a, b):
    return num(scalar(a)) * num(scalar(b))


def div(a, b):
    n = num(scalar(a))                 # operands are coerced left to right: ="x"/0 is #VALUE!
    d = num(scalar(b))
    if d == 0:
        raise DIV0
    return n / d


def power(a, b):
    try:
        r = num(scalar(a)) ** num(scalar(b))
    except ZeroDivisionError:
        raise DIV0
    except OverflowError:          # found by the property-based TAC test
        raise NUM
    if isinstance(r, complex):
        raise NUM
    return r


def neg(a):
    return -num(scalar(a))


def pos(a):
    return num(scalar(a))


def pct(a):
    return num(scalar(a)) / 100


def concat(a, b):
    return text(scalar(a)) + text(scalar(b))


def _rank(v):
    if v is None:
        return 0
    if isinstance(v, bool):
        return 3
    if isinstance(v, (int, float)):
        return 1
    return 2


def compare(op: str, a, b) -> bool:
    a, b = scalar(a), scalar(b)
    for v in (a, b):
        if isinstance(v, XLError):
            raise v
    if a is None:
        a = "" if isinstance(b, str) else (False if isinstance(b, bool) else 0.0)
    if b is None:
        b = "" if isinstance(a, str) else (False if isinstance(a, bool) else 0.0)
    ra, rb = _rank(a), _rank(b)
    if ra != rb:
        x, y = ra, rb
    elif isinstance(a, str):
        x, y = a.lower(), b.lower()
    else:
        x, y = a, b
    return {"=": x == y, "<>": x != y, "<": x < y, ">": x > y, "<=": x <= y,
            ">=": x >= y}[op]


BINOPS = {"+": add, "-": sub, "*": mul, "/": div, "^": power, "&": concat}


# --------------------------------------------------------------- helpers
def _nums(args, strict_scalars=True):
    """Numbers for aggregate functions: ranges skip text/bools/blanks, but direct
    scalar arguments are coerced (Excel rule)."""
    out = []
    for a in args:
        if isinstance(a, Range):
            for v in a.flat():
                if isinstance(v, XLError):
                    raise v
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    out.append(v)
        elif a is None:
            continue
        else:
            out.append(num(a) if strict_scalars else a)
    return out


def _flat(args):
    for a in args:
        if isinstance(a, Range):
            yield from a.flat()
        else:
            yield a


def _crit(c) -> Callable[[Any], bool]:
    """Compile a SUMIF/COUNTIF criterion like '>=10', '<>x', 'ab*' into a predicate."""
    if isinstance(c, XLError):
        raise c
    if not isinstance(c, str):
        target = c

        def eq(v):
            if isinstance(target, (int, float)) and not isinstance(target, bool):
                try:
                    return v is not None and not isinstance(v, bool) and num(v) == target
                except XLError:
                    return False
            return v == target
        return eq
    m = re.match(r"^(<=|>=|<>|<|>|=)?(.*)$", c, re.S)
    op, rhs = m.group(1) or "=", m.group(2)
    try:
        rnum = num(rhs) if rhs != "" else None
    except XLError:
        rnum = None
    if rnum is not None:
        def p(v):
            if v is None or isinstance(v, (bool, XLError)):
                return op == "<>"
            try:
                return compare(op, num(v), rnum)
            except XLError:
                return op == "<>"
        return p
    pat = re.compile("^" + re.escape(rhs).replace(r"\*", ".*").replace(r"\?", ".") + "$",
                     re.I | re.S)

    def ptxt(v):
        s = "" if v is None else (text(v) if not isinstance(v, XLError) else "")
        if op == "=":
            return bool(pat.match(s)) if rhs != "" else v is None or v == ""
        if op == "<>":
            return not pat.match(s)
        if not isinstance(v, str):
            return False
        return compare(op, v, rhs)
    return ptxt


def _cells(r):
    return list(r.flat()) if isinstance(r, Range) else [r]


# --------------------------------------------------------------- dates
_EPOCH = _dt.date(1899, 12, 30)


def date_to_serial(d) -> float:
    if isinstance(d, _dt.datetime):
        delta = d - _dt.datetime(1899, 12, 30)
        return delta.days + delta.seconds / 86400
    return float((d - _EPOCH).days)


def serial_to_date(s) -> _dt.date:
    return _EPOCH + _dt.timedelta(days=int(num(s)))


def _add_months(d: _dt.date, months: int) -> _dt.date:
    m = d.month - 1 + months
    y, m = d.year + m // 12, m % 12 + 1
    import calendar
    day = min(d.day, calendar.monthrange(y, m)[1])
    return _dt.date(y, m, day)


# --------------------------------------------------------------- functions
def _round(x, n, mode):
    x, n = num(scalar(x)), int(num(scalar(n)))
    if n > 15:
        return x                       # beyond double precision: nothing to round
    if n < -308:
        return 0.0
    f = 10.0 ** n
    if mode == "half":
        v = math.floor(abs(x) * f + 0.5 + 1e-9) / f
    elif mode == "up":
        v = math.ceil(abs(x) * f - 1e-9) / f
    else:
        v = math.floor(abs(x) * f + 1e-9) / f
    return math.copysign(v, x)


def xsum(xs) -> float:
    """Left-to-right double addition, like a spreadsheet. (Python 3.12+'s sum() uses
    compensated summation, which would make results depend on the Python version.)"""
    t = 0.0
    for x in xs:
        t += x
    return t


def _logicals(args):
    """AND/OR operands: blanks are ignored; with nothing left the result is #VALUE!."""
    vals = [truthy(v) for v in _flat(args) if v is not None]
    if not vals:
        raise VALUE
    return vals


def _mod(a, b):
    if b == 0:
        raise DIV0
    return a - b * math.floor(a / b)


def _avg(xs):
    if not xs:
        raise DIV0
    return xsum(xs) / len(xs)


def _stdev(xs):
    if len(xs) < 2:
        raise DIV0
    m = xsum(xs) / len(xs)
    return math.sqrt(xsum((x - m) ** 2 for x in xs) / (len(xs) - 1))


def _median(xs):
    if not xs:
        raise NUM
    s = sorted(xs)
    k = len(s) // 2
    return s[k] if len(s) % 2 else (s[k - 1] + s[k]) / 2


def _sumif(rng, crit, sum_rng=None):
    p = _crit(scalar(crit) if not isinstance(crit, Range) else crit.rows[0][0])
    keys = _cells(rng)
    vals = _cells(sum_rng) if sum_rng is not None else keys
    return xsum(v for k, v in zip(keys, vals) if p(k) and isinstance(v, (int, float))
               and not isinstance(v, bool))


def _ifs_mask(pairs):
    mask = None
    for rng, crit in pairs:
        p = _crit(scalar(crit))
        m = [p(v) for v in _cells(rng)]
        if mask is not None and len(m) != len(mask):
            raise VALUE
        mask = m if mask is None else [a and b for a, b in zip(mask, m)]
    return mask


def _sumifs(sum_rng, *rest):
    mask = _ifs_mask(list(zip(rest[0::2], rest[1::2])))
    vals = _cells(sum_rng)
    if len(vals) != len(mask):
        raise VALUE
    return xsum(v for v, k in zip(vals, mask) if k and isinstance(v, (int, float))
               and not isinstance(v, bool))


def _countifs(*rest):
    return float(sum(_ifs_mask(list(zip(rest[0::2], rest[1::2])))))


def _xifs(fn):
    def f(val_rng, *rest):
        mask = _ifs_mask(list(zip(rest[0::2], rest[1::2])))
        xs = [v for v, k in zip(_cells(val_rng), mask) if k and isinstance(v, (int, float))
              and not isinstance(v, bool)]
        return fn(xs) if xs else 0.0
    return f


def _lookup_match(key, cands, approx):
    key = scalar(key)
    if not approx:
        for i, c in enumerate(cands):
            try:
                if compare("=", c, key):
                    return i
            except XLError:
                continue
        raise NA
    best = None
    for i, c in enumerate(cands):
        if c is None:
            continue
        try:
            if _rank(c) == _rank(key) and compare("<=", c, key):
                best = i
            elif _rank(c) == _rank(key):
                break
        except XLError:
            continue
    if best is None:
        raise NA
    return best


def _vlookup(key, table, col, approx=True):
    rows = table.rows if isinstance(table, Range) else [[table]]
    k = int(num(col))
    if k < 1:
        raise VALUE
    if k > len(rows[0]):
        raise REF
    i = _lookup_match(key, [r[0] for r in rows], approx is None or truthy(approx))
    return rows[i][k - 1]


def _hlookup(key, table, row, approx=True):
    rows = table.rows if isinstance(table, Range) else [[table]]
    k = int(num(row))
    if k < 1:
        raise VALUE
    if k > len(rows):
        raise REF
    i = _lookup_match(key, rows[0], approx is None or truthy(approx))
    return rows[k - 1][i]


def _xlookup(key, look, ret, if_nf=None, mode=0, _search=1):
    cands = _cells(look)
    vals = _cells(ret)
    try:
        i = _lookup_match(key, cands, False)
    except XLError:
        if if_nf is not None:
            return if_nf
        raise
    return vals[i]


def _index(rng, r, c=None):
    rows = rng.rows if isinstance(rng, Range) else [[rng]]
    r = int(num(r)) if r is not None else 0
    c = int(num(c)) if c is not None else 0
    if len(rows) == 1 and c == 0:
        r, c = 1, r
    if c == 0 and len(rows[0]) == 1:
        c = 1
    if r < 1 or c < 1 or r > len(rows) or c > len(rows[0]):
        raise REF
    return rows[r - 1][c - 1]


def _match(key, rng, mtype=1):
    cands = _cells(rng)
    mtype = int(num(mtype)) if mtype is not None else 1
    return float(_lookup_match(key, cands, mtype != 0) + 1)


def _pmt(rate, nper, pv, fv=0, typ=0):
    rate, nper, pv = num(rate), num(nper), num(pv)
    fv, typ = num(fv or 0), num(typ or 0)
    if rate == 0:
        return -(pv + fv) / nper
    f = (1 + rate) ** nper
    return -(rate * (pv * f + fv)) / ((1 + rate * typ) * (f - 1))


def _fv(rate, nper, pmt, pv=0, typ=0):
    rate, nper, pmt, pv, typ = num(rate), num(nper), num(pmt), num(pv or 0), num(typ or 0)
    if rate == 0:
        return -(pv + pmt * nper)
    f = (1 + rate) ** nper
    return -(pv * f + pmt * (1 + rate * typ) * (f - 1) / rate)


def _pv(rate, nper, pmt, fv=0, typ=0):
    rate, nper, pmt, fv, typ = num(rate), num(nper), num(pmt), num(fv or 0), num(typ or 0)
    if rate == 0:
        return -(fv + pmt * nper)
    f = (1 + rate) ** nper
    return -(fv + pmt * (1 + rate * typ) * (f - 1) / rate) / f


def _npv(rate, *vals):
    r = num(rate)
    return xsum(v / (1 + r) ** (i + 1) for i, v in enumerate(_nums(vals)))


def _text_fmt(v, fmt):
    x = num(v)
    f = text(fmt)
    if "%" in f:
        dec = len(f.split(".")[1].rstrip("%")) if "." in f else 0
        return f"{x * 100:.{dec}f}%"
    dec = len(re.sub(r"[^0#]", "", f.split(".")[1])) if "." in f else 0
    s = f"{x:,.{dec}f}" if "," in f else f"{x:.{dec}f}"
    return ("$" + s) if "$" in f else s


def _left(s, n=1):
    return text(s)[:int(num(n if n is not None else 1))]


def _right(s, n=1):
    n = int(num(n if n is not None else 1))
    return text(s)[-n:] if n else ""


def _find(needle, hay, start=1, case=True):
    h, nd, st = text(hay), text(needle), int(num(start or 1))
    i = (h if case else h.lower()).find(nd if case else nd.lower(), st - 1)
    if i < 0:
        raise VALUE
    return float(i + 1)


def _substitute(s, old, new, inst=None):
    s, old, new = text(s), text(old), text(new)
    if inst is None:
        return s.replace(old, new)
    k = int(num(inst))
    idx = -1
    for _ in range(k):
        idx = s.find(old, idx + 1)
        if idx < 0:
            return s
    return s[:idx] + new + s[idx + len(old):]


def _value(s):
    return num(scalar(s))


def _rows(r):
    return float(r.shape[0] if isinstance(r, Range) else 1)


def _cols(r):
    return float(r.shape[1] if isinstance(r, Range) else 1)


def _sumproduct(*rs):
    arrays = [_cells(r) for r in rs]
    if len({len(a) for a in arrays}) != 1:
        raise VALUE
    tot = 0.0
    for vals in zip(*arrays):
        p = 1.0
        for v in vals:
            p *= v if isinstance(v, (int, float)) and not isinstance(v, bool) else 0.0
        tot += p
    return tot


def _date(y, m, d):
    y, m, d = int(num(y)), int(num(m)), int(num(d))
    if y < 1900:
        y += 1900
    base = _add_months(_dt.date(y, 1, 1), m - 1)
    return date_to_serial(base + _dt.timedelta(days=d - 1))


FUNCS = {
    "SUM": lambda *a: xsum(_nums(a)),
    "PRODUCT": lambda *a: float(math.prod(_nums(a))),
    "AVERAGE": lambda *a: _avg(_nums(a)),
    "MEDIAN": lambda *a: _median(_nums(a)),
    "MIN": lambda *a: float(min(_nums(a), default=0.0)),
    "MAX": lambda *a: float(max(_nums(a), default=0.0)),
    "COUNT": lambda *a: float(sum(1 for v in _flat(a) if isinstance(v, (int, float))
                                  and not isinstance(v, bool))),
    "COUNTA": lambda *a: float(sum(1 for v in _flat(a) if v is not None and v != "")),
    "COUNTBLANK": lambda r: float(sum(1 for v in _cells(r) if v is None or v == "")),
    "STDEV": lambda *a: _stdev(_nums(a)),
    "STDEV.S": lambda *a: _stdev(_nums(a)),
    "VAR": lambda *a: _stdev(_nums(a)) ** 2,
    "ROUND": lambda x, n=0: _round(x, n or 0, "half"),
    "ROUNDUP": lambda x, n=0: _round(x, n or 0, "up"),
    "ROUNDDOWN": lambda x, n=0: _round(x, n or 0, "down"),
    "INT": lambda x: float(math.floor(num(x))),
    "ABS": lambda x: abs(num(x)),
    "SQRT": lambda x: math.sqrt(num(x)) if num(x) >= 0 else _raise(NUM),
    "POWER": lambda a, b: power(a, b),
    "MOD": lambda a, b: _mod(num(scalar(a)), num(scalar(b))),
    "EXP": lambda x: math.exp(num(x)),
    "LN": lambda x: math.log(num(x)) if num(x) > 0 else _raise(NUM),
    "LOG": lambda x, b=10: math.log(num(x), num(b if b is not None else 10))
    if num(x) > 0 else _raise(NUM),
    "LOG10": lambda x: math.log10(num(x)) if num(x) > 0 else _raise(NUM),
    "PI": lambda: math.pi,
    "SIGN": lambda x: float((num(x) > 0) - (num(x) < 0)),
    "CEILING": lambda x, s=1: math.ceil(num(x) / num(s or 1)) * num(s or 1),
    "FLOOR": lambda x, s=1: math.floor(num(x) / num(s or 1)) * num(s or 1),
    "SUMPRODUCT": _sumproduct,
    "SUMIF": _sumif,
    "SUMIFS": _sumifs,
    "COUNTIF": lambda r, c: float(sum(1 for v in _cells(r) if _crit(scalar(c))(v))),
    "COUNTIFS": _countifs,
    "AVERAGEIF": lambda r, c, s=None: (lambda xs: _avg(xs))(
        [v for k, v in zip(_cells(r), _cells(s if s is not None else r))
         if _crit(scalar(c))(k) and isinstance(v, (int, float)) and not isinstance(v, bool)]),
    "MAXIFS": _xifs(max),
    "MINIFS": _xifs(min),
    "RAND": lambda: random.random(),
    "RANDBETWEEN": lambda a, b: float(random.randint(int(num(a)), int(num(b)))),
    "AND": lambda *a: all(_logicals(a)),
    "OR": lambda *a: any(_logicals(a)),
    "XOR": lambda *a: sum(truthy(v) for v in _flat(a) if v is not None) % 2 == 1,
    "NOT": lambda x: not truthy(x),
    "TRUE": lambda: True,
    "FALSE": lambda: False,
    "ISBLANK": lambda x: scalar(x) is None,
    "ISNUMBER": lambda x: isinstance(scalar(x), (int, float)) and not isinstance(scalar(x), bool),
    "ISTEXT": lambda x: isinstance(scalar(x), str),
    "NA": lambda: _raise(NA),
    "VLOOKUP": _vlookup,
    "HLOOKUP": _hlookup,
    "XLOOKUP": _xlookup,
    "INDEX": _index,
    "MATCH": _match,
    "ROWS": _rows,
    "COLUMNS": _cols,
    "CONCATENATE": lambda *a: "".join(text(v) for v in _flat(a)),
    "CONCAT": lambda *a: "".join(text(v) for v in _flat(a)),
    "TEXTJOIN": lambda d, skip, *a: text(d).join(text(v) for v in _flat(a)
                                                  if not (truthy(skip) and (v is None or v == ""))),
    "LEN": lambda s: float(len(text(scalar(s)))),
    "LEFT": _left,
    "RIGHT": _right,
    "MID": lambda s, a, n: text(s)[int(num(a)) - 1:int(num(a)) - 1 + int(num(n))],
    "UPPER": lambda s: text(s).upper(),
    "LOWER": lambda s: text(s).lower(),
    "PROPER": lambda s: text(s).title(),
    "TRIM": lambda s: " ".join(text(s).split()),
    "TEXT": _text_fmt,
    "VALUE": _value,
    "FIND": lambda n, h, s=1: _find(n, h, s, True),
    "SEARCH": lambda n, h, s=1: _find(n, h, s, False),
    "SUBSTITUTE": _substitute,
    "REPT": lambda s, n: text(s) * int(num(n)),
    "TODAY": lambda: date_to_serial(_dt.date.today()),
    "NOW": lambda: date_to_serial(_dt.datetime.now()),
    "DATE": _date,
    "YEAR": lambda d: float(serial_to_date(d).year),
    "MONTH": lambda d: float(serial_to_date(d).month),
    "DAY": lambda d: float(serial_to_date(d).day),
    "WEEKDAY": lambda d, t=1: float((serial_to_date(d).isoweekday() % 7) + 1),
    "EDATE": lambda d, m: date_to_serial(_add_months(serial_to_date(d), int(num(m)))),
    "EOMONTH": lambda d, m: date_to_serial(
        _add_months(serial_to_date(d).replace(day=1), int(num(m)) + 1) - _dt.timedelta(days=1)),
    "DAYS": lambda e, s: float(int(num(e)) - int(num(s))),
    "PMT": _pmt,
    "FV": _fv,
    "PV": _pv,
    "NPV": _npv,
}

# --------------------------------------------------------------- more functions (v1.3)
def _int(x) -> int:
    x = num(scalar(x))
    if math.isnan(x) or math.isinf(x):
        raise NUM
    return int(x)


def _trunc(x, n=0):
    x, n = num(scalar(x)), _int(n if n is not None else 0)
    f = 10.0 ** n
    return math.trunc(x * f) / f


def _even_odd(x, odd):
    x = num(scalar(x))
    s = -1.0 if x < 0 else 1.0
    v = math.ceil(abs(x) - 1e-12)
    if odd:
        v = v if v % 2 == 1 else v + 1
    else:
        v = v if v % 2 == 0 else v + 1
    return s * v


def _fact(x):
    n = _int(x)
    if n < 0:
        raise NUM
    if n > 170:
        raise NUM
    return float(math.factorial(n))


def _combin(n, k):
    n, k = _int(n), _int(k)
    if n < 0 or k < 0 or k > n:
        raise NUM
    return float(math.comb(n, k))


def _gcd(*a):
    xs = [int(v) for v in _nums(a)]
    if any(v < 0 for v in xs):
        raise NUM
    g = 0
    for v in xs:
        g = math.gcd(g, v)
    return float(g)


def _lcm(*a):
    xs = [int(v) for v in _nums(a)]
    if any(v < 0 for v in xs):
        raise NUM
    out = 1
    for v in xs:
        if v == 0:
            return 0.0
        out = out * v // math.gcd(out, v)
    return float(out)


def _mround(x, m):
    x, m = num(scalar(x)), num(scalar(m))
    if m == 0:
        return 0.0
    if (x > 0 > m) or (x < 0 < m):
        raise NUM
    q = math.floor(abs(x / m) + 0.5 + 1e-12)
    return math.copysign(q * abs(m), x)


def _quotient(a, b):
    a, b = num(scalar(a)), num(scalar(b))
    if b == 0:
        raise DIV0
    return float(math.trunc(a / b))


def _kth(r, k, largest):
    xs = sorted(_nums([r]), reverse=largest)
    k = _int(k)
    if k < 1 or k > len(xs):
        raise NUM
    return float(xs[k - 1])


def _char(n):
    n = _int(n)
    if n < 1 or n > 1114111:
        raise VALUE
    return chr(n)


def _code(s):
    s = text(scalar(s))
    if not s:
        raise VALUE
    return float(ord(s[0]))


def _n(v):
    v = scalar(v)
    if isinstance(v, XLError):
        raise v
    if isinstance(v, bool):
        return 1.0 if v else 0.0
    return float(v) if isinstance(v, (int, float)) else 0.0


def _t(v):
    v = scalar(v)
    if isinstance(v, XLError):
        raise v
    return v if isinstance(v, str) else ""


def _var(xs, sample):
    if len(xs) < (2 if sample else 1):
        raise DIV0
    m = xsum(xs) / len(xs)
    return xsum((x - m) ** 2 for x in xs) / (len(xs) - (1 if sample else 0))


def _time_part(s, unit):
    secs = round((num(scalar(s)) % 1) * 86400)
    return float({"h": secs // 3600, "m": secs % 3600 // 60, "s": secs % 60}[unit])


def _time(h, m, s):
    t = num(scalar(h)) * 3600 + num(scalar(m)) * 60 + num(scalar(s))
    if t < 0:
        raise NUM
    return (t % 86400) / 86400


def _countunique(*a):
    seen = set()
    for v in _flat(a):
        if v is None or v == "":
            continue
        if isinstance(v, XLError):
            raise v
        seen.add(v.lower() if isinstance(v, str) else v)
    return float(len(seen))


def _mode(*a):
    xs = _nums(a)
    best, count = None, 1
    for x in xs:
        c = xs.count(x)
        if c > count:
            best, count = x, c
    if best is None:
        raise NA
    return float(best)


def _rank_fn(x, r, order=0):
    x = num(scalar(x))
    xs = _nums([r])
    if x not in xs:
        raise NA
    asc = order is not None and num(scalar(order)) != 0
    return float(1 + sum(1 for v in xs if (v < x if asc else v > x)))


def _averageifs(avg_rng, *rest):
    mask = _ifs_mask(list(zip(rest[0::2], rest[1::2])))
    vals = _cells(avg_rng)
    if len(vals) != len(mask):
        raise VALUE
    return _avg([v for v, k in zip(vals, mask) if k and isinstance(v, (int, float))
                 and not isinstance(v, bool)])


def _percentile(r, p):
    xs = sorted(_nums([r]))
    p = num(scalar(p))
    if not xs or p < 0 or p > 1:
        raise NUM
    h = (len(xs) - 1) * p
    lo = math.floor(h)
    return xs[lo] + (h - lo) * (xs[min(lo + 1, len(xs) - 1)] - xs[lo])


FUNCS.update({
    "TRUNC": _trunc,
    "EVEN": lambda x: _even_odd(x, False),
    "ODD": lambda x: _even_odd(x, True),
    "FACT": _fact,
    "COMBIN": _combin,
    "GCD": _gcd,
    "LCM": _lcm,
    "MROUND": _mround,
    "QUOTIENT": _quotient,
    "SUMSQ": lambda *a: xsum(x * x for x in _nums(a)),
    "LARGE": lambda r, k: _kth(r, k, True),
    "SMALL": lambda r, k: _kth(r, k, False),
    "EXACT": lambda a, b: text(scalar(a)) == text(scalar(b)),
    "CHAR": _char,
    "CODE": _code,
    "CLEAN": lambda s: "".join(ch for ch in text(scalar(s)) if ord(ch) >= 32),
    "N": _n,
    "T": _t,
    "ISODD": lambda x: _int(x) % 2 == 1,
    "ISEVEN": lambda x: _int(x) % 2 == 0,
    "ISLOGICAL": lambda x: isinstance(scalar(x), bool),
    "HOUR": lambda s: _time_part(s, "h"),
    "MINUTE": lambda s: _time_part(s, "m"),
    "SECOND": lambda s: _time_part(s, "s"),
    "TIME": _time,
    "STDEV.P": lambda *a: math.sqrt(_var(_nums(a), False)),
    "STDEVP": lambda *a: math.sqrt(_var(_nums(a), False)),
    "VAR.S": lambda *a: _var(_nums(a), True),
    "VAR.P": lambda *a: _var(_nums(a), False),
    "VARP": lambda *a: _var(_nums(a), False),
    "COUNTUNIQUE": _countunique,
    "MODE": _mode,
    "RANK": _rank_fn,
    "AVERAGEIFS": _averageifs,
    "PERCENTILE": _percentile,
})


# aggregates that skip text / booleans inside *references* (=SUM(A1) with A1="x" is 0) but
# coerce literal arguments (=SUM("x") is #VALUE!): a single-cell reference is passed as a 1x1 Range
REF_AGG = {"SUM", "PRODUCT", "AVERAGE", "MEDIAN", "MIN", "MAX", "STDEV", "STDEV.S", "VAR",
           "SUMSQ", "STDEV.P", "STDEVP", "VAR.S", "VAR.P", "VARP", "MODE", "GCD", "LCM"}

# functions that receive an erroring argument as an error *value* instead of failing
# (=COUNT(1/0, 5) is 1, =COUNTA(1/0) is 1)
ERROR_TOLERANT = {"COUNT", "COUNTA"}

# functions whose arguments must be evaluated lazily (short-circuit)
LAZY = {"IF", "IFERROR", "IFNA", "IFS", "SWITCH", "CHOOSE", "ISERROR", "ISNA"}


def call_lazy(name: str, thunks: List[Callable[[], Any]]):
    """IF & friends receive their arguments as thunks (zero-arg functions)."""
    if not arity_ok(name, len(thunks)):
        raise NA
    if name == "IF":
        cond = truthy(scalar(thunks[0]()))
        if cond:
            return thunks[1]()
        return thunks[2]() if len(thunks) > 2 else False
    if name in ("IFERROR", "IFNA"):
        try:
            v = thunks[0]()
            v = scalar(v) if isinstance(v, Range) and v.shape == (1, 1) else v
            if isinstance(v, XLError):
                raise v
            return v
        except XLError as e:
            if name == "IFNA" and e.code != "#N/A":
                raise
            return thunks[1]()
    if name in ("ISERROR", "ISNA"):
        try:
            v = scalar(thunks[0]())
            if isinstance(v, XLError):
                raise v
            return False
        except XLError as e:
            return True if name == "ISERROR" else e.code == "#N/A"
    if name == "IFS":
        for c, v in zip(thunks[0::2], thunks[1::2]):
            if truthy(scalar(c())):
                return v()
        raise NA
    if name == "SWITCH":
        key = scalar(thunks[0]())
        rest = thunks[1:]
        for c, v in zip(rest[0::2], rest[1::2]):
            if compare("=", key, c()):
                return v()
        if len(rest) % 2 == 1:
            return rest[-1]()
        raise NA
    if name == "CHOOSE":
        k = int(num(scalar(thunks[0]())))
        if not 1 <= k < len(thunks):
            raise VALUE
        return thunks[k]()
    raise NAME


def call(name: str, args: list):
    f = FUNCS.get(name)
    if f is None:
        raise NAME
    if not arity_ok(name, len(args)):
        raise NA
    try:
        r = f(*args)
    except XLError:
        raise
    except (TypeError, IndexError):
        raise VALUE
    except (ValueError, OverflowError):
        raise NUM
    except ZeroDivisionError:
        raise DIV0
    return r


def finish(v):
    """Normalise a final cell value: unwrap 1×1 ranges, ints -> floats, NaN -> #NUM!."""
    if isinstance(v, Range):
        v = v.rows[0][0] if v.shape == (1, 1) else VALUE
    if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
        return NUM
    if isinstance(v, int) and not isinstance(v, bool):
        return float(v)
    return v


def display(v) -> str:
    if v is None:
        return ""
    if isinstance(v, float):
        if v.is_integer() and abs(v) < 1e15:
            return str(int(v))
        return f"{v:.6g}" if abs(v) < 1e-3 or abs(v) >= 1e9 else f"{round(v, 6)}"
    return text(v) if not isinstance(v, XLError) else v.code
