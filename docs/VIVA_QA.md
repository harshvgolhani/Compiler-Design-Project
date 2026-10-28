# SheetGuard: Viva / Review Q&A

These are short, honest answers to the questions examiners usually ask. Each one points at the file that backs it up, so you can open the code if you're challenged.

---

## Big picture

**Q1. Why is this a *compiler* project and not just a linter?**
A linter matches patterns in text. SheetGuard runs the full front-end and back-end:
- a **lexer** produces tokens;
- a **recursive-descent parser** builds an AST;
- a **symbol table** resolves names across scopes;
- the **dependency DAG** is the intermediate representation (IR);
- **semantic analysis** covers types and units;
- **optimizations** run over the AST and the DAG;
- **code generation** emits executable Python or NumPy, and that code is run and benchmarked.

Every phase feeds the next one, through `pipeline.py`.

**Q2. What exactly is the "source language"?**
Spreadsheet formulas (the Excel / Google Sheets dialect). The *program* is the whole workbook: each formula cell is a statement that assigns to that cell, and constant cells are the inputs. The grammar is in the docstring of `parser.py`.

**Q3. Where does AI/LLM come in?**
Nowhere in the analysis. All of it is deterministic and covered by tests. The summary is template text filled from compiler features (`summary.py`). An LLM can *optionally* rephrase only the last sentence; it's off by default and needs `SHEETGUARD_LLM=1` plus an API key.

**Q4. How do you know your results are correct?**
1. **131 automated tests** cover every phase (`tests/`), including property-based tests on random formulas and differential tests that compare all five execution engines on random workbooks (Q59).
2. The interpreter's values were **cross-checked against LibreOffice**: we recalculated the same .xlsx there and the figures match exactly (test `test_demo_matches_libreoffice`).
3. The benchmark **verifies every engine cell-by-cell** against the interpreter: naive, interpreted, compiled, optimized and NumPy.

---

## Lexical analysis (`lexer.py`, `tokens.py`)

**Q5. Why hand-write the lexer instead of using regex or a generator?**
A hand-written scanner is a DFA written as code. It gives exact column positions for error messages, and it lets us use **one character of lookahead** for the ambiguous cases. Lexer generators also struggle with context such as `A:C`, which is a column range only where an operand is expected.

**Q6. Give an example of an ambiguity the lexer resolves.**
`LOG10` is a valid *cell* (column LOG, row 10), but `LOG10(` is a *function*. Likewise `Sales!` is a sheet prefix, `TRUE` is a boolean, and `TaxRate` is a defined name. The classification depends on the next character.

**Q7. What token classes are there?**
NUMBER, STRING, BOOL, ERROR, CELL, COL_RANGE, ROW_RANGE, SHEET, FUNC, NAME, OP, parentheses, comma, semicolon, colon, braces, EOF. See `tokens.py`.

---

## Syntax analysis (`parser.py`, `ast_nodes.py`)

**Q8. Why recursive descent?**
The formula grammar is small and expression-heavy. Recursive descent with **one function per precedence level** is easy to verify against the grammar, gives precise error messages, and makes recovery easy. After left recursion is removed (using `while` loops), the grammar is LL(1), so no backtracking is needed.

**Q9. How did you remove left recursion?**
`additive → additive '+' term` becomes `additive → term ('+' term)*`, which is a loop that builds a left-leaning `BinOp` tree. That keeps the operators **left-associative**.

**Q10. What is the precedence order?**
From lowest to highest: comparison < `&` < `+ -` < `* /` < `^` < unary `- +` < postfix `%` < primary.

**Q11. Any surprising rule?**
Yes. Unary minus binds tighter than `^`, so `=-2^2` is **4** in Excel and Sheets, not −4. Our grammar reproduces this (`unary` sits below `power`), and there's a test for it. `^` is also left-associative in Excel: `2^3^2 = 64`.

**Q12. AST vs parse tree?**
The AST drops syntax that carries no meaning: parentheses disappear and their grouping is encoded in the tree's shape. `unparse()` regenerates a formula with the *minimum* parentheses, which shows the AST keeps all the meaning.

**Q13. How does error recovery work?**
It's **phrase-level recovery**. At the error position, `repair()` tries the smallest local edits (insert `)`, delete the offending token, close a string, drop a dangling operator) and returns the first one that parses. For example, `=SUM(A1:A3` becomes `=SUM(A1:A3)`. Panic mode, which skips to a synchronising token, isn't needed because a formula is a single expression.

---

## Symbol table (`symbols.py`, `functions.py`)

**Q14. What scopes exist?**
Three nested scopes: built-in (function signatures) → workbook (sheets and workbook-level named ranges) → sheet (sheet-level names). Lookup goes innermost first, so sheet scope **shadows** workbook scope (tested).

**Q15. What's the "linking" analogy?**
`Rates!B2` is like an external symbol in another object file. The resolver checks the target "module" (the sheet) exists and records a link. The **link map** (Sales → Inputs, 15 references) is the equivalent of a linker map. A missing sheet is an "undefined reference", which shows up in the sheet as `#REF!`.

**Q16. What does the function table store?**
A signature for each function: arity (minimum and maximum, or variadic), parameter kinds, return kind (including "same unit as the arguments") and whether it is *volatile*. Calls are checked against declarations, like a C prototype. `SUMM(` gets "did you mean SUM?" using edit distance.

---

## Intermediate representation: the DAG (`graph.py`)

**Q17. Why a DAG and not just ASTs?**
The ASTs are per cell. Cross-cell analysis (cycles, recalculation order, impact) needs the **whole-program graph**. An edge P → D means "D reads P". Ranges expand to the non-empty cells they cover.

**Q18. Explain the cycle detection.**
**Tarjan's strongly-connected-components algorithm**, which runs in O(V+E). We wrote an iterative version so deep sheets can't overflow Python's recursion limit. Any SCC with more than one node, or a node with a self-loop, is a circular reference. Cells *downstream* of a cycle are marked *blocked*.

**Q19. Explain the recalculation order.**
**Kahn's topological sort**, processed **level by level**. Each level contains cells whose inputs are all ready, so each level could be computed in parallel. The demo has 8 levels.

**Q20. What is impact analysis?**
A forward BFS from a cell over the edges gives every cell that can change. Incremental recalculation uses it: editing one row's units recomputes **17 of 10,012** formulas.

---

## Semantic analysis (`typecheck.py`)

**Q21. Describe the type system.**
It's a lattice: `Number[unit]` (unit is plain, currency or percent), `Date`, `Text` (flagged *numeric-text* if it looks like a number), `Bool`, `Error`, `Empty`, `Range<T> r×c`, and `Any` as the top. `join()` computes the least upper bound, for example at the branches of an IF. Constants get their types from their value and number format, and formulas are typed in topological order.

**Q22. What are the unit rules?**
- currency ± currency = currency
- **currency ± percent = error**
- currency × percent = currency
- currency × currency = warning ("money squared")
- currency ÷ currency = plain ratio
- date − date = days
- date + date = error

It works like dimensional analysis in physics.

**Q23. What are dimension checks?**
They compare range shapes: VLOOKUP asking for column 5 of a 3-column table, INDEX past the end of a range, and SUMIFS / SUMPRODUCT ranges of different sizes.

**Q24. Why is "$45.00" as text a bug if the sheet computes fine?**
Arithmetic silently *coerces* it, but SUM, AVERAGE and COUNT **skip text without any error**, so totals come out wrong silently. That's an implicit-coercion warning, much like `-Wconversion` in C.

---

## Static analysis / smells (`smells.py`, `transforms.py`)

**Q25. How does copy-paste detection work?**
Formulas are converted to **R1C1 form**, which is relative to their own position. `=B6*C6` in row 6 and `=B7*C7` in row 7 both become `RC[-2]*RC[-1]`. In a run of copied formulas, the dominant form (at least 60% of the run) is the pattern, and any deviant is flagged. The fix is the dominant form **shifted** to that cell (`shift()` moves relative references and keeps `$` references fixed, exactly like paste). This is normalisation to a canonical form, similar to α-equivalence.

**Q26. How do you avoid false positives?**
- Totals rows are excluded: a formula whose range covers the run itself is an aggregate, not a member.
- Small runs (fewer than 3 cells) are ignored.
- Magic-number matches are a *warning* only when the literal is distinctive (0.18 matching TaxRate). A coincidental round number like 10% is just a note.
- A test checks there are **zero findings outside the planted bugs** on the demo workbook.

**Q27. Off-by-one and double counting?**
- **Off-by-one:** a SUM/AVERAGE range stops one cell short of a contiguous numeric block.
- **Double counting:** an aggregate's range contains a cell that is itself an aggregate over the same range, such as an AVERAGE that includes the TOTAL row.

---

## Optimization (`optimizer.py`)

**Q28. Which optimizations, and are they safe?**
- **Constant folding**, plus **algebraic identities** (`x*1`, `x+0`, `x/1`). Identities are applied only when the operand is *proven numeric*, because `"abc"*1` must stay `#VALUE!`.
- **Dead-branch elimination** (`IF(TRUE,…)`).
- **Constant propagation** of named constants.

**Volatile** functions such as RAND, NOW and TODAY are never folded, and an expression that would fold to an error is left alone so the error still appears at runtime.

**Q29. What is dead-cell elimination here?**
It finds numeric inputs that no formula reads, in columns where the neighbouring inputs *are* read. It also traces the root cause: Central's price C6 is dead *because* D6 reads C5. So the optimizer independently confirms the copy-paste bug.

**Q30. Program slicing?**
`--target Summary!B6` compiles only the backward slice needed for that one output: 34 of the 59 formulas.

---

## Code generation (`codegen.py`, `vectorize.py`)

**Q31. What does the code generator emit, and why Python?**
It emits straight-line Python: one assignment per cell, in topological order, held in local variables. That means no dictionary lookups and no tree walking at runtime. Python can be compiled and executed in-process, inspected and benchmarked. The same back-end design could emit JavaScript or SQL, which is in our future work.

**Q32. What is type-directed specialization with deoptimization?**
Where the type checker *proved* both operands are numbers, `B2*C2` compiles to a native float multiply. That's a speculative fast path. If a value turns out to be an error or blank at runtime, the cell falls back to the always-correct helper `mul()`. This is the same idea JIT compilers use (speculate, guard, deoptimize).

**Q33. Common subexpression elimination?**
The canonical code string of every subexpression is counted across the whole workbook. `(1 - Discount)` occurs 8 times, so it's computed once into a temporary and reused 7 times.

**Q34. Explain the vectorizer.**
A column of copied formulas is a **loop**: `for r: D[r] = B[r]*C[r]`. If there's no **loop-carried dependence** (no cell reads its own column, as a running total does), the loop becomes a single NumPy array operation. Aggregates over vectors become `np.sum` and similar. If any input is non-numeric or a result is not finite, it **deoptimizes** to the scalar code.

**Q35. Isn't the ~1,000× speedup unfair?**
The baseline re-parses every formula on every recalculation, which is what a pure interpreter without a compile step does. Even against our own cached-AST interpreter, compiled code is about **60× faster**, and vectorized code is about **400× faster**. All engines are checked to give identical values.

---

## Engineering & deployment

**Q36. How does the Google Sheets add-on work?**
It's a **container-bound** Apps Script, attached to one spreadsheet and never published to the Marketplace. `Code.gs` reads the formulas, values, number formats and named ranges, and sends JSON with `UrlFetchApp` to the Python backend. The sidebar HTML renders the results and can highlight cells, jump to them, apply fixes and create a corrected copy. The backend is a zero-dependency standard-library HTTP server with an optional `X-SheetGuard-Key` header.

**Q37. One UI code base for web and Sheets?**
Yes. `web/app.js` runs in three shells (web, sidebar and graph dialog). `tools/build_addon.py` inlines it into the Apps Script HTML files. Only the transport changes: `fetch()` on the web, `google.script.run` in Sheets.

**Q38. Limitations (be honest)?**
- Dynamic references (`INDIRECT`, `OFFSET`) can't be resolved statically, so we flag them as volatile.
- Array or spill formulas and Google-only functions (QUERY, FILTER, ARRAYFORMULA …) are recognised but not evaluated.
- Locale-specific separators are only partly supported (`;` is accepted as an argument separator).
- Smell detection is heuristic, so it can still have false positives on unusual layouts.
- Very large sheets send a lot of JSON. The UI caps the graph at 900 nodes.

**Q39. Related work?**
- Spreadsheet-error research (Panko) reports that most large operational spreadsheets contain errors.
- Excel's own error checks (the green triangles) are local, per-cell pattern checks.
- Academic tools include unit/type checking for spreadsheets (e.g. Erwig and colleagues' UCheck) and statistical outlier detection of formulas (e.g. ExceLint).

Our contribution is putting the *whole* classical pipeline (front end, IR, semantic analysis, optimization *and* code generation) into one tool with explanations and fixes.

**Q40. What would you do next (for the final review)?**
- Multi-target code generation (JavaScript / SQL) from the same IR.
- AST-level semantic diffing of two workbook versions.
- Type-guided fuzz testing of formulas.
- Running the compiler in the browser with WebAssembly, so the add-on needs no backend.


---

## Syllabus lab (`grammar.py`, `tac.py`, the "Syllabus lab" tab)

**Q41. Is your grammar really LL(1)? Prove it.**
`grammar.py` builds the LL(1) form of the grammar (left recursion removed, one non-terminal per precedence level), computes FIRST and FOLLOW by fixed-point iteration, and fills the predictive table M[A, a]. A grammar is LL(1) exactly when no table cell gets two productions. Our table has 25 non-terminals, 31 terminals, 271 entries and **0 conflicts**. Run `python -m sheetguard grammar` to see it.

**Q42. How are FIRST and FOLLOW computed?**
- FIRST(X) for a non-terminal is the union of FIRST over its alternatives. For a sequence, take FIRST of the first symbol, and move on to the next symbol while ε is possible.
- FOLLOW(start) contains $. For A → αBβ, FOLLOW(B) ⊇ FIRST(β) − {ε}; if β can derive ε, then FOLLOW(B) ⊇ FOLLOW(A).

Both rules are repeated until nothing changes.

**Q43. How is the LL(1) table filled?**
For each production A → α:
- M[A, a] = A → α for every a in FIRST(α);
- if ε ∈ FIRST(α), also for every b in FOLLOW(A).

For example, A' → ε is placed under FOLLOW(A') = { &, comparison operators, ), ',', $ }.

**Q44. Explain the LL(1) parse trace.**
The stack starts as `$ E`.
- If the top of the stack is a terminal, it must match the lookahead token, and both are popped.
- If the top is a non-terminal A, it is replaced by the right-hand side from M[A, lookahead], pushed in reverse.
- An empty table cell is a syntax error.

The productions used, in order, form the **leftmost derivation**.

**Q45. Why also SLR(1)? What is different?**
LR parsers handle the natural left-recursive grammar directly (A → A + T keeps operators left-associative). We build:
- the augmented grammar E' → E;
- LR(0) items, CLOSURE and GOTO;
- the canonical collection of item sets (**79 states**);
- the ACTION/GOTO table, where shift entries come from GOTO on terminals and reduce A → α is placed on FOLLOW(A) (that is the "S" in SLR).

There are **0 shift-reduce and 0 reduce-reduce conflicts**. The tests check that the recursive-descent, LL(1) and SLR(1) parsers accept and reject exactly the same formulas.

**Q46. Why does R2 → cell | cell : cell not cause a conflict?**
In the item set after shifting `cell`, there is a shift on `:` and a reduce by R2 → cell on FOLLOW(R2). `:` is not in FOLLOW(R2), so SLR resolves it with no conflict.

**Q47. What three-address code do you generate? What are quadruples, triples and indirect triples?**
Each operator becomes `t = a op b`; IF becomes `ifFalse c goto L1 … goto L2 … L1: … L2:`; a function call becomes `param x … t = call f, n`.
- **Quadruples** store (op, arg1, arg2, result).
- **Triples** drop the result field: later instructions refer to earlier results by position, written (i).
- **Indirect triples** add a separate statement list that points into the triple table. The optimizer can then reorder statements without renumbering the triples.

**Q48. How do you find basic blocks?**
With the leaders algorithm. Leaders are the first instruction, every jump target and every instruction that follows a jump; a block runs from one leader to the next. The successor edges form the flow graph. An IF gives 4 blocks: the condition, then, else and join.

**Q49. What is the DAG of a basic block, and what does it optimize?**
Value numbering builds the DAG. Each new (op, vn1, vn2) gets a node, and a repeated one reuses the existing node. That gives:
- **common-subexpression elimination**, where one node has several attached names (t3, t7);
- **constant folding**, where a node with constant children becomes a constant leaf;
- **copy propagation**;
- **algebraic identities**.

Global **dead-code elimination** then removes temporaries that are never used. On the whole demo workbook: 134 → 123 instructions, 7 common subexpressions and 4 constant folds. The `(1 − Discount)` expression is computed once instead of 8 times.

**Q50. How do you know the TAC optimizer is correct?**
A small TAC interpreter (`run_tac`) executes the code before and after optimization. A property-based test generates **300 random formulas** (arithmetic, unary minus, %, nested IF) and checks that both versions give the same value on random inputs. While writing that test, it found a real bug: `^` could raise a Python OverflowError instead of returning #NUM!. That bug is fixed now.

**Q51. Explain the target code generator.**
It is the classic "simple code generator" for a load/store machine with *n* registers (`LD`, `ST`, `ADD`/`MUL`/…, `BRZ`, `BR`):
- **Register descriptors** record which variables each register holds.
- **Address descriptors** record where each variable's current value is.
- **getReg** prefers a register that already holds the operand, then an empty one, then one whose value is dead (from next-use information) or already safe in memory. Otherwise it **spills**.

With 2 registers, our example needs 2 spills; with 3 registers it needs none. Optimizing the TAC first cuts the machine code from 22 to 16 instructions.


---

## Extensions: future work, implemented (`multitarget.py`, `diff.py`, `fuzz.py`, "Extensions" tab)

**Q52. How does one IR give several targets?**
All back-ends consume the same optimized ASTs and the same topological order from the dependency DAG.
- **Python** (`codegen.py`) emits straight-line statements.
- **NumPy** vectorizes formula columns.
- **JavaScript** (`JSGenerator`) emits the same straight-line structure, plus a 60-line runtime that implements spreadsheet semantics. Errors are thrown objects, so `#DIV/0!` propagates.
- **SQL** (`SQLGenerator`) turns the whole workbook into one query.

**Q53. How can a workbook be one SQL query?**
Topological *levels* are the key. Every level becomes a common table expression that `SELECT *`s the previous one and adds one column per formula cell. A cell at level k only reads cells from levels < k, which are columns that already exist. Spreadsheet errors become NULL, which propagates through arithmetic just as an error does. `IFERROR(a,b)` becomes `COALESCE(a,b)`, and `/` becomes `CASE WHEN d = 0 THEN NULL ELSE … END`.

**Q54. How do you know the generated JS and SQL are right?**
We *execute* them: Node.js runs the JavaScript and Python's built-in sqlite3 runs the SQL. Every formula cell is then compared with the interpreter.
- JavaScript matches **59/59** cells on the buggy demo (errors included) and on the fixed one.
- SQL matches **all** cells on the fixed demo and on the synthetic model.
- On the buggy demo, every SQL difference traces back to the `"$45.00"` text cell. SQLite converts that text to 0 while spreadsheets read it as 45, and the type checker already flags it as SG305. This shows why implicit coercion is dangerous.

**Q55. What does the semantic diff do that a text diff can't?**
- It compares **ASTs**, so `=( A1+B1 )` vs `=A1+B1` is *cosmetic*.
- It aligns the two trees top-down and reports only the **minimal changed subtrees**, each with a meaning: range resized (+1 row), reference changed (+1 row), constant → reference, function renamed, operator changed.
- It detects **moved formulas** by matching R1C1 forms.
- It evaluates both versions to list the **ripple effect** (cells whose formula did not change but whose value did) and the cause.
- It compares diagnostics to report **issues resolved and introduced**, like a code review for spreadsheets.

**Q56. What is type-guided fuzz testing?**
It is dynamic analysis. Test inputs are generated from each input's *inferred type*:
- percentages: 0 %, 100 %, 99.9 %;
- money: 0, 1 cent, 10 million;
- quantities: 0, 1, 1 million;
- whole columns set to 0;
- adversarial cases: negative values, blanks, text.

Each case runs through the **compiled** code, which manages over 10,000 runs per second. A formula that was fine at baseline but now errors is reported with the smallest **witness** input and a suggested guard. Propagated errors are suppressed, so only root causes are shown.

**Q57. What did the fuzzer find in the demo?**
Two latent bugs that no static check can see, because the formulas are correct for today's data:
- `Effective tax rate = F10/E10` becomes `#DIV/0!` when Discount = 100 %.
- `Revenue per unit = B2/B10` becomes `#DIV/0!` when no units are sold.

It suggests `=IF(Sales!E10=0,0,Sales!F10/Sales!E10)`. Separately, it reports that 8 formulas break if someone types text into a number cell, which is a data-validation issue.

**Q58. Static vs dynamic analysis: which is better?**
They complement each other.
- **Static** analysis (type checker, smells) sees every path but must be conservative.
- **Dynamic** analysis (fuzzing) only sees the inputs it tries, but every bug it reports is real and comes with a reproducing input.

SheetGuard uses both, and the compiler is what makes the dynamic side cheap: 169 runs take about 15 ms.

---

## Testing & reliability (`tests/test_robustness.py`, `sheetguard/doctor.py`)

**Q59. How do you know your five execution engines compute the same thing?**
By **differential testing**. Thousands of random workbooks, both well-formed and garbage, go through the interpreter, the compiled Python (with and without the optimizer), the NumPy vectorizer, the JavaScript back-end (run in Node) and the SQL back-end (run in SQLite), and every formula cell is compared with the interpreter. Any difference is a bug in a back-end. The SQL back-end may differ only in documented ways (SQL has no error values and silently converts text), and those cases are labelled. The hardening pass ran about 10,000 random workbooks and found 0 unexplained differences. `tests/test_robustness.py` replays a deterministic sample on every test run.

**Q60. What real bugs did differential testing find?**
Several, each now covered by a regression test:
- A call with the wrong number of arguments (`=IF()`) crashed some back-ends. It now evaluates to `#N/A` in every engine, as in Google Sheets.
- `=SUM(A1)` with text in A1 must be 0 (references skip text), but `=SUM("x")` is `#VALUE!`. One engine got this wrong.
- `=COUNT(1/0, 5)` is 1, because COUNT ignores errors.
- Python's `**` returns a *complex* number for `(-8)^(1/3)`. The fast path now calls the runtime's `power`, which gives `#NUM!`.
- NumPy summed a column of TRUE/FALSE as 1/0, but spreadsheets skip booleans in SUM. Boolean columns now stay scalar.
- The SQL query flattener copied expressions into every later CTE, which grew exponentially on running totals. `OFFSET 0` now acts as an optimization fence.
- Numbers in text (`=0.1+0.2&""`) now use 15 significant digits ("0.3"), the same in Python and JavaScript.

**Q61. What happens with huge or hostile input?**
- Workbooks are limited to 250,000 cells, with a clear message beyond that.
- Formulas nested more than 100 levels deep (Excel allows 64) get a clean syntax error. Every CLI command and server request runs on a large-stack thread, so legal deep formulas never crash.
- Very long formulas (`=A1+A1+…`, 2,000 terms) compile, because the code generator hoists temporaries every 40 levels to stay within Python's nesting limit.
- A 10,000-cell dependency chain is analysed in about 2 s (it took 72 s before impact analysis was given a budget).
- Corrupt `.xlsx` files, `.xls` files, bad JSON, semicolon CSVs with decimal commas, dates, unicode or invalid sheet names, and multi-area named ranges all load or fail with a one-line explanation, never a traceback.
- The server answers 400 instead of 500 for bad input, refuses path traversal, and moves to the next free port if 8765 is busy.

**Q62. What is `python -m sheetguard doctor`?**
A ten-second installation check, also available as `CHECK_SETUP.bat`. It checks the Python version, optional packages (openpyxl, numpy), Node.js, SQLite, the UI and add-on files and the port. It then runs a live self-test: the demo analysis (7 errors, 9 warnings, 10 fixes), all five engines against the interpreter, the LL(1)/SLR(1) conflict check and the fuzzer.

**Q63. What about functions you don't implement, like FILTER or QUERY?**
They are still lexed, parsed, name-checked and placed in the dependency graph. They are only not executed. When the file carries the value the spreadsheet computed, every engine treats that cell as a constant with that value, so everything downstream stays correct, and an SG106 note explains this. 129 of the 213 catalogued functions are executed. 35 of them (TRUNC, LARGE, RANK, AVERAGEIFS, …) were added in the hardening pass.

**Q64. How do you know the three parsers and the code generators really agree?**
These checks were added in a second verification round:
- **Parsers.** On thousands of random token strings, the recursive-descent parser, the LL(1) table-driven parser and the SLR(1) shift-reduce parser accept and reject exactly the same formulas. Two checks are not context-free, so they stay in semantic analysis: array rows must have equal length, and both ends of `Sheet1!A1:Sheet1!B2` must name the same sheet. This round found and fixed two gaps in the grammars: omitted arguments `IF(A1,,0)` and array constants in the LR grammar.
- **Intermediate code.** The three-address code, before and after optimization, is executed (`run_tac`) and compared with the interpreter on random formulas.
- **Target code.** The register-machine assembly is run on a small machine simulator with 2, 3, 4 and 8 registers, which proves that spilling and the register/address descriptors are correct.
- **Bugs found in this round.** The TAC optimizer applied `t + 0 → t` when `t` was text or a boolean, which changes the result, so identities now need a provably numeric operand. Folded constants lost precision.
- **Auto-fixer.** It now validates its own output (translation validation): the corrected workbook is compiled again, and any fix that would create a new error is withheld with an explanation.
