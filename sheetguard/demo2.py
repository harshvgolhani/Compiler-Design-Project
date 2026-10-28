"""Demo 2: a semester grade book with planted bugs.

A second, independent test case. It is a different domain from demo 1 (the Acme
sales report) and exercises the checks demo 1 does not reach: wrong argument
counts, references to a missing sheet, dimension mismatches in VLOOKUP and
SUMPRODUCT, a self-referencing range, an empty-cell read and a lexical error.

  #  Where             Bug                                          Caught by
  -- ----------------  -------------------------------------------  -----------------------------
  1  Marks!I5          copy-paste drift  ...D4*W_CAT2 (row 4)        smell detector (R1C1 diff)
  2  Marks!I8          hard-coded FAT weight  F8*0.4                 smell detector (magic number)
  3  Marks!I10         weighted total overwritten by a typed 71.5   smell detector
  4  Marks!F6          FAT mark stored as text "78"                 type checker (coercion)
  5  Marks!J7          VLOOKUP column 3 of a 2-column grade table   type checker (dimensions)
  6  Marks!K9          attendance limit hard-coded as 0.75          smell detector (magic number)
  7  Marks!C12         CAT-1 average misses the last student        smell detector (off-by-one)
  8  Marks!I12         class average includes itself  I2:I12       dependency graph (cycle)
  9  Report!B7         sheet name typo  Mark!I2:I11                 symbol table (missing sheet)
  10 Report!B8         VLOOKUP() without the column index           symbol table (arity)
  11 Report!B9         SUMPRODUCT of 10x1 and 5x1 ranges            type checker (dimensions)
  12 Report!B10        missing closing quote  "S)                   lexer (error recovery)
  13 Report!B11        name typo  MinAttendence                     symbol table (undefined name)
  14 Report!B12        typo  STDEVV(...)                            symbol table (unknown function)
  15 Report!B13        adds the empty moderation cell Weights!B12   type checker / graph
  16 Report!B14        100*(15%+15%+10%+40%+20%)                    optimizer (constant folding)
  17 Weights!B11       old FAT weight nobody uses                   optimizer (dead cell)
  latent bug only the fuzzer can see (fuzz.py):
     Report!B15        SUMIF(...)/B3   #DIV/0! when nobody passes
"""
from __future__ import annotations

from .workbook import Workbook

STUDENTS = [  # reg no, name, CAT-1, CAT-2, DA, FAT, Lab, attendance  (all marks out of 100)
    ("26BCE1001", "Aarav Menon", 78, 82, 90, 74, 88, 0.92),
    ("26BCE1002", "Diya Raman", 91, 88, 95, 86, 94, 0.97),
    ("26BCE1003", "Kabir Sethi", 55, 61, 70, 48, 72, 0.81),
    ("26BCE1004", "Meera Iyer", 66, 59, 80, 63, 77, 0.88),
    ("26BCE1005", "Rohan Das", 42, 50, 65, "78", 60, 0.71),
    ("26BCE1006", "Sana Qureshi", 84, 90, 88, 81, 90, 0.95),
    ("26BCE1007", "Tanvi Joshi", 38, 45, 55, 41, 58, 0.69),
    ("26BCE1008", "Vikram Nair", 72, 70, 85, 69, 80, 0.86),
    ("26BCE1009", "Ishaan Roy", 60, 64, 75, 58, 70, 0.78),
    ("26BCE1010", "Nikita Shah", 95, 93, 98, 92, 96, 0.99),
]
GRADES = [(0, "F"), (50, "E"), (55, "D"), (60, "C"), (70, "B"), (80, "A"), (90, "S")]
PCT = "0%"
ONE = "0.0"


def total_formula(r: int) -> str:
    return (f"=ROUND(C{r}*W_CAT1+D{r}*W_CAT2+E{r}*W_DA+F{r}*W_FAT+G{r}*W_LAB,1)")


def build_demo2_workbook() -> Workbook:
    wb = Workbook("CSE2001 — Winter Semester Grade Book")

    # ---------------------------------------------------------------- Weights
    w = wb.add_sheet("Weights")
    rows = [("Component", "Weight"), ("CAT-1", 0.15), ("CAT-2", 0.15), ("Digital Assignments", 0.10),
            ("FAT", 0.40), ("Lab", 0.20), (None, None), ("Rule", "Value"), ("Pass mark", 50),
            ("Minimum attendance", 0.75), ("Old FAT weight (2025)", 0.50), ("Moderation marks", None)]
    for r, (a, b) in enumerate(rows, start=1):
        if a is not None:
            w.set(1, r, a)
        if b is not None:
            w.set(2, r, b, fmt=PCT if isinstance(b, float) else "")
    w.set(1, 14, "Min score")
    w.set(2, 14, "Grade")
    for i, (lo, g) in enumerate(GRADES):
        w.set(1, 15 + i, lo)
        w.set(2, 15 + i, g)
    for name, ref in (("W_CAT1", "$B$2"), ("W_CAT2", "$B$3"), ("W_DA", "$B$4"), ("W_FAT", "$B$5"),
                      ("W_LAB", "$B$6"), ("PassMark", "$B$9"), ("MinAttendance", "$B$10"),
                      ("GradeTable", "$A$15:$B$21")):
        wb.define_name(name, "Weights!" + ref)

    # ---------------------------------------------------------------- Marks
    m = wb.add_sheet("Marks")
    headers = ["Reg No", "Name", "CAT-1", "CAT-2", "DA", "FAT", "Lab", "Attendance",
               "Weighted Total", "Grade", "Result"]
    for c, h in enumerate(headers, start=1):
        m.set(c, 1, h)
    for i, (reg, name, c1, c2, da, fat, lab, att) in enumerate(STUDENTS):
        r = i + 2
        for c, v in enumerate((reg, name, c1, c2, da, fat, lab), start=1):
            m.set(c, r, v)
        m.set(8, r, att, fmt=PCT)
        m.set(9, r, total_formula(r), fmt=ONE)
        m.set(10, r, f"=VLOOKUP(I{r},GradeTable,2,TRUE)")
        m.set(11, r, f'=IF(AND(I{r}>=PassMark,H{r}>=MinAttendance),"PASS","FAIL")')

    # planted bugs ------------------------------------------------------
    m.set(9, 5, "=ROUND(C5*W_CAT1+D4*W_CAT2+E5*W_DA+F5*W_FAT+G5*W_LAB,1)", fmt=ONE)  # 1 drift
    m.set(9, 8, "=ROUND(C8*W_CAT1+D8*W_CAT2+E8*W_DA+F8*0.4+G8*W_LAB,1)", fmt=ONE)    # 2 magic number
    m.set(9, 10, 71.5, fmt=ONE)                                                     # 3 overwritten
    # 4: F6 already holds the text "78" (see STUDENTS)
    m.set(10, 7, "=VLOOKUP(I7,GradeTable,3,TRUE)")                                  # 5 column 3 of 2
    m.set(11, 9, '=IF(AND(I9>=PassMark,H9>=0.75),"PASS","FAIL")')                   # 6 magic number

    m.set(1, 12, "Class average")
    m.set(3, 12, "=AVERAGE(C2:C10)", fmt=ONE)                                       # 7 off-by-one
    for c, col in ((4, "D"), (5, "E"), (6, "F"), (7, "G")):
        m.set(c, 12, f"=AVERAGE({col}2:{col}11)", fmt=ONE)
    m.set(8, 12, "=AVERAGE(H2:H11)", fmt=PCT)
    m.set(9, 12, "=AVERAGE(I2:I12)", fmt=ONE)                                       # 8 includes itself

    # ---------------------------------------------------------------- Report
    rep = wb.add_sheet("Report")
    report = [
        ("Metric", "Value", ""),
        ("Students", "=COUNTA(Marks!B2:B11)", ""),
        ("Passed", '=COUNTIF(Marks!K2:K11,"PASS")', ""),
        ("Pass percentage", "=B3/B2", PCT),
        ("Highest weighted total", "=MAX(Marks!I2:I11)", ONE),
        ("Topper", "=INDEX(Marks!B2:B11,MATCH(B5,Marks!I2:I11,0))", ""),
        ("Class average", "=AVERAGE(Mark!I2:I11)", ONE),                         # 9 missing sheet
        ("Grade of the top score", "=VLOOKUP(B5,GradeTable)", ""),               # 10 arity
        ("Weighted CAT-1 check", "=SUMPRODUCT(Marks!C2:C11,Weights!B2:B6)", ONE),  # 11 shapes
        ("Students with grade S", '=COUNTIF(Marks!J2:J11,"S)', ""),            # 12 unclosed string
        ("Below attendance limit", '=COUNTIF(Marks!H2:H11,"<"&MinAttendence)', ""),  # 13 name typo
        ("Spread of totals (std. dev.)", "=STDEVV(Marks!I2:I11)", ONE),          # 14 unknown function
        ("Top score after moderation", "=B5+Weights!B12", ONE),                  # 15 empty cell
        ("Maximum possible score", "=100*(15%+15%+10%+40%+20%)", ONE),           # 16 constant folding
        ("Average total of passed students", '=SUMIF(Marks!K2:K11,"PASS",Marks!I2:I11)/B3', ONE),  # latent
    ]
    for r, (a, b, f) in enumerate(report, start=1):
        rep.set(1, r, a)
        rep.set(2, r, b, fmt=f)
    return wb


DEMO2_SPECS = __doc__
