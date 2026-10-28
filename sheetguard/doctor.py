"""`python -m sheetguard doctor` - one command that checks the installation and
proves every engine works, so a demo machine can be verified in ten seconds."""
from __future__ import annotations

import os
import platform
import shutil
import socket
import sqlite3
import sys
import time

from . import __version__

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _check(results, name, fn):
    t = time.perf_counter()
    try:
        status, detail = fn()
    except Exception as e:                         # a check must never crash the doctor
        status, detail = "fail", f"{type(e).__name__}: {e}"
    results.append((status, name, detail, (time.perf_counter() - t) * 1000))


def _python():
    v = sys.version_info
    if v < (3, 8):
        return "fail", (f"Python {platform.python_version()} ({sys.executable}) - SheetGuard "
                        f"needs Python 3.8 or newer")
    if v.releaselevel != "final":
        return "warn", (f"Python {platform.python_version()} is a PRE-RELEASE ({sys.executable}). "
                        f"SheetGuard works, but numpy may not install for it: install Python 3.13 "
                        f"from python.org (the launchers prefer it automatically)")
    return "ok", f"Python {platform.python_version()} ({sys.executable})"


def _module(name, purpose):
    def f():
        try:
            m = __import__(name)
            return "ok", f"{name} {getattr(m, '__version__', '')} - {purpose}"
        except ImportError:
            return "warn", f"{name} missing - {purpose} disabled  (pip install {name})"
    return f


def _node():
    node = shutil.which("node")
    if not node:
        return "warn", "Node.js not found - JavaScript back-end can be generated but not executed"
    import subprocess
    v = subprocess.run([node, "--version"], capture_output=True, text=True, timeout=10).stdout
    return "ok", f"Node.js {v.strip()} - JavaScript back-end is verified by execution"


def _sqlite():
    return "ok", f"SQLite {sqlite3.sqlite_version} - SQL back-end is verified by execution"


def _files():
    from .install_check import install_problem
    problem = install_problem()
    if problem:
        return "fail", problem
    need = ["web/index.html", "web/app.js", "web/app.css", "addon/Code.gs", "addon/Sidebar.html",
            "addon/appsscript.json"]
    missing = [p for p in need if not os.path.isfile(os.path.join(ROOT, p))]
    if missing:
        return "fail", "missing: " + ", ".join(missing)
    side = open(os.path.join(ROOT, "addon/Sidebar.html"), encoding="utf-8").read()
    app = open(os.path.join(ROOT, "web/app.js"), encoding="utf-8").read()
    if app.replace("</script>", "<\\/script>") not in side:
        return "warn", "addon/Sidebar.html is older than web/app.js - run: python tools/build_addon.py"
    return "ok", "web UI and Google Sheets add-on files present and in sync"


def _writable():
    # os.access only: the check must not leave files behind in the user's folder
    if os.access(ROOT, os.W_OK):
        return "ok", f"project folder is writable ({ROOT})"
    return "warn", f"cannot write in {ROOT} - `fix` / `demo-files` need -o elsewhere"


def _port(port):
    def f():
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            busy = s.connect_ex(("127.0.0.1", port)) == 0
        if not busy:
            return "ok", f"port {port} is free for `serve`"
        from .server import _is_sheetguard
        if _is_sheetguard("127.0.0.1", port):
            return "ok", f"SheetGuard is already running on http://localhost:{port}/"
        return "warn", f"port {port} is used by another program - `serve` will pick the next free port"
    return f


def _pipeline():
    from .demo import build_demo_workbook
    from .pipeline import analyze
    rep = analyze(build_demo_workbook())
    c = rep.counts()
    ok = (c["error"], c["warning"], len(rep.fixes)) == (7, 9, 10)
    return ("ok" if ok else "fail",
            f"demo workbook: {c['error']} errors, {c['warning']} warnings, {c['info']} notes, "
            f"{len(rep.fixes)} auto-fixes" + ("" if ok else "  (expected 7 / 9 / 10)"))


def _engines():
    import math
    from . import runtime as rt
    from .codegen import CodeGenerator, prepare_inputs
    from .demo import build_demo_workbook
    from .evaluator import Interpreter
    from .multitarget import JSGenerator, SQLGenerator, verify_js, verify_sql
    from .pipeline import analyze
    wb = build_demo_workbook()
    rep = analyze(wb)
    ref = Interpreter(wb, rep.symbols, rep.graph).run()
    asts = rep.optimization.asts if rep.optimization else None

    def same(a, b):
        if isinstance(a, float) and isinstance(b, float):
            return math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-9)
        return str(a) == str(b)
    keys = [c.key for c in wb.formula_cells()]
    out = CodeGenerator(wb, rep.symbols, rep.graph, rep.types, asts).compile()(prepare_inputs(wb))
    parts = [f"compiled Python {sum(same(ref[k], out[k]) for k in keys)}/{len(keys)}"]
    bad = sum(not same(ref[k], out[k]) for k in keys)
    try:
        from .vectorize import VectorCompiler
        vc = VectorCompiler(wb, rep.symbols, rep.graph, rep.types, asts)
        fn = vc.compile(fallback=CodeGenerator(wb, rep.symbols, rep.graph, rep.types,
                                               asts).compile())
        vo = fn.materialize(fn(prepare_inputs(wb)))
        n = sum(same(ref[k], vo.get(k)) for k in keys)
        bad += len(keys) - n
        parts.append(f"NumPy {n}/{len(keys)}")
    except ImportError:
        parts.append("NumPy skipped")
    js = JSGenerator(wb, rep.symbols, rep.graph, asts)
    v = verify_js(wb, rep, js.generate(), js.unsupported)
    parts.append(f"JavaScript {v['matching']}/{v['cells']}" if "cells" in v else "JavaScript skipped")
    if "cells" in v and v["unexplained"]:
        bad += v["unexplained"]
    sq = SQLGenerator(wb, rep.symbols, rep.graph, asts)
    v = verify_sql(wb, rep, sq.generate(), sq.unsupported)
    if "cells" in v:
        parts.append(f"SQL {v['matching']}/{v['cells']} ({v['cells'] - v['matching']} explained)")
        bad += v["unexplained"]
    else:
        parts.append(f"SQL failed: {v.get('reason')}")
        bad += 1
    return ("ok" if bad == 0 else "fail", "all engines agree with the interpreter: " +
            ", ".join(parts))


def _theory():
    from .grammar import LL1, SLR_PARSER
    _, conflicts = LL1.ll1_table()
    ok = not conflicts and not SLR_PARSER.conflicts
    return ("ok" if ok else "fail",
            f"grammar is LL(1) ({len(conflicts)} conflicts) and SLR(1) "
            f"({len(SLR_PARSER.states)} states, {len(SLR_PARSER.conflicts)} conflicts)")


def _fuzz():
    from .demo import build_demo_workbook
    from .fuzz import fuzz_workbook
    from .pipeline import analyze
    wb = build_demo_workbook()
    r = fuzz_workbook(wb, analyze(wb))
    ok = len(r["plausible"]) == 2
    return ("ok" if ok else "fail",
            f"fuzzer: {r['runs']} runs in {r['ms']} ms, {len(r['plausible'])} latent bugs found "
            f"(expected 2)")


def run_doctor(port: int = 8765) -> int:
    from .__main__ import c
    print(c(f"SheetGuard {__version__} - installation check", "bold"))
    results = []
    _check(results, "Python", _python)
    _check(results, "openpyxl", _module("openpyxl", "reading/writing .xlsx files"))
    _check(results, "numpy", _module("numpy", "vectorizing back-end + 1000x benchmark"))
    _check(results, "Node.js", _node)
    _check(results, "SQLite", _sqlite)
    _check(results, "files", _files)
    _check(results, "folder", _writable)
    _check(results, "port", _port(port))
    _check(results, "compiler", _pipeline)
    _check(results, "engines", _engines)
    _check(results, "theory", _theory)
    _check(results, "fuzzer", _fuzz)
    mark = {"ok": c("  OK  ", "green"), "warn": c(" WARN ", "yellow"), "fail": c(" FAIL ", "red")}
    for status, name, detail, ms in results:
        print(f"{mark[status]} {name:<9} {detail}" + c(f"  ({ms:.0f} ms)", "dim"))
    fails = [r for r in results if r[0] == "fail"]
    warns = [r for r in results if r[0] == "warn"]
    print()
    if fails:
        print(c(f"{len(fails)} check(s) failed - see above.", "red"))
        return 1
    print(c("All essential checks passed - SheetGuard is ready." +
            (f" ({len(warns)} optional feature(s) unavailable)" if warns else ""), "green"))
    return 0
