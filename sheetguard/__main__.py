"""SheetGuard command-line interface.

    python -m sheetguard lex     "=SUM(A1:A3)*2"      token stream
    python -m sheetguard parse   "=SUM(A1:A3)*2"      AST tree (+ --dot for Graphviz)
    python -m sheetguard analyze [file]               full report (demo workbook if no file)
    python -m sheetguard graph   [file]               dependency DAG as Graphviz DOT
    python -m sheetguard fix     [file] -o out.xlsx   auto-correct + annotated .xlsx
    python -m sheetguard demo-files [--set 2]         write a demo set (buggy .xlsx/.json, fixed .xlsx)
    python -m sheetguard run     [file]               evaluate the workbook (interpreter)
    python -m sheetguard compile [file] [--numpy]     show the generated Python / NumPy code
    python -m sheetguard bench   [--rows N]           benchmark the 5 execution engines
    python -m sheetguard serve   [--port 8765]        web UI + API for the Google Sheets add-on
    python -m sheetguard grammar                      BNF, FIRST/FOLLOW, LL(1) & SLR(1) tables
    python -m sheetguard ll1     "=1+2*3"             table-driven LL(1) parse with stack trace
    python -m sheetguard slr     "=1+2*3"             SLR(1) shift-reduce parse with stack trace
    python -m sheetguard tac     "=IF(A1>0,A1*2,0)"   three-address code, blocks, optimization, asm
    python -m sheetguard js|sql  [file] [--verify]    compile the workbook to JavaScript / SQL
    python -m sheetguard diff    [old] [new]          semantic diff (demo: buggy vs auto-fixed)
    python -m sheetguard fuzz    [file]               type-guided fuzz testing for latent errors
    python -m sheetguard doctor                       check the installation, self-test all engines
    python -m sheetguard repl                         interactive formula compiler
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from . import __version__
from .demo import build_demo_workbook
from .errors import SheetGuardError
from .lexer import tokenize
from .parser import parse
from .pipeline import analyze
from .printer import ast_dot, ast_tree, token_table
from .workbook import Workbook, WorkbookLoadError


def _setup_console() -> None:
    if os.name == "nt":
        os.system("")                      # enables ANSI colours in Windows terminals
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass


_C = {"red": "\033[91m", "yellow": "\033[93m", "blue": "\033[94m", "green": "\033[92m",
      "dim": "\033[2m", "bold": "\033[1m", "end": "\033[0m"}


def c(text: str, color: str) -> str:
    if not sys.stdout.isatty() or os.environ.get("NO_COLOR"):
        return text
    return f"{_C[color]}{text}{_C['end']}"


def _load(path: str | None) -> Workbook:
    return Workbook.load(path) if path else build_demo_workbook()


def cmd_lex(args) -> int:
    try:
        print(token_table(tokenize(args.formula)))
    except SheetGuardError as e:
        print(c(e.pretty(), "red"))
        return 1
    return 0


def cmd_parse(args) -> int:
    try:
        tree = parse(args.formula)
    except SheetGuardError as e:
        print(c(e.pretty(), "red"))
        return 1
    if args.dot:
        print(ast_dot(tree))
    elif args.json:
        print(json.dumps(tree.to_dict(), indent=2))
    else:
        print(ast_tree(tree))
        print(c(f"\nre-generated: ={tree.unparse()}", "dim"))
    return 0


def cmd_analyze(args) -> int:
    wb = _load(args.file)
    rep = analyze(wb)
    if args.json:
        print(json.dumps(rep.to_dict(), indent=2, default=str))
        return 0
    st = wb.stats()
    print(c(f"SheetGuard {__version__} — {wb.name}", "bold"))
    print(f"{st['sheets']} sheets, {st['cells']} cells, {st['formulas']} formulas, "
          f"{st['names']} named ranges\n")

    print(c("Symbol table", "bold"))
    for s in rep.symbols.all_symbols():
        uses = f"{len(s.refs)} use(s)"
        print(f"  {s.kind:9} {s.name:22} {s.scope:12} {s.definition[:28]:28} {uses}")
    if rep.symbols.links:
        print(c("\nCross-sheet link map", "bold"))
        for (a, b), n in sorted(rep.symbols.links.items()):
            print(f"  {a} ──▶ {b}   ({n} reference(s))")

    g = rep.graph
    gs = g.stats()
    print(c("\nDependency graph", "bold"))
    print(f"  {gs['nodes']} nodes, {gs['edges']} edges, {gs['levels']} levels, "
          f"{gs['cycles']} cycle(s)")
    for cyc in g.cycles:
        print(c("  cycle: " + " → ".join(cyc + [cyc[0]]), "red"))
    print(c("\nRecalculation order (formula cells, by parallel level)", "bold"))
    for i, lvl in enumerate(g.formula_levels()):
        print(f"  L{i}: " + ", ".join(lvl))
    if g.blocked:
        print(c("  blocked by cycle: " + ", ".join(g.blocked), "red"))

    if rep.optimization and rep.optimization.changes:
        print(c("\nOptimizer", "bold"))
        for ch in rep.optimization.changes:
            print(f"  {ch.key:<14} {ch.before}  ⟶  {ch.after}")
            for p in ch.passes:
                print(c(f"                 · {p}", "dim"))

    if args.types:
        print(c("\nInferred types (formula cells)", "bold"))
        for cell in wb.formula_cells():
            print(f"  {cell.key:<14} {str(rep.types.get(cell.key, '?')):<22} {cell.raw}")

    cnt = rep.counts()
    print(c(f"\nDiagnostics: {cnt['error']} errors, {cnt['warning']} warnings, "
            f"{cnt['info']} notes   ({len(rep.fixes)} auto-fixable — run `fix`)", "bold"))
    colors = {"error": "red", "warning": "yellow", "info": "blue"}
    for d in rep.diagnostics:
        sev = c(f"{d.severity.upper():<8}", colors[d.severity])
        print(f"  {sev}{d.cell or '':<14} {d.code}  {d.message}")
        if args.verbose:
            if d.explanation:
                print(c("            why: " + d.explanation.replace("\n", "\n                 "),
                        "dim"))
            if d.fix:
                print(c("            fix: " + d.fix, "green"))
    print(c("\nPhase timings: " + ", ".join(f"{k} {v * 1000:.2f} ms"
                                           for k, v in rep.timings.items()), "dim"))
    return 0


def cmd_graph(args) -> int:
    rep = analyze(_load(args.file))
    print(rep.graph.to_dot())
    return 0


def cmd_fix(args) -> int:
    from .fixes import apply_fixes, export_xlsx
    wb = _load(args.file)
    rep = analyze(wb)
    print(c(f"{len(rep.fixes)} automatic fix(es) for {wb.name}:", "bold"))
    for f in rep.fixes:
        print(f"  {f.cell:<14} {str(f.before):<24} {c('→', 'green')} {f.after}   "
              + c(f"[{f.code} {f.title}]", "dim"))
    after = analyze(apply_fixes(wb, rep.fixes))
    b, a = rep.counts(), after.counts()
    print(c(f"\nbefore: {b['error']} errors, {b['warning']} warnings   →   after: "
            f"{a['error']} errors, {a['warning']} warnings", "bold"))
    if after.diagnostics:
        print(c("needs a human decision:", "yellow"))
        for d in after.diagnostics:
            print(f"  {d.cell:<14} {d.code}  {d.message}")
    out = args.output or (os.path.splitext(args.file)[0] if args.file else "demo") \
        + "_sheetguard.xlsx"
    try:
        export_xlsx(wb, out, rep.diagnostics, rep.fixes)
        print(c(f"\nannotated + corrected copy written to {out}", "green"))
    except RuntimeError as e:
        print(c(str(e), "yellow"))
    return 0


def cmd_demo_files(args) -> int:
    """Write a demo set: the buggy workbook (.xlsx + .json) and the SheetGuard-fixed copy.

    --set 1  Acme Q3 sales report      (default, into ./demo)
    --set 2  CSE2001 semester grade book (into "./demo 2")
    """
    from .fixes import apply_fixes, export_xlsx
    if args.set == 2:
        from .demo2 import build_demo2_workbook as build, DEMO2_SPECS as specs
        stem, folder = "CSE2001_Grades", args.dir or "demo 2"
    else:
        build, specs, stem, folder = build_demo_workbook, None, "Acme_Q3_Sales", args.dir or "demo"
    os.makedirs(folder, exist_ok=True)
    wb = build()
    j = os.path.join(folder, f"{stem}_buggy.json")
    with open(j, "w", encoding="utf-8") as fh:
        json.dump(wb.to_json(), fh, indent=1)
    print("wrote", j)
    try:
        print("wrote", export_xlsx(wb, os.path.join(folder, f"{stem}_buggy.xlsx")))
        if args.set == 2 or args.fixed:
            rep = analyze(wb)
            print("wrote", export_xlsx(wb, os.path.join(folder, f"{stem}_FIXED.xlsx"),
                                       rep.diagnostics, rep.fixes))
            after = analyze(apply_fixes(wb, rep.fixes)).counts()
            b = rep.counts()
            print(f"  {len(rep.fixes)} auto-fixes: {b['error']} errors, {b['warning']} warnings  ->  "
                  f"{after['error']} errors, {after['warning']} warnings")
    except RuntimeError as e:
        print(e)
    if specs:
        with open(os.path.join(folder, "README.md"), "w", encoding="utf-8") as fh:
            fh.write(_demo_readme(stem, specs))
    return 0


def _demo_readme(stem: str, specs: str) -> str:
    title, _, rest = specs.strip().partition("\n")
    return (f"# {title}\n\n"
            f"| File | What it is |\n|---|---|\n"
            f"| `{stem}_buggy.xlsx` | the workbook with the planted bugs (open in Excel, or File > Import in Google Sheets) |\n"
            f"| `{stem}_buggy.json` | the same workbook in SheetGuard's JSON form (the format the Sheets add-on sends) |\n"
            f"| `{stem}_FIXED.xlsx` | SheetGuard's auto-corrected copy: fixed cells are green with a comment, first sheet is the report |\n\n"
            f"Try it:\n\n```\npython -m sheetguard analyze \"demo 2/{stem}_buggy.xlsx\"\n"
            f"python -m sheetguard fix     \"demo 2/{stem}_buggy.xlsx\"\n"
            f"python -m sheetguard fuzz    \"demo 2/{stem}_buggy.json\"\n"
            f"python -m sheetguard diff    \"demo 2/{stem}_buggy.xlsx\" \"demo 2/{stem}_FIXED.xlsx\"\n```\n\n"
            f"or drag `{stem}_buggy.xlsx` onto the web app at http://localhost:8765.\n\n"
            f"## Planted bugs\n\n```{rest}```\n")


def cmd_run(args) -> int:
    from .evaluator import Interpreter
    from .refs import a1
    from .runtime import XLError, display
    wb = _load(args.file)
    rep = analyze(wb)
    vals = Interpreter(wb, rep.symbols, rep.graph).run()
    for sh in wb.sheets.values():
        print(c(f"\n── {sh.name} " + "─" * 60, "bold"))
        nrows, ncols = min(sh.max_row, args.rows), min(sh.max_col, 26)
        widths = [max([6] + [len(display(vals.get(f"{sh.name}!{a1(col, r)}")))
                             for r in range(1, nrows + 1)]) for col in range(1, ncols + 1)]
        for r in range(1, nrows + 1):
            cells = []
            for col in range(1, ncols + 1):
                v = vals.get(f"{sh.name}!{a1(col, r)}")
                s = display(v).ljust(min(widths[col - 1], 22))[:22]
                cells.append(c(s, "red") if isinstance(v, XLError) else s)
            print(f"{r:>3} " + " ".join(cells))
        if sh.max_row > nrows or sh.max_col > ncols:
            print(c(f"    … showing {nrows} of {sh.max_row} rows and {ncols} of {sh.max_col} "
                    f"columns (use --rows N)", "dim"))
    return 0


def cmd_compile(args) -> int:
    from .codegen import CodeGenerator
    wb = _load(args.file)
    rep = analyze(wb)
    asts = rep.optimization.asts if rep.optimization else None
    if args.numpy:
        from .vectorize import VectorCompiler, pretty_source
        vc = VectorCompiler(wb, rep.symbols, rep.graph, rep.types, asts)
        print(pretty_source(vc.generate()))
        print(c(f"# {dict(vc.stats)}", "dim"))
        return 0
    target = None
    if args.target:
        from .refs import parse_cell
        sheet, _, ref = args.target.rpartition("!")
        sheet = sheet.strip("'") or next(iter(wb.sheets), "")
        match = next((s for s in wb.sheets if s.lower() == sheet.lower()), None)
        try:
            col, row, _, _ = parse_cell(ref.replace("$", "").upper())
        except Exception:
            col = None
        cell = wb.sheets[match].get(col, row) if match and col else None
        if cell is None or not cell.is_formula:
            print(c(f"error: --target {args.target!r} is not a formula cell; use e.g. "
                    f"--target Summary!B2", "red"), file=sys.stderr)
            return 2
        target = cell.key
    cg = CodeGenerator(wb, rep.symbols, rep.graph, rep.types, asts,
                       targets=[target] if target else None)
    print(cg.generate())
    print(c(f"# {dict(cg.stats)}", "dim"))
    return 0


def cmd_bench(args) -> int:
    from .bench import format_benchmark, run_benchmark
    print(c(f"Benchmarking {args.rows:,}-row sales model (this takes a few seconds)…", "dim"))
    b = run_benchmark(n_rows=args.rows, repeat=args.repeat, include_naive=not args.no_naive)
    print(format_benchmark(b))
    return 0


def cmd_serve(args) -> int:
    from .server import serve
    serve(host=args.host, port=args.port, open_browser=not args.no_browser)
    return 0


def cmd_grammar(args) -> int:
    from .grammar import END, EPS, LL1, SLR_PARSER
    g = LL1
    print(c("LL(1) grammar of the formula language", "bold"), c(f"({g.note})", "dim"))
    for line in g.bnf():
        print("  " + line)
    first, follow = g.first_sets(), g.follow_sets()
    print(c("\nFIRST and FOLLOW sets", "bold"))
    for nt in g.nonterminals:
        fs = ", ".join(sorted(first[nt], key=lambda x: (x == EPS, x)))
        fo = ", ".join(sorted(follow[nt], key=lambda x: (x == END, x)))
        print(f"  {nt:<8} FIRST = {{ {fs} }}\n  {'':<8} FOLLOW = {{ {fo} }}")
    table, conflicts = g.ll1_table()
    n = sum(len(v) for v in table.values())
    print(c(f"\nLL(1) table: {len(g.nonterminals)} non-terminals × {len(g.terminals) + 1} "
            f"terminals, {n} entries, {len(conflicts)} conflicts → "
            f"{'the grammar IS LL(1)' if not conflicts else 'NOT LL(1)'}", "green"))
    s = SLR_PARSER
    print(c(f"SLR(1): {len(s.states)} LR(0) item sets, {len(s.conflicts)} conflicts → "
            f"{'the grammar IS SLR(1)' if not s.conflicts else 'NOT SLR(1)'}", "green"))
    return 0


def _trace(rows, ok, err) -> None:
    w1 = max(len(r["stack"]) for r in rows) if rows else 5
    w2 = max(len(r["input"]) for r in rows) if rows else 5
    print(f"{'#':>3}  {'STACK':<{w1}}  {'INPUT':>{w2}}  ACTION")
    for r in rows:
        print(f"{r['step']:>3}  {r['stack']:<{w1}}  {r['input']:>{w2}}  {r['action']}")
    print(c("accepted" if ok else f"rejected: {err}", "green" if ok else "red"))


def cmd_ll1(args) -> int:
    from .grammar import ll1_parse
    r = ll1_parse(args.formula)
    _trace(r["trace"], r["accepted"], r.get("error"))
    return 0


def cmd_slr(args) -> int:
    from .grammar import SLR_PARSER
    r = SLR_PARSER.parse(args.formula)
    _trace(r["trace"], r["accepted"], r.get("error"))
    return 0


def cmd_tac(args) -> int:
    from .tac import tac_report
    try:
        r = tac_report(args.formula, args.target, nregs=args.regs)
    except SheetGuardError as e:
        print(c(e.pretty(), "red"))
        return 1
    print(c("Three-address code (quadruples)", "bold"))
    print(f"  {'#':>2}  {'op':<8} {'arg1':<14} {'arg2':<10} result")
    for q in r["quadruples"]:
        print(f"  {q['i']:>2}  {q['op']:<8} {q['arg1']:<14} {q['arg2']:<10} {q['result']}")
    print(c("\nTriples", "bold"))
    for t in r["triples"]:
        print(f"  ({t['i']})  {t['op']:<8} {t['arg1']:<14} {t['arg2']}")
    print(c("\nBasic blocks / flow graph", "bold"))
    for b in r["blocks"]:
        print(f"  {b['id']} → {', '.join(b['succ']) or 'exit'}")
        for line in b["code"]:
            print("     " + line)
    print(c("\nOptimized TAC", "bold"))
    for q in r["optimized"]:
        print("  " + q["text"])
    for entry in r["log"]:
        print(c("   · " + entry, "dim"))
    print(c(f"\nTarget code ({args.regs} registers): {r['target']['stats']}", "bold"))
    for line in r["target"]["asm"]:
        print("  " + line)
    return 0


def cmd_target(args) -> int:
    from .multitarget import JSGenerator, SQLGenerator, verify_js, verify_sql
    wb = _load(args.file)
    rep = analyze(wb)
    asts = rep.optimization.asts if rep.optimization else None
    if args.cmd == "js":
        g = JSGenerator(wb, rep.symbols, rep.graph, asts)
        src = g.generate()
        ver = verify_js if args.verify else None
    else:
        g = SQLGenerator(wb, rep.symbols, rep.graph, asts)
        src = g.generate()
        ver = verify_sql if args.verify else None
    if not args.verify:
        print(src)
        return 0
    v = ver(wb, rep, src, g.unsupported)
    if not v.get("ran"):
        print(c(f"could not run: {v.get('reason')}", "yellow"))
        return 1
    if "cells" not in v:
        print(c(f"generated code failed: {v.get('reason')}", "red"))
        return 1
    col = "green" if v["ok"] or v.get("ok_except_flagged") else "red"
    print(c(f"{args.cmd.upper()}: executed the generated code; {v['matching']}/{v['cells']} formula "
            f"cells identical to the interpreter", col))
    for d in v["diffs"]:
        print(f"  {d['cell']:<14} interpreter={d['interpreter']:<22} {args.cmd}={d['target']:<22} "
              + c(d.get("explained", ""), "dim"))
    return 0


def cmd_diff(args) -> int:
    from .diff import diff_demo, diff_workbooks
    d = diff_workbooks(Workbook.load(args.old), Workbook.load(args.new)) if args.old and args.new \
        else diff_demo()
    print(c(f"Semantic diff: {d['old']}  →  {d['new']}", "bold"))
    print(d["summary"] + "\n")
    for e in d["cells"]:
        print(f"  {e['cell']:<14} {e['status']:<16} {str(e.get('old', '')):<26} → {e.get('new', '')}")
        for ch in e.get("changes", []):
            print(c(f"                 · {ch['kind']}: {ch['from']} → {ch['to']}"
                    f"{'  (' + ch['detail'] + ')' if ch['detail'] else ''}", "dim"))
        if e.get("note"):
            print(c(f"                 · {e['note']}", "dim"))
    if d["ripple"]:
        print(c(f"\n{len(d['ripple'])} cell(s) changed value as a consequence:", "bold"))
        for r in d["ripple"][:12]:
            print(f"  {r['cell']:<14} {r['old_value']:>14} → {r['new_value']:<14}  because of "
                  f"{', '.join(r['because_of'])}")
    iss = d["issues"]
    print(c(f"\nIssues: {len(iss['resolved'])} resolved, {len(iss['introduced'])} introduced, "
            f"{iss['persisting']} persisting", "bold"))
    return 0


def cmd_fuzz(args) -> int:
    from .fuzz import fuzz_workbook
    wb = _load(args.file)
    r = fuzz_workbook(wb, analyze(wb))
    print(c(f"Fuzzed {r['inputs']} inputs with {r['runs']} generated test cases in {r['ms']} ms "
            f"({r['runs_per_second']:,} runs/s, compiled execution)", "bold"))
    print(f"sensitivity coverage: {r['sensitivity_coverage']}% of healthy formulas reacted to some "
          f"input\n")
    if r["plausible"]:
        print(c("Latent errors reachable with ordinary inputs:", "red"))
        for f in r["plausible"]:
            print(f"  {f['cell']:<14} {f['formula']:<26} → {f['error']} when {f['witness']}")
            print(c(f"                 fix: {f['suggested_formula']}", "green"))
    for g in r["adversarial_groups"]:
        ex = g["example"]
        print(c(f"\n{g['count']} formula(s) fail with {g['error']} on unusual inputs, e.g. "
                f"{ex['cell']} when {ex['witness']}", "yellow"))
    return 0


def cmd_doctor(args) -> int:
    from .doctor import run_doctor
    return run_doctor(args.port)


def cmd_repl(args) -> int:
    print(c("SheetGuard formula REPL — type a formula (e.g. =SUM(A1:A3)*2), blank line to quit",
            "bold"))
    while True:
        try:
            line = input(c("formula> ", "blue")).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not line:
            return 0
        if not line.startswith("="):
            line = "=" + line
        try:
            toks = tokenize(line)
            print(c("tokens: ", "dim") + " ".join(f"{t.type.value}({t.value})" for t in toks[:-1]))
            tree = parse(line)
            print(ast_tree(tree))
        except SheetGuardError as e:
            print(c(e.pretty(), "red"))


def main(argv=None) -> int:
    _setup_console()
    from .install_check import install_problem
    problem = install_problem()
    if problem:
        print(c("error: " + problem, "red"), file=sys.stderr)
        return 2
    p = argparse.ArgumentParser(prog="sheetguard", description="Compiler for spreadsheet formulas")
    p.add_argument("--version", action="version", version=f"SheetGuard {__version__}")
    sub = p.add_subparsers(dest="cmd")

    a = sub.add_parser("lex", help="show the token stream of a formula")
    a.add_argument("formula")
    a.set_defaults(fn=cmd_lex)

    a = sub.add_parser("parse", help="show the AST of a formula")
    a.add_argument("formula")
    a.add_argument("--dot", action="store_true", help="emit Graphviz DOT")
    a.add_argument("--json", action="store_true", help="emit JSON")
    a.set_defaults(fn=cmd_parse)

    a = sub.add_parser("analyze", help="analyze a workbook (.xlsx / .csv / .json)")
    a.add_argument("file", nargs="?")
    a.add_argument("-v", "--verbose", action="store_true", help="show explanations and fixes")
    a.add_argument("-t", "--types", action="store_true", help="show inferred cell types")
    a.add_argument("--json", action="store_true")
    a.set_defaults(fn=cmd_analyze)

    a = sub.add_parser("graph", help="dependency DAG as Graphviz DOT")
    a.add_argument("file", nargs="?")
    a.set_defaults(fn=cmd_graph)

    a = sub.add_parser("fix", help="auto-correct a workbook and export an annotated .xlsx")
    a.add_argument("file", nargs="?")
    a.add_argument("-o", "--output")
    a.set_defaults(fn=cmd_fix)

    a = sub.add_parser("demo-files", help="write a demo workbook set (.xlsx, .json, fixed .xlsx)")
    a.add_argument("--set", type=int, choices=(1, 2), default=1,
                   help="1 = Acme sales report (./demo), 2 = semester grade book (./demo 2)")
    a.add_argument("--dir", default=None)
    a.add_argument("--fixed", action="store_true", help="also write the auto-fixed copy (set 1)")
    a.set_defaults(fn=cmd_demo_files)

    a = sub.add_parser("run", help="evaluate the workbook and print every sheet")
    a.add_argument("file", nargs="?")
    a.add_argument("--rows", type=int, default=200, help="rows to print per sheet")
    a.set_defaults(fn=cmd_run)

    a = sub.add_parser("compile", help="print the generated Python (or NumPy) code")
    a.add_argument("file", nargs="?")
    a.add_argument("--numpy", action="store_true", help="vectorizing NumPy back-end")
    a.add_argument("--target", help="compile only the slice needed for this cell, e.g. "
                                    "Summary!B2")
    a.set_defaults(fn=cmd_compile)

    a = sub.add_parser("bench", help="benchmark interpreter vs compiled vs NumPy")
    a.add_argument("--rows", type=int, default=2000)
    a.add_argument("--repeat", type=int, default=3)
    a.add_argument("--no-naive", action="store_true")
    a.set_defaults(fn=cmd_bench)

    a = sub.add_parser("serve", help="start the web UI / API backend")
    a.add_argument("--host", default="127.0.0.1")
    a.add_argument("--port", type=int, default=8765)
    a.add_argument("--no-browser", action="store_true")
    a.set_defaults(fn=cmd_serve)

    a = sub.add_parser("grammar", help="formal grammar, FIRST/FOLLOW, LL(1)/SLR(1) checks")
    a.set_defaults(fn=cmd_grammar)
    a = sub.add_parser("ll1", help="LL(1) predictive parse trace")
    a.add_argument("formula")
    a.set_defaults(fn=cmd_ll1)
    a = sub.add_parser("slr", help="SLR(1) shift-reduce parse trace")
    a.add_argument("formula")
    a.set_defaults(fn=cmd_slr)
    a = sub.add_parser("tac", help="three-address code, basic blocks, optimization, target code")
    a.add_argument("formula")
    a.add_argument("--target", default="result")
    a.add_argument("--regs", type=int, default=4)
    a.set_defaults(fn=cmd_tac)

    for name, helptext in (("js", "compile the workbook to JavaScript"),
                           ("sql", "compile the workbook to one SQL query (CTE per DAG level)")):
        a = sub.add_parser(name, help=helptext)
        a.add_argument("file", nargs="?")
        a.add_argument("--verify", action="store_true", help="execute it and compare every cell")
        a.set_defaults(fn=cmd_target)
    a = sub.add_parser("diff", help="semantic diff between two workbook versions")
    a.add_argument("old", nargs="?")
    a.add_argument("new", nargs="?")
    a.set_defaults(fn=cmd_diff)
    a = sub.add_parser("fuzz", help="type-guided fuzz testing for latent runtime errors")
    a.add_argument("file", nargs="?")
    a.set_defaults(fn=cmd_fuzz)

    a = sub.add_parser("doctor", help="check the installation and self-test every engine")
    a.add_argument("--port", type=int, default=8765)
    a.set_defaults(fn=cmd_doctor)

    a = sub.add_parser("repl", help="interactive formula compiler")
    a.set_defaults(fn=cmd_repl)

    args = p.parse_args(argv)
    if not getattr(args, "fn", None):
        p.print_help()
        return 0
    try:
        if args.fn in (cmd_serve, cmd_repl):
            return args.fn(args)
        from .stack import run_deep
        return run_deep(args.fn, args)          # big stack: deeply nested formulas are fine
    except RecursionError:
        print(c("error: a formula is nested too deeply to analyse", "red"), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print()
        return 130
    except BrokenPipeError:                      # e.g. `... | head`
        return 0
    except (FileNotFoundError, IsADirectoryError, PermissionError) as e:
        print(c(f"error: {e.strerror or e}: {e.filename}" if getattr(e, "filename", None)
                else f"error: {e}", "red"), file=sys.stderr)
        return 2
    except (WorkbookLoadError, RuntimeError, ValueError) as e:
        print(c(f"error: {e}", "red"), file=sys.stderr)
        if os.environ.get("SHEETGUARD_DEBUG"):
            raise
        return 2
    except Exception as e:                       # never show a raw traceback to a user
        if os.environ.get("SHEETGUARD_DEBUG"):
            raise
        print(c(f"internal error: {type(e).__name__}: {e}", "red"), file=sys.stderr)
        print(c("please report this; set SHEETGUARD_DEBUG=1 to see the full traceback",
                "dim"), file=sys.stderr)
        return 3


if __name__ == "__main__":
    sys.exit(main())
