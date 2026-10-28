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
1. **74 unit tests** cover every phase (`tests/`).
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
`--target Summary!B6` compiles only the backward slice needed for that one output: 34 of the 57 formulas.

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
