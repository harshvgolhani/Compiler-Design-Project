# SheetGuard: Review Demo Script (about 6 minutes, 3 presenters)

**H** = Harsh Vardhan Golhani · **U** = Utkarsh Raj · **S** = Shrijay Sinha

The slides cover the *idea*. This script covers the *live demo* that follows them. Practise it twice with a timer.

---

## 30 minutes before the review: checklist

- [ ] Laptop charged. Browser zoom set to **110–125%** so the back row can read it. Notifications off.
- [ ] Double-click **`START_SheetGuard.bat`**. The web app opens at http://localhost:8765 with the demo loaded.
- [ ] Optional, for the Google Sheets wow moment: run **`START_TUNNEL.bat`** and paste the URL into the sidebar ⚙. Open the demo spreadsheet with the sidebar already authorised, then click **Clear highlights** so the first analysis is live.
- [ ] Open a terminal in the project folder (for the test-suite / CLI moment).
- [ ] Open the backup **`dist\SheetGuard_Demo.html`** in a second browser tab. It works with no Python and no internet.
- [ ] Open the **Benchmark** tab once so the numbers are warm, then go back to **Overview**.

> **Golden rule:** if anything misbehaves, don't debug live. Say "let me show you the recorded run" and switch to the backup tab. It shows the same views.

---

## The demo

### 0:00 · H: the hook (Overview tab)
> "Spreadsheets are programs. They have expressions, references and functions, but no compiler. So we built one. This is a real quarterly sales report with bugs that are common in practice. SheetGuard ran six classical compiler phases over it in under 50 milliseconds."

**Point at** the pipeline strip (Lexer + Parser → … → Optimizer), then the health ring **F (16/100)**, then the summary paragraph.
> "Every sentence of this summary comes from a compiler analysis, not an AI guess. For example, the 'best region revenue' on this report shows **$67,100**, but the correct value is **$57,057**."

### 0:45 · U: front end (Playground tab, or press ▶ Guided demo and use →)
Type slowly: `=SUM(Sales!B2:B9)*(1+TaxRate)`
> "Our hand-written lexer turns this into tokens. Function, sheet prefix, cell, colon, named range: each has a token class. The recursive-descent parser builds this abstract syntax tree, with one parse function per precedence level."

Click the example `=LOG10(LOG10)`:
> "One character of lookahead decides it. LOG10 followed by a bracket is a function. Without the bracket it's a cell: column LOG, row 10."

Click `=SUM(A1:A3` (or use step 4 of the tour):
> "Syntax errors point at the exact column, just like gcc. Phrase-level error recovery then finds the smallest edit that makes the formula valid." Click the green repair chip.

### 1:45 · S: symbol table & DAG (Structure tab)
> "Names like TaxRate are resolved through nested scopes, so a sheet-level name shadows a workbook-level one. Cross-sheet references behave like external symbols across object files. Here is the link map."

**Point at** the DAG:
> "Every cell is a node and every reference is an edge. **Tarjan's algorithm** finds strongly connected components: the red pair is a circular reference between Bonus and Profit. **Kahn's topological sort** gives these levels, which is the minimal recalculation order. Each column could even run in parallel."

Click node **D6** (precedents turn blue, dependents orange):
> "Impact analysis: if D6 is wrong, these 19 cells are wrong."

### 2:45 · H: semantic analysis (Verification tab)
Open **Unit mismatch, Sales!I4**:
> "Our type checker infers a type for every cell: currency, percent, date, text. `G4 + H4` adds a percentage to a dollar amount. That's like adding metres to seconds. The fix is `G4*(1+H4)`."

Open **Inconsistent formula, Sales!D6**:
> "The smell detector converts formulas to **R1C1 canonical form**. All the copied formulas in this column become `RC[-2]*RC[-1]`. D6 is the odd one out: it points one row up."

Open **Dead cell, Sales!C6**:
> "The optimizer's dead-cell analysis finds that Central's unit price is never read. That independently proves the same copy-paste bug."

Click **Apply fix** on one issue (D6).

### 3:45 · U: deliverables (Deliverables tab)
Click **Apply all fixes**.
> "Ten fixes are applied automatically. The before-and-after card shows **F 16 → C 74**, and errors drop from 7 to 3. The three that remain, such as the circular reference, need a human decision, so we don't guess. This table re-runs the whole workbook to show what each bug actually cost: revenue was overstated by 4.1%, and the 'best region' figure by 15%."

**Optional: Google Sheets (30 s).** Switch to the spreadsheet → **🛡 SheetGuard → Analyze this sheet**. Flagged cells turn red or amber; hover one to show its note. Click a cell and the **Structure** tab follows the selection live. Then **Create corrected copy**.

### 4:45 · S: back end (Compiler output → Benchmark tabs)
> "The optimizer folds `12*25000*(1+10%)` into 330,000 at compile time. The code generator emits straight-line Python. Where the type checker proved both operands are numbers, it uses native float operations, and it falls back to the safe path if an error appears at runtime. A column of copied formulas is really a loop, so the vectorizer turns it into a single NumPy array operation."

**Point at** the benchmark:
> "Same 10,000-formula model: re-interpreting every formula takes about half a second. Compiled and vectorized, it takes **under a millisecond, roughly a thousand times faster**. On our laptop it measured 0.41 ms, about 1,460×. Every engine is verified cell-by-cell to produce identical values."

> Numbers vary with machine load (we've seen 700× to 1,500×). Read out whatever the screen shows.

### 5:30 · H: close
> "Every syllabus module, from lexical analysis to code generation, maps onto a working component, and 74 unit tests check them. Next we're targeting multi-target code generation and semantic diffing between workbook versions. Thank you."

---

## If you are asked to show "proof it's real"
- Terminal: `python -m unittest discover -s tests` → **74 tests, OK**.
- Terminal: `python -m sheetguard parse "=SUM(A1:A3"` shows the caret error. `python -m sheetguard repl` lets the examiner type any formula.
- Open `sheetguard/parser.py` and show the grammar in the docstring. Each rule is one Python function.
- Open `demo/Acme_Q3_Sales_FIXED.xlsx` in Excel. Fixed cells are green with comments, and the first sheet is the report.

## Key numbers (memorise)
| | |
|---|---|
| Demo workbook | 3 sheets, 126 cells, **57 formulas**, 4 named ranges, **14 planted bugs** |
| Findings | **7 errors, 9 warnings, 3 notes**; **10 auto-fixes**; health **16/F → 74/C** |
| Biggest impact | Best region revenue **$67,100 → $57,057 (−15%)**; total revenue overstated **4.1%** |
| Analysis time | ~**45 ms** for the whole workbook (all 7 phases) |
| Benchmark | 10,012 formulas: naive ~**500–600 ms** → NumPy ~**0.4–0.8 ms** (≈**700–1,500×**), all verified identical |
| Code | ~**6,900** lines of Python compiler + 1,600 lines UI/add-on, **74 tests**, 213 functions recognised / 86 implemented |
