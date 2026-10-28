# SheetGuard architecture

```
                         ┌───────────────────────── front ends ─────────────────────────┐
  Google Sheet ──► Code.gs (Apps Script) ─┐                                               │
  Browser ───────► web/app.js ────────────┼──► HTTP JSON API (server.py → api.py)         │
  Terminal ──────► python -m sheetguard ──┘                                               │
                         └───────────────────────────────────────────────────────────────┘
                                              │
                                   Workbook model (workbook.py)
                                              │
 ┌────────────────────────────── compiler pipeline (pipeline.py) ───────────────────────────────┐
 │ 1 Lexer        lexer.py      chars → tokens (1-char lookahead, positions for caret errors)  │
 │ 2 Parser       parser.py     tokens → AST (recursive descent, LL(1)), phrase-level recovery │
 │ 3 Symbols      symbols.py    nested scopes, named ranges, function signatures, link map     │
 │ 4 IR           graph.py      dependency DAG · Tarjan SCC (cycles) · Kahn levels · BFS impact│
 │ 5 Semantics    typecheck.py  type lattice + unit algebra + dimension checks                 │
 │ 6 Smells       smells.py     R1C1 canonical form · off-by-one · magic numbers · double count│
 │ 7 Optimizer    optimizer.py  folding · identities · dead branches · dead cells · slicing    │
 │   Fixes        fixes.py      merge findings → fix plan → corrected / annotated workbook     │
 └──────────────────────────────────────────────────────────────────────────────────────────────┘
                                              │
 ┌───────────────────────────────────── back ends ──────────────────────────────────────────────┐
 │ evaluator.py  AST interpreter (topological order, incremental recalc) ┐                       │
 │ codegen.py    straight-line Python, type specialization + deopt, CSE  ├─ runtime.py (shared   │
 │ vectorize.py  formula columns → NumPy, loop-carried-dependence check  ┘   value semantics)    │
 │ multitarget.py JavaScript + SQL (CTE per topological level), verified by execution           │
 │ tac.py        three-address code · basic blocks · DAG/value numbering · register-machine asm  │
 │ grammar.py    formal grammar · FIRST/FOLLOW · LL(1) table · SLR(1) automaton (parse traces)  │
 │ diff.py       semantic AST diff of two workbook versions · fuzz.py type-guided fuzzing        │
 │ summary.py    plain-English summary · before/after impact · health score                     │
 └──────────────────────────────────────────────────────────────────────────────────────────────┘
```

## Key data structures

| Structure | Where | Notes |
|---|---|---|
| `Token(type, value, pos, end)` | tokens.py | positions point into the original formula text |
| AST nodes | ast_nodes.py | `Number, String, Bool, ErrorLit, CellRef, RangeRef, NameRef, BinOp, UnaryOp, Percent, FuncCall, ArrayLit, Missing`; each has `children()`, `label()`, `to_dict()`, `unparse()` |
| `Workbook → Sheet → Cell` | workbook.py | sparse grid; cell = raw input + value + number format + AST |
| `SymbolTable` | symbols.py | `scopes: {<builtin>, <workbook>, sheet…}` and `links: {(from, to): count}` |
| `DependencyGraph` | graph.py | `preds` and `succs` adjacency sets keyed by `"Sheet!A1"` |
| `Ty`, `RangeTy` | typecheck.py | immutable lattice values; `join()` is the least upper bound |
| `Diagnostic` | diagnostics.py | code, severity, cell, message, explanation, fix, suggested formula |

## Complexity

| Phase | Cost |
|---|---|
| Lex + parse | O(n) in the formula length |
| Symbol resolution | O(total AST nodes) |
| DAG build | O(V + E), with ranges expanded over the non-empty cells they cover |
| Tarjan SCC, Kahn topological sort | O(V + E) |
| Type check | O(total AST nodes + range sizes) |
| Copy-paste detection | O(cells) per column or row run |
| Code generation | O(total AST nodes); emitted code runs in O(V + E) |

## Diagnostic codes

| Code | Meaning | Phase |
|---|---|---|
| SG001 / SG002 | lexical / syntax error (with repair) | lexer, parser |
| SG101 | unknown function (did you mean…) | symbols |
| SG102 | wrong number of arguments | symbols |
| SG103 | undefined name | symbols |
| SG104 | missing sheet (#REF!) | symbols |
| SG105 | reference to an empty cell | graph / smells |
| SG106 | function recognised but not executed (cached value used) | symbols |
| SG201 | circular reference | graph |
| SG301 | type mismatch | types |
| SG302 | unit mismatch ($ + %) | types |
| SG303 | dimension mismatch | types |
| SG304 | division by zero / empty | types |
| SG305 | number stored as text | types |
| SG401 | inconsistent (copy-paste) formula | smells |
| SG402 | off-by-one range | smells |
| SG403 | hard-coded constant | smells |
| SG404 | formula overwritten by a value | smells |
| SG405 | double counting | smells |
| SG501 | constant expression | optimizer |
| SG502 | dead cell | optimizer |
| SG601 | latent runtime error (found by fuzzing, with witness input) | fuzz |

## The 14 planted bugs in the demo workbook

See the docstring of `sheetguard/demo.py`. Each bug exercises a different phase, and `tests/test_semantics.py::DemoEndToEndTests` asserts that every one is found and that nothing else is flagged.
