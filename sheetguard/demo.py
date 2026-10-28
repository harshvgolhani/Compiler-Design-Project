"""The demo workbook: a realistic quarterly sales report with planted bugs.

Each bug exercises a different compiler phase, so the demo walks through
the whole pipeline:

  #  Where             Bug                                   Caught by
  -- ----------------  ------------------------------------  -------------------------
  1  Sales!D6          copy-paste error  =B6*C5              smell detector (R1C1 diff)
  2  Sales!F8          hard-coded tax rate  *0.18            smell detector (magic number)
  3  Sales!D9          formula overwritten by a typed value  smell detector
  4  Sales!B10         off-by-one  SUM(B2:B8) misses row 9   smell detector (range check)
  5  Sales!I4          currency + percentage  =G4+H4         type & unit checker
  6  Sales!C7          price stored as text "$45.00"          type checker (coercion)
  7  Sales!D11         AVERAGE over the total row too        smell detector (double count)
  8  Summary!B4/B5     bonus <-> profit loop                 dependency graph (Tarjan SCC)
  9  Summary!B7        typo  SUMM(...)                       symbol table (unknown function)
  10 Summary!B9        typo  TaxRte                          symbol table (undefined name)
  11 Summary!B8        divide by an empty cell               type checker / graph
  12 Summary!B10       12*25000*(1+10%)                      optimizer (constant folding)
  13 Inputs!B6         old FX rate nobody uses               optimizer (dead cell)
  14 Summary!B13       =SUM(Sales!D2:D9   (missing ')')       parser (syntax error + recovery)
  latent bugs that no static check can see, only the fuzzer (fuzz.py):
     Summary!B14       =Sales!F10/Sales!E10  #DIV/0! when Discount = 100 %
     Summary!B15       =B2/Sales!B10         #DIV/0! when no units are sold
"""
from __future__ import annotations

from .workbook import Workbook

REGIONS = ["North", "South", "East", "West", "Central", "Coastal", "Hills", "Islands"]
UNITS = [1200, 950, 1430, 800, 1100, 670, 540, 720]
PRICES = [45.0, 52.5, 39.9, 61.0, 47.25, "$45.00", 58.0, 44.5]
GROWTH = [0.12, 0.08, 0.15, -0.03, 0.05, 0.10, 0.02, 0.07]

USD = '"$"#,##0.00'
PCT = "0.0%"


def build_demo_workbook() -> Workbook:
    wb = Workbook("Acme Retail — Q3 Sales Report")
    fmts = {}

    # ---------------------------------------------------------------- Inputs
    inp = wb.add_sheet("Inputs")
    rows = [("Parameter", "Value"), ("Tax Rate", 0.18), ("Discount", 0.05),
            ("Commission Rate", 0.10), ("USD→INR", 83.2), ("Old FX rate (2024)", 82.1)]
    for r, (a, b) in enumerate(rows, start=1):
        inp.set(1, r, a)
        inp.set(2, r, b, fmt=PCT if r in (2, 3, 4) else "")
    wb.define_name("TaxRate", "Inputs!$B$2")
    wb.define_name("Discount", "Inputs!$B$3")
    wb.define_name("CommissionRate", "Inputs!$B$4")
    wb.define_name("FX", "Inputs!$B$5")

    # ---------------------------------------------------------------- Sales
    s = wb.add_sheet("Sales")
    headers = ["Region", "Units", "Unit Price", "Revenue", "Discounted", "Tax",
               "Total incl. Tax", "Growth %", "Projected Q4"]
    for c, h in enumerate(headers, start=1):
        s.set(c, 1, h)
    for i, region in enumerate(REGIONS):
        r = i + 2
        s.set(1, r, region)
        s.set(2, r, UNITS[i])
        s.set(3, r, PRICES[i], fmt=USD)
        s.set(4, r, f"=B{r}*C{r}", fmt=USD)
        s.set(5, r, f"=D{r}*(1-Discount)", fmt=USD)
        s.set(6, r, f"=E{r}*TaxRate", fmt=USD)
        s.set(7, r, f"=E{r}+F{r}", fmt=USD)
        s.set(8, r, GROWTH[i], fmt=PCT)
        s.set(9, r, f"=G{r}*(1+H{r})", fmt=USD)

    # planted bugs ------------------------------------------------------
    s.set(4, 6, "=B6*C5", fmt=USD)            # 1 copy-paste error (row drift)
    s.set(6, 8, "=E8*0.18", fmt=USD)          # 2 hard-coded tax rate
    s.set(4, 9, 32040, fmt=USD)               # 3 formula overwritten by a constant
    s.set(9, 4, "=G4+H4", fmt=USD)            # 5 currency + percentage
    # 6: C7 already holds the text "$45.00" (see PRICES)

    s.set(1, 10, "TOTAL")
    s.set(2, 10, "=SUM(B2:B8)")               # 4 off-by-one: misses row 9
    for c, col in ((4, "D"), (5, "E"), (6, "F"), (7, "G"), (9, "I")):
        s.set(c, 10, f"=SUM({col}2:{col}9)", fmt=USD)
    s.set(1, 11, "Average revenue")
    s.set(4, 11, "=AVERAGE(D2:D10)", fmt=USD)  # 7 includes the TOTAL row

    # ---------------------------------------------------------------- Summary
    m = wb.add_sheet("Summary")
    summary = [
        ("Metric", "Value"),
        ("Total revenue (incl. tax)", "=Sales!G10"),
        ("Commission", "=B2*CommissionRate"),
        ("Bonus pool (10% of profit)", "=B5*10%"),          # 8 cycle  B4 -> B5 -> B4
        ("Profit after bonus", "=B2-B3-B4"),
        ("Revenue in INR", "=B2*FX"),
        ("Total tax collected", "=SUMM(Sales!F2:F9)"),      # 9 unknown function
        ("Margin per unit", "=B5/B12"),                     # 11 B12 is empty
        ("Tax payable (check)", "=Sales!E10*TaxRte"),       # 10 undefined name
        ("Annual target", "=12*25000*(1+10%)"),             # 12 constant folding
        ("Best region revenue", "=MAX(Sales!D2:D9)"),
        (None, None),
        ("Revenue check", "=SUM(Sales!D2:D9"),             # 14 syntax error (missing ')')
        ("Effective tax rate", "=Sales!F10/Sales!E10"),      # latent: #DIV/0! if Discount = 100%
        ("Revenue per unit", "=B2/Sales!B10"),               # latent: #DIV/0! if no units sold
    ]
    for r, (a, b) in enumerate(summary, start=1):
        if a is None:
            continue
        m.set(1, r, a)
        m.set(2, r, b, fmt=('"₹"#,##0' if r == 6 else PCT if r == 14 else USD) if r > 1 else "")
    return wb


DEMO_SPECS = __doc__
