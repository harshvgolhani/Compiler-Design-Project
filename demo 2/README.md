# Demo 2: a semester grade book with planted bugs.

| File | What it is |
|---|---|
| `CSE2001_Grades_buggy.xlsx` | the workbook with the planted bugs (open in Excel, or File > Import in Google Sheets) |
| `CSE2001_Grades_buggy.json` | the same workbook in SheetGuard's JSON form (the format the Sheets add-on sends) |
| `CSE2001_Grades_FIXED.xlsx` | SheetGuard's auto-corrected copy: fixed cells are green with a comment, first sheet is the report |

Try it:

```
python -m sheetguard analyze "demo 2/CSE2001_Grades_buggy.xlsx"
python -m sheetguard fix     "demo 2/CSE2001_Grades_buggy.xlsx"
python -m sheetguard fuzz    "demo 2/CSE2001_Grades_buggy.json"
python -m sheetguard diff    "demo 2/CSE2001_Grades_buggy.xlsx" "demo 2/CSE2001_Grades_FIXED.xlsx"
```

or drag `CSE2001_Grades_buggy.xlsx` onto the web app at http://localhost:8765.

## Planted bugs

```
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
     Report!B15        SUMIF(...)/B3   #DIV/0! when nobody passes```
