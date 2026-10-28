# SheetGuard

**A compiler-based verification engine for spreadsheet formulas, delivered as a Google Sheets add-on.**

BCSE307P Compiler Design Lab · VIT Vellore · School of Computer Science and Engineering
Harsh Vardhan Golhani (24BAI0242) · Utkarsh Raj (24BCE2868) · Shrijay Sinha (24BCE2998)
Guide: Nithya K, Assistant Professor Senior Grade II

SheetGuard treats a spreadsheet as source code. It runs a real compiler pipeline over every formula and reports bugs with plain-English explanations and fixes.

```
formula text ─► Lexer ─► Parser ─► AST ─► Symbol table ─► Dependency DAG ─► Type checker ─► Smell detector ─► Optimizer ─► Code generator
               tokens   recursive         scopes/linking    cycles, topo sort
                        descent
```

## Quick start

**Windows:** right-click `SheetGuard.zip` → **Extract All** into an **empty** folder (never into an existing SheetGuard folder), open the `SheetGuard_v1.3` folder it creates, then double-click `START_SheetGuard.bat`. (First time? Double-click `CHECK_SETUP.bat` first. It checks Python, the optional packages and every engine in about 15 seconds.) The web app opens at http://localhost:8765. It has 10 views: Overview, Workbook, Structure (AST + DAG), Verification, Deliverables, Compiler output, Benchmark, Playground, **Syllabus lab** (FIRST/FOLLOW, the LL(1) table, the SLR(1) automaton, three-address code, basic blocks, DAGs and register-machine code) and **Extensions** (JavaScript and SQL back-ends, semantic diff, fuzz testing).

**No Python at all?** Double-click `dist/SheetGuard_Demo.html`. It is a self-contained replay of the compiler's real output on the demo workbook and works offline. Rebuild it with `python tools/build_static.py`.

**Presenting?** Click **▶ Guided demo** in the web app for a 9-step walkthrough of every compiler phase. Use ← and → to move between steps.

**Google Sheets add-on:** see [`docs/SETUP_GOOGLE_SHEETS.md`](docs/SETUP_GOOGLE_SHEETS.md). The sidebar files are in `addon/`.

**Command line (pure Python 3.8+, no installation needed):** double-click `RUN_DEMO.bat`, or run:

```bash
python -m sheetguard analyze -v          # analyze the built-in buggy demo workbook
python -m sheetguard lex   "=SUM(Sales!B2:B9)*(1+TaxRate)"
python -m sheetguard parse "=SUM(Sales!B2:B9)*(1+TaxRate)"
python -m sheetguard repl                # type formulas, see tokens + AST live
python -m sheetguard analyze -t         # ...plus the inferred type of every formula cell
python -m sheetguard fix                # auto-correct + write an annotated, corrected .xlsx
python -m sheetguard demo-files         # write the buggy demo workbook as .xlsx / .json
python -m sheetguard run                # evaluate the workbook with the interpreter
python -m sheetguard compile            # show the generated Python code (--numpy for NumPy)
python -m sheetguard bench --rows 2000  # naive vs interpreter vs compiled vs NumPy
python -m sheetguard serve              # web UI + JSON API (used by the Sheets add-on)
python -m sheetguard grammar            # BNF, FIRST/FOLLOW, LL(1) & SLR(1) conflict checks
python -m sheetguard ll1 "=1+2*3"       # table-driven LL(1) parse with the stack trace
python -m sheetguard slr "=1+2*3"       # SLR(1) shift-reduce parse with the stack trace
python -m sheetguard tac "=IF(A1>0,A1*2,0)" --regs 2   # TAC, triples, blocks, DAG opt, asm
python -m sheetguard js  --verify       # compile to JavaScript, run it with Node, compare every cell
python -m sheetguard sql --verify       # compile to ONE SQL query (CTE per DAG level), run in SQLite
python -m sheetguard diff [old new]     # semantic AST-level diff (demo: buggy vs auto-fixed)
python -m sheetguard fuzz [file]        # type-guided fuzz testing for latent runtime errors
python -m sheetguard analyze my.xlsx     # your own file (.xlsx needs: pip install openpyxl; .csv/.json need nothing)
python -m sheetguard doctor              # installation check + live self-test of all five engines
python -m unittest discover -s tests -v  # run the test-suite
```

## Benchmark (2,000-row model, 10,012 formulas)

| Engine | Time | Speed-up |
|---|---:|---:|
| naive: re-parse and interpret | ~510 ms | 1× |
| AST interpreter | ~150 ms | 3.4× |
| compiled Python | ~14 ms | 35× |
| + type specialization + CSE | ~2.8 ms | ~180× |
| + NumPy loop vectorization | ~0.35–0.7 ms | **~750–1,450×** |

Timings vary from run to run. Run `python -m sheetguard bench` on the demo laptop to get the numbers for your slides. All engines produce identical values. The benchmark verifies this on every cell.

## Project layout

| File | Compiler phase |
|---|---|
| `sheetguard/tokens.py`, `lexer.py` | **Lexical analysis**: hand-written scanner with 1-char lookahead |
| `sheetguard/parser.py`, `ast_nodes.py` | **Syntax analysis**: recursive-descent parser → AST (grammar in the docstring) |
| `sheetguard/symbols.py`, `functions.py` | **Symbol tables**: nested scopes, named ranges, cross-sheet linking, function signatures |
| `sheetguard/graph.py` | **IR**: whole-workbook dependency DAG; Tarjan SCC (cycles), Kahn topological sort (levels) |
| `sheetguard/typecheck.py` | **Semantic analysis**: type lattice (Number[currency/percent], Date, Text, Range<T>), unit algebra, dimension checks |
| `sheetguard/smells.py`, `transforms.py` | **Static analysis**: R1C1 canonical form finds copy-paste errors, off-by-one ranges, magic numbers, double counting |
| `sheetguard/fixes.py` | **Auto-correction**: merges duplicate findings, plans and applies fixes, exports an annotated .xlsx |
| `sheetguard/optimizer.py` | **Optimization**: constant folding, algebraic simplification, dead-branch & dead-cell elimination, program slicing |
| `sheetguard/runtime.py`, `evaluator.py` | **Execution**: spreadsheet value semantics, 129 executed functions (213 recognised), AST interpreter, incremental recalculation |
| `sheetguard/codegen.py` | **Code generation**: workbook → straight-line Python; type-directed specialization with deoptimization; CSE |
| `sheetguard/vectorize.py` | **Vectorization**: formula columns → NumPy array ops, with loop-carried-dependence check |
| `sheetguard/bench.py` | Benchmark of the 5 engines on a synthetic model |
| `sheetguard/summary.py` | **Deliverables**: plain-English summary from structural features, before/after bug-impact report, health score |
| `sheetguard/api.py`, `server.py` | JSON API + zero-dependency HTTP server (`/api/analyze`, `/api/parse`, `/api/export`, ...) |
| `web/` | Standalone web UI (one code base shared with the Sheets sidebar) |
| `addon/` | Google Apps Script add-on: `Code.gs`, `Sidebar.html`, `Graph.html` (built by `tools/build_addon.py`) |
| `sheetguard/grammar.py` | **Formal grammar**: FIRST/FOLLOW, LL(1) predictive table and table-driven parser; LR(0) items, SLR(1) ACTION/GOTO, shift-reduce parser |
| `sheetguard/tac.py` | **Intermediate code**: three-address code (quadruples, triples, indirect triples), basic blocks and flow graph, DAG/value numbering, local optimizations, simple code generator with register/address descriptors |
| `sheetguard/multitarget.py` | **More back-ends**: JavaScript (with a spreadsheet-semantics runtime) and SQL (one CTE per topological level), both verified by execution |
| `sheetguard/diff.py` | **Semantic diff**: AST alignment, minimal changed subtrees with meanings, moved formulas, ripple effect, issues resolved/introduced |
| `sheetguard/fuzz.py` | **Dynamic analysis**: type-guided fuzz testing on compiled code; latent errors with witnesses and guards (SG601) |
| `sheetguard/pipeline.py` | Compiler driver that runs all phases and builds the report |
| `sheetguard/demo.py` | Demo workbook with 14 planted, realistic bugs + 2 latent ones for the fuzzer (`demo/Acme_Q3_Sales_buggy.xlsx`) |
| `sheetguard/demo2.py` | Demo 2: a semester grade book with 17 more planted bugs (arity, missing sheet, VLOOKUP/SUMPRODUCT dimensions, self-reference, lexical error…) in `demo 2/` (buggy .xlsx/.json + SheetGuard-fixed .xlsx); regenerate with `python -m sheetguard demo-files --set 2` |
| `sheetguard/doctor.py`, `stack.py` | Installation self-test; large-stack worker threads for deeply nested formulas |
| `tests/` | 131 tests: every phase, plus differential tests that run all five engines on random workbooks |

## Status

- [x] Step 1: Lexer, parser, AST, error messages with caret positions
- [x] Step 2: Symbol table, dependency DAG, cycle detection, topological order, impact analysis
- [x] Step 3: Type & unit checker, smell detector, auto-fix + annotated .xlsx export
- [x] Step 4: Optimizer (folding, simplification, dead branches, dead cells, slicing), interpreter, Python code generator (type specialization + CSE), NumPy vectorizer, benchmark
- [x] Step 5: HTTP backend, standalone web UI, Google Sheets add-on (sidebar + graph dialog), plain-English summary and bug-impact report
- [x] Step 6: Guided demo tour, syntax-error recovery, before/after view, offline/hosted demo build, mobile layout
- [x] Step 7: Demo script, viva Q&A, architecture notes, slide screenshots
- [x] Step 8: Syllabus lab: LL(1) and SLR(1) parser construction with traces, TAC and its optimization, target code generation
- [x] Step 9: JavaScript/SQL code generation (verified by execution), semantic diff between workbook versions, type-guided fuzz testing
- [x] Step 10: Hardening. About 10,000 random workbooks were run through all five engines (0 unexplained differences); malformed/huge/deep inputs; Python 3.8–3.13; `doctor` self-test; 35 more functions

## Reliability

SheetGuard was hardened by differential testing. Random workbooks (garbage formulas, well-formed models and vectorizable column models) go through the AST interpreter, the compiled Python (with and without the optimizer), the NumPy vectorizer, the JavaScript back-end (run in Node.js) and the SQL back-end (run in SQLite). Every formula cell is compared with the interpreter. Each bug this found has a regression test in `tests/test_robustness.py`:

- wrong argument counts give `#N/A` in every engine;
- references skip text in SUM;
- COUNT ignores errors;
- spreadsheet-style number-to-text;
- complex-number and overflow guards in the fast path;
- boolean columns stay scalar in NumPy;
- an SQL optimization fence.

Bad input never produces a traceback:

- corrupt or old `.xls` files, bad JSON, semicolon CSVs with decimal commas;
- unicode or invalid sheet names, multi-area names;
- 100-level nesting, 2,000-term formulas, 10,000-cell chains and 250,000-cell workbooks.

Each of these loads, or fails with a one-line explanation. The test suite passes on Python 3.8 and 3.13, with or without numpy/openpyxl.

A second round cross-checked the components against each other:

- the three parsers (recursive descent, LL(1), SLR(1)) accept the same language;
- three-address code before and after optimization, and the register-machine code run on a simulator with 2–8 registers, compute what the interpreter computes;
- incremental recalculation equals a full recalculation;
- every auto-fix is re-compiled before it is offered;
- whole-column ranges stay fast even with a stray value at XFD1048576.

## Documentation

| File | What it is |
|---|---|
| [`docs/DEMO_SCRIPT.md`](docs/DEMO_SCRIPT.md) | 6-minute live demo: who says what and what to click, plus a pre-review checklist |
| [`docs/VIVA_QA.md`](docs/VIVA_QA.md) | 64 likely examiner questions with answers and file references |
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | Pipeline diagram, data structures, complexity, diagnostic codes |
| [`docs/SETUP_GOOGLE_SHEETS.md`](docs/SETUP_GOOGLE_SHEETS.md) | Installing the add-on in a spreadsheet (about 10 minutes) |
| `docs/screenshots/` | High-resolution screenshots for the slide deck |
