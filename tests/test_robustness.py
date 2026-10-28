"""Robustness and cross-engine consistency.

Every bug found by the hardening pass (random workbooks, differential testing of
the five execution engines, malformed files, very large and very deep inputs)
has a regression test here.
"""
import json
import math
import os
import random
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from sheetguard import api
from sheetguard import runtime as rt
from sheetguard.codegen import CodeGenerator, prepare_inputs
from sheetguard.errors import SheetGuardError
from sheetguard.evaluator import Interpreter
from sheetguard.multitarget import JSGenerator, SQLGenerator, verify_js, verify_sql
from sheetguard.parser import MAX_NESTING, parse
from sheetguard.pipeline import analyze
from sheetguard.workbook import Workbook, WorkbookLoadError

try:
    import numpy  # noqa: F401
    HAS_NP = True
except ImportError:
    HAS_NP = False
try:
    import openpyxl  # noqa: F401
    HAS_XL = True
except ImportError:
    HAS_XL = False
HAS_NODE = shutil.which("node") is not None


def norm(v):
    if isinstance(v, rt.XLError):
        return v.code
    if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
        return "#NUM!"
    return v


def same(a, b):
    a, b = norm(a), norm(b)
    if isinstance(a, float) and isinstance(b, float):
        return abs(a - b) <= 1e-9 * max(1.0, abs(a), abs(b))
    return a == b


def engines(wb):
    """Values from the interpreter and the compiled code (with and without optimizer ASTs)."""
    rep = analyze(wb)
    ref = Interpreter(wb, rep.symbols, rep.graph).run()
    outs = []
    for asts in (None, rep.optimization.asts):
        fn = CodeGenerator(wb, rep.symbols, rep.graph, rep.types, asts).compile()
        outs.append(fn(prepare_inputs(wb)))
    return rep, ref, outs


def values(cells, **kw):
    wb = Workbook.from_dict({"S": cells}, **kw)
    rep, ref, outs = engines(wb)
    for out in outs:
        for c in wb.formula_cells():
            assert same(ref[c.key], out[c.key]), (c.raw, ref[c.key], out[c.key])
    return {k.split("!")[1]: norm(v) for k, v in ref.items()}


class Demo2Tests(unittest.TestCase):
    """The second demo set (semester grade book) - every planted bug is reported."""

    def test_every_planted_bug_is_caught_and_engines_agree(self):
        from sheetguard.demo2 import build_demo2_workbook
        from sheetguard.fixes import apply_fixes
        wb = build_demo2_workbook()
        rep, ref, outs = engines(wb)
        found = {(d.cell, d.code) for d in rep.diagnostics}
        planted = {("Marks!I5", "SG401"), ("Marks!I8", "SG403"), ("Marks!I10", "SG404"),
                   ("Marks!F6", "SG305"), ("Marks!J7", "SG303"), ("Marks!K9", "SG403"),
                   ("Marks!C12", "SG402"), ("Marks!I12", "SG201"), ("Report!B7", "SG104"),
                   ("Report!B8", "SG102"), ("Report!B9", "SG303"), ("Report!B10", "SG001"),
                   ("Report!B11", "SG103"), ("Report!B12", "SG101"), ("Report!B13", "SG105"),
                   ("Report!B14", "SG501"), ("Weights!B11", "SG502")}
        self.assertEqual(planted - found, set())
        for out in outs:
            for c in wb.formula_cells():
                self.assertTrue(same(ref[c.key], out[c.key]), (c.key, ref[c.key], out[c.key]))
        fixed = analyze(apply_fixes(wb, rep.fixes))
        self.assertEqual(sorted(d.cell for d in fixed.diagnostics if d.severity == "error"),
                         ["Report!B7", "Report!B8", "Report!B9"])     # need a human decision
        self.assertEqual(Interpreter(wb, rep.symbols, rep.graph).run()["Marks!I2"], 80.2)   # 11.7+12.3+9+29.6+17.6


class SpreadsheetSemanticsTests(unittest.TestCase):
    def test_wrong_argument_count_is_na_in_every_engine(self):
        v = values({"A1": 1, "B1": "=IF()", "B2": "=IFERROR()", "B3": "=ROUND(1)",
                    "B4": "=SUM(1/0)+MOD()", "B5": "=IF(A1)"})
        self.assertEqual([v["B1"], v["B2"], v["B3"], v["B5"]], ["#N/A"] * 4)
        self.assertEqual(v["B4"], "#DIV/0!")          # left operand fails first

    def test_unknown_function_is_name_before_arguments_run(self):
        v = values({"B1": "=NOSUCHFN(1/0)", "B2": "=FILTER(1/0)"})
        self.assertEqual((v["B1"], v["B2"]), ("#NAME?", "#NAME?"))

    def test_aggregates_skip_text_in_references_but_not_literals(self):
        v = values({"A1": "x", "A2": True, "A3": 5, "B1": "=SUM(A1)", "B2": '=SUM("x")',
                    "B3": "=SUM(A1:A3)", "B4": "=SUM(A2)", "B5": "=AVERAGE(A1)", "B6": "=SUM(TRUE)"})
        self.assertEqual((v["B1"], v["B2"], v["B3"], v["B4"], v["B5"], v["B6"]),
                         (0.0, "#VALUE!", 5.0, 0.0, "#DIV/0!", 1.0))

    def test_count_ignores_errors_and_and_needs_a_value(self):
        v = values({"A1": 5, "B1": "=COUNT(1/0,A1)", "B2": "=COUNTA(1/0,A1)", "B3": "=AND(Z9)",
                    "B4": "=OR(Z9,1)"})
        self.assertEqual((v["B1"], v["B2"], v["B3"], v["B4"]), (1.0, 2.0, "#VALUE!", True))

    def test_numbers_in_text_use_15_significant_digits(self):
        v = values({"B1": '=0.1+0.2&""', "B2": '=1/3&""', "B3": '=10^20&""', "B4": '=2.5&"x"'})
        self.assertEqual((v["B1"], v["B2"], v["B3"], v["B4"]),
                         ("0.3", "0.333333333333333", "1E+20", "2.5x"))

    def test_numeric_edge_cases(self):
        v = values({"A1": -8, "A2": 1e300, "B1": "=A1^(1/3)", "B2": "=A2*A2", "B3": "=ROUND(2.5,20)",
                    "B4": "=MOD(A1,0)", "B5": "=0^-1", "B6": "=A2*A2-A2*A2", "B7": "=ROUND(1234,-2)"})
        self.assertEqual((v["B1"], v["B2"], v["B3"], v["B4"], v["B5"], v["B6"], v["B7"]),
                         ("#NUM!", "#NUM!", 2.5, "#DIV/0!", "#DIV/0!", "#NUM!", 1200.0))

    def test_new_functions(self):
        v = values({"A1": 2.75, "A2": 4, "A3": 4, "A4": 10, "B1": "=TRUNC(A1,1)", "B2": "=EVEN(3)",
                    "B3": "=LARGE(A1:A4,2)", "B4": "=MODE(A1:A4)", "B5": "=RANK(A4,A1:A4)",
                    "B6": "=AVERAGEIFS(A1:A4,A1:A4,\">3\")", "B7": "=FACT(5)", "B8": "=GCD(12,18)"})
        self.assertEqual([v[f"B{i}"] for i in range(1, 9)], [2.7, 4.0, 4.0, 4.0, 1.0, 6.0, 120.0, 6.0])

    def test_unevaluated_function_uses_the_cached_value(self):
        wb = Workbook.from_json({"name": "t", "sheets": [{"name": "S",
                                 "formulas": [["", "=FILTER(A1:A3,A1:A3>1)"], ["", "=B1*2"]],
                                 "values": [[1, 5], [2, ""], [3, ""]]}]})
        rep, ref, outs = engines(wb)
        self.assertEqual(ref["S!B2"], 10.0)
        self.assertTrue(all(o["S!B2"] == 10.0 for o in outs))
        self.assertIn("SG106", [d.code for d in rep.diagnostics])


class AnalysisFalsePositiveTests(unittest.TestCase):
    def test_division_guarded_by_iferror_is_not_an_error(self):
        rep = analyze(Workbook.from_dict({"S": {"A1": '=IFERROR(1/0,"n/a")', "A2": "=1/0"}}))
        flagged = {d.cell for d in rep.diagnostics if d.code == "SG304"}
        self.assertEqual(flagged, {"S!A2"})

    def test_multi_area_named_range_is_a_note_not_an_error(self):
        wb = Workbook.from_dict({"S": {"A1": 1, "A2": 2, "B1": "=SUM(Both)"}},
                                names={"Both": "S!$A$1,S!$A$2"})
        rep = analyze(wb)
        self.assertEqual([d.severity for d in rep.diagnostics if d.code == "SG103"], ["info"])


class DeepAndLargeInputTests(unittest.TestCase):
    def test_nesting_limit_gives_a_clean_error(self):
        parse("=" + "(" * 40 + "1" + ")" * 40)
        with self.assertRaises(SheetGuardError) as ctx:
            parse("=" + "(" * (MAX_NESTING + 5) + "1" + ")" * (MAX_NESTING + 5))
        self.assertIn("nested too deeply", ctx.exception.message)
        parse("=" + "-" * 3000 + "1")                 # unary chains are iterative

    def test_long_formula_compiles_with_hoisted_temporaries(self):
        f = "=" + "+".join(["A1"] * 150)
        v = values({"A1": 2, "B1": f, "B2": "=IFERROR(" + "+".join(["1/0"] * 120) + ",7)"})
        self.assertEqual((v["B1"], v["B2"]), (300.0, 7.0))

    def test_long_dependency_chain_is_fast(self):
        import time
        n = 3000
        wb = {"name": "chain", "sheets": [{"name": "S", "formulas": [[""]] + [[f"=A{i}+1"] for i in range(1, n)],
                                           "values": [[1]] + [[""] for _ in range(1, n)]}]}
        t = time.perf_counter()
        d = api.analyze_payload({"workbook": wb})
        self.assertLess(time.perf_counter() - t, 20)
        self.assertEqual(d["cells"]["S!A2"]["impact_count"], None)       # budgeted: "200+"
        self.assertEqual(len(d["cells"]["S!A2"]["impact"]), 200)

    def test_sql_running_total_chain_does_not_explode(self):
        cells = {"A1": 1}
        for r in range(2, 40):
            cells[f"A{r}"] = f"=ROUND(A{r - 1}*1.01,2)+INT(A{r - 1}/7)"
        wb = Workbook.from_dict({"S": cells})
        rep = analyze(wb)
        g = SQLGenerator(wb, rep.symbols, rep.graph)
        v = verify_sql(wb, rep, g.generate(), g.unsupported)
        self.assertTrue(v["ok"], v)


class LoaderTests(unittest.TestCase):
    def tmp(self, name, data, mode="w"):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        path = os.path.join(d, name)
        with open(path, mode, **({} if "b" in mode else {"encoding": "utf-8"})) as fh:
            fh.write(data)
        return path

    def test_semicolon_csv_with_decimal_commas(self):
        wb = Workbook.load(self.tmp("eu.csv", "Item;Qty;Price;Total\nA;2;3,5;=B2*C2\n"))
        rep, ref, _ = engines(wb)
        self.assertEqual(ref["Sheet1!D2"], 7.0)

    def test_bad_inputs_raise_clear_errors(self):
        with self.assertRaises(FileNotFoundError):
            Workbook.load(os.path.join(ROOT, "no_such_file.xlsx"))
        for name, data, mode in (("x.json", '{"sheets": 5}', "w"), ("x.json", "nonsense", "w"),
                                 ("old.xls", "x", "w"), ("x.xlsx", b"\x00garbage", "wb")):
            with self.assertRaises(WorkbookLoadError, msg=name):
                Workbook.load(self.tmp(name, data, mode))
        with self.assertRaises(WorkbookLoadError):
            Workbook.from_json({"sheets": [1, 2]})

    def test_literal_coercion_is_strict(self):
        wb = Workbook.from_dict({"S": {"A1": "nan", "B1": "inf", "C1": "1_000", "D1": "1,200", "E1": "TRUE"}})
        self.assertEqual([c.raw for c in wb.cells()], ["nan", "inf", "1_000", 1200, True])

    def test_json_text_numbers_stay_text(self):
        # Apps Script getValues() is typed: the string "78" is a number stored as text
        wb = Workbook.from_json({"sheets": [{"name": "S", "values": [["78", 5, "#N/A", ""]],
                                             "formulas": [["", "", "", "=A1+B1"]]}]})
        self.assertEqual([c.raw for c in wb.cells()], ["78", 5, "#N/A", "=A1+B1"])
        self.assertIn("SG305", [d.code for d in analyze(wb).diagnostics])
        from sheetguard.demo2 import build_demo2_workbook
        demo2 = build_demo2_workbook()
        again = Workbook.from_json(json.loads(json.dumps(demo2.to_json())))
        self.assertEqual(again.cell("Marks!F6").raw, "78")
        self.assertEqual(sorted(d.code for d in analyze(again).diagnostics),
                         sorted(d.code for d in analyze(demo2).diagnostics))

    def test_invalid_cell_lookup_returns_none(self):
        wb = Workbook.from_dict({"S": {"A1": 1}})
        self.assertIsNone(wb.cell("S!ZZZ"))
        self.assertIsNone(wb.cell("nonsense"))

    @unittest.skipUnless(HAS_XL, "openpyxl not installed")
    def test_xlsx_dates_booleans_and_odd_names(self):
        import datetime
        import openpyxl
        from openpyxl.workbook.defined_name import DefinedName
        book = openpyxl.Workbook()
        ws = book.active
        ws.title = "Données Q3"
        ws["A1"] = datetime.date(2026, 7, 1)
        ws["A2"] = datetime.datetime(2026, 7, 1, 12, 0)
        ws["A3"] = "=TRUE()"
        ws["A4"] = "=A2-A1"
        book.defined_names["Broken"] = DefinedName("Broken", attr_text="#REF!")
        path = self.tmp("edge.xlsx", "")
        book.save(path)
        wb = Workbook.load(path)
        self.assertEqual(wb.cell("Données Q3!A1").raw, 46204.0)
        self.assertIs(wb.cell("Données Q3!A3").raw, True)
        rep, ref, _ = engines(wb)
        self.assertEqual(ref["Données Q3!A4"], 0.5)

    @unittest.skipUnless(HAS_XL, "openpyxl not installed")
    def test_export_survives_hostile_sheet_names_and_broken_formulas(self):
        import openpyxl
        from sheetguard.fixes import export_xlsx
        wb = Workbook.from_json({"sheets": [
            {"name": "a/b:c*?[x]", "formulas": [["", "=SUM(A1:A3", "=1+"]], "values": [["x\x02y", "", ""]]},
            {"name": "SheetGuard Report", "formulas": [["=1"]]},
            {"name": "A" * 40, "formulas": [["=2"]]}, {"name": "A" * 35, "formulas": [["=3"]]}]})
        rep = analyze(wb)
        path = self.tmp("out.xlsx", "")
        export_xlsx(wb, path, rep.diagnostics, rep.fixes)
        names = openpyxl.load_workbook(path).sheetnames
        self.assertEqual(len(names), 5)
        self.assertEqual(len({n.lower() for n in names}), 5)


class ApiRobustnessTests(unittest.TestCase):
    def test_bad_parameters_raise_value_errors(self):
        for fn, payload in ((api.bench_payload, {"rows": "abc"}),
                            (api.theory_payload, {"formula": "=1", "nregs": "x"}),
                            (api.compile_payload, {"demo": True, "target": "nonsense"}),
                            (api.analyze_payload, {}),
                            (api.analyze_payload, {"xlsx_base64": "!!!"})):
            with self.assertRaises(ValueError):
                fn(payload)

    def test_odd_but_valid_payloads(self):
        self.assertIn("error", api.parse_payload({"formula": None}))
        self.assertIn("tokens", api.parse_payload({"formula": 123}))
        self.assertEqual(api.analyze_payload({"workbook": {"sheets": []}})["counts"]["error"], 0)
        d = api.analyze_payload({"sheets": [{"name": "S", "formulas": [["=1+1"]]}]})
        self.assertEqual(d["workbook"]["formulas"], 1)


class DifferentialTests(unittest.TestCase):
    """Random workbooks; all execution engines must agree with the interpreter."""

    FORMS = ["A{r}*B{r}", "A{r}/B{r}", "IF(A{r}>B{r},A{r}-B{r},0)", "ROUND(A{r}/3,2)",
             "A{r}^2-B{r}", "MAX(A{r},B{r})", "IFERROR(A{r}/B{r},-1)", "A{r}&\"x\"", "SUM(A{r}:B{r})",
             "AND(A{r},B{r})", "COUNT(A{r},1/B{r})", "INT(A{r}/2)", "MOD(A{r},3)", "-A{r}%"]

    def random_workbook(self, rnd, rows=12):
        cells = {}
        form = rnd.choice(self.FORMS)
        for r in range(1, rows + 1):
            cells[f"A{r}"] = rnd.choice([rnd.randint(-9, 99), round(rnd.uniform(-5, 500), 3), 0, "x", True, 1e300])
            cells[f"B{r}"] = rnd.choice([rnd.randint(-3, 9), 0, 2.5, ""])
            cells[f"C{r}"] = "=" + (rnd.choice(self.FORMS) if rnd.random() < .15 else form).format(r=r)
        cells[f"C{rows + 1}"] = f"=SUM(C1:C{rows})"
        cells[f"D{rows + 1}"] = f"=AVERAGE(C1:C{rows})"
        return Workbook.from_dict({"S": {k: v for k, v in cells.items() if v != ""}})

    def test_engines_agree_on_random_workbooks(self):
        rnd = random.Random(2026)
        for i in range(40):
            wb = self.random_workbook(rnd)
            rep, ref, outs = engines(wb)
            for out in outs:
                for c in wb.formula_cells():
                    self.assertTrue(same(ref[c.key], out[c.key]), (i, c.raw, ref[c.key], out[c.key]))
            asts = rep.optimization.asts
            if HAS_NP:
                from sheetguard.vectorize import VectorCompiler
                vc = VectorCompiler(wb, rep.symbols, rep.graph, rep.types, asts)
                fn = vc.compile(fallback=CodeGenerator(wb, rep.symbols, rep.graph, rep.types, asts).compile())
                vo = fn.materialize(fn(prepare_inputs(wb)))
                for c in wb.formula_cells():
                    self.assertTrue(same(ref[c.key], vo[c.key]), ("numpy", i, c.raw, ref[c.key], vo[c.key]))
            g = SQLGenerator(wb, rep.symbols, rep.graph, asts)
            v = verify_sql(wb, rep, g.generate(), g.unsupported)
            self.assertTrue(v.get("ok_except_flagged"), (i, v))
            if HAS_NODE and i % 4 == 0:
                g = JSGenerator(wb, rep.symbols, rep.graph, asts)
                v = verify_js(wb, rep, g.generate(), g.unsupported)
                self.assertTrue(v.get("ok_except_flagged"), (i, v))


class ParserAgreementTests(unittest.TestCase):
    """The hand-written parser, the LL(1) table parser and the SLR(1) parser recognise the
    same language (array row lengths are a semantic check, so those are left out)."""

    def test_three_parsers_agree(self):
        from sheetguard.grammar import SLR_PARSER, ll1_parse
        rnd = random.Random(7)
        atoms = ["A1", "B2", "Sheet2!A1", "'My S'!B2", "A1:B3", "A:A", "1:1", "Rate", "1", "2.5", '"t"',
                 "TRUE", "#N/A", "{1,-2;\"a\",TRUE}", "+", "-", "*", "/", "^", "&", "=", "<>", "<=", "%",
                 "(", ")", ",", ";", ":", "SUM(", "IF(", "ROUND(", "{", "}", " "]
        corpus = ["='My S'!B2:'My S'!C3", "=F(,)", "=IF(A1,,0)", "=SUM(A1,)", "=SUM()", "={1,2;3,4}", "=-1%%", "=--1"]
        for _ in range(1500):
            corpus.append("=" + "".join(rnd.choice(atoms) for _ in range(rnd.randint(1, 9))))
        semantic = ("two different sheets", "rows have different lengths")   # not context-free
        for f in corpus:
            try:
                parse(f)
                rd = True
            except SheetGuardError as e:
                rd = False
                if any(s in e.message for s in semantic):
                    continue
            self.assertEqual((rd, ll1_parse(f)["accepted"], SLR_PARSER.parse(f)["accepted"]),
                             (rd, rd, rd), f)


class TacConsistencyTests(unittest.TestCase):
    """Three-address code (before and after optimization) computes what the interpreter does."""

    def rf(self, rnd, d=0):
        if d > 3 or rnd.random() < 0.3:
            return rnd.choice(["A1", "B1", "C1", "2", "0.5", "10", "0", '"x"', "TRUE"])
        k = rnd.random()
        if k < 0.5:
            op = rnd.choice(["+", "-", "*", "/", "^", "&", "=", ">"])
            return f"({self.rf(rnd, d + 1)}{op}{self.rf(rnd, d + 1)})"
        if k < 0.6:
            return f"-{self.rf(rnd, d + 1)}"
        if k < 0.8:
            return f"IF({self.rf(rnd, d + 1)}>{self.rf(rnd, d + 1)},{self.rf(rnd, d + 1)},{self.rf(rnd, d + 1)})"
        f = rnd.choice(["SUM", "MAX", "IFERROR", "ROUND"])
        n = {"IFERROR": 2, "ROUND": 2}.get(f, rnd.randint(1, 3))
        return f"{f}(" + ",".join(self.rf(rnd, d + 1) for _ in range(n)) + ")"

    def test_tac_matches_interpreter(self):
        from sheetguard.tac import Quad, optimize_tac, run_tac, translate
        rnd = random.Random(11)
        for _ in range(400):
            f = "=" + self.rf(rnd)
            env = {x: rnd.choice([0.0, 1.0, -2.0, 3.5, None, "s", True]) for x in ("A1", "B1", "C1")}
            wb = Workbook.from_dict({"S": {**{k: v for k, v in env.items() if v is not None}, "E1": f}})
            rep = analyze(wb)
            ref = Interpreter(wb, rep.symbols, rep.graph).run()["S!E1"]
            code = translate(f, "OUT")
            opt, _, _ = optimize_tac([Quad(q.op, q.arg1, q.arg2, q.result) for q in code])
            self.assertTrue(same(ref, run_tac(code, env).get("OUT")), (f, env))
            self.assertTrue(same(ref, run_tac(opt, env).get("OUT")), ("optimized", f, env))

    def test_register_machine_code_computes_the_same_value(self):
        """Run the generated load/store assembly on a tiny machine simulator, with 2-8
        registers: proves the register allocator (spills, descriptors) is correct."""
        from sheetguard.tac import _MNEMONIC, Quad, optimize_tac, run_tac, target_code, translate
        ops = {v: k for k, v in _MNEMONIC.items()}

        def simulate(asm, env):
            mem, reg, stack = dict(env), {}, []
            labels = {l.strip()[:-1]: i for i, l in enumerate(asm) if l.strip().endswith(":")}
            pc = 0
            while pc < len(asm):
                line = asm[pc].strip()
                pc += 1
                if line.endswith(":"):
                    continue
                op, _, rest = line.partition(" ")
                a = [x.strip() for x in rest.split(",")]
                if op == "LD":
                    reg[a[0]] = float(a[1][1:]) if a[1].startswith("#") else mem[a[1]]
                elif op == "ST":
                    mem[a[0]] = reg[a[1]]
                elif op == "BR":
                    pc = labels[a[0]]
                elif op == "BRZ":
                    if isinstance(reg[a[0]], rt.XLError):
                        return None                   # error condition: not comparable
                    if not rt.truthy(reg[a[0]]):
                        pc = labels[a[1]]
                elif op == "PUSH":
                    stack.append(reg[a[0]])
                elif op == "POP":
                    reg[a[0]] = stack.pop()
                else:
                    try:
                        if op == "NEG":
                            if isinstance(reg[a[1]], rt.XLError):
                                raise reg[a[1]]
                            reg[a[0]] = -reg[a[1]]
                        elif op == "CALL":
                            n = int(a[1])
                            args = stack[len(stack) - n:]
                            del stack[len(stack) - n:]
                            try:
                                stack.append(rt.call(a[0], args))
                            except rt.XLError as e:
                                stack.append(e)
                        else:
                            o = ops[op]
                            x, y = reg[a[1]], reg[a[2]]
                            for z in (x, y):
                                if isinstance(z, rt.XLError):
                                    raise z
                            reg[a[0]] = rt.compare(o, x, y) if o in ("=", "<>", "<", ">", "<=", ">=") \
                                else rt.BINOPS[o](x, y)
                    except rt.XLError as e:
                        reg[a[0]] = e
            return mem

        rnd = random.Random(5)

        def g(d=0):
            if d > 3 or rnd.random() < 0.3:
                return rnd.choice(["A1", "B1", "C1", "D1", "2", "0.5", "10"])
            k = rnd.random()
            if k < 0.55:
                return f"({g(d + 1)}{rnd.choice(['+', '-', '*', '/'])}{g(d + 1)})"
            if k < 0.65:
                return f"-{g(d + 1)}"
            if k < 0.85:
                return f"IF({g(d + 1)}>{g(d + 1)},{g(d + 1)},{g(d + 1)})"
            return f"{rnd.choice(['SUM', 'MAX', 'MIN'])}({g(d + 1)},{g(d + 1)})"
        for _ in range(250):
            f = "=" + g()
            env = {x: rnd.choice([1.0, -2.0, 3.5, 7.0]) for x in ("A1", "B1", "C1", "D1")}
            code = translate(f, "OUT")
            opt, _, _ = optimize_tac([Quad(q.op, q.arg1, q.arg2, q.result) for q in code])
            want = run_tac(code, env)["OUT"]
            for prog in (code, opt):
                for nregs in (2, 3, 4, 8):
                    mem = simulate(target_code(prog, nregs)["asm"], env)
                    if mem is not None:
                        self.assertTrue(same(want, mem["OUT"]), (f, nregs, want, mem["OUT"]))

    def test_identities_only_on_numbers(self):
        from sheetguard.tac import Quad, optimize_tac, run_tac, translate
        for f, env, want in (('=("x"&A1)+0', {"A1": 1.0}, "#VALUE!"), ("=(A1>0)+0", {"A1": 1.0}, 1.0),
                             ("=(A1*2)*1", {"A1": 3.0}, 6.0)):
            code = translate(f, "OUT")
            opt, log, _ = optimize_tac([Quad(q.op, q.arg1, q.arg2, q.result) for q in code])
            self.assertEqual(norm(run_tac(opt, env)["OUT"]), want, f)


class RoundTwoRegressionTests(unittest.TestCase):
    def test_fixes_that_would_introduce_errors_are_not_applied(self):
        cells = {"F1": 0.18}
        for r in range(2, 9):
            cells[f"A{r}"] = r
            cells[f"C{r}"] = f"=A{r}/$F$2"               # the pattern divides by an empty cell
        cells["C5"] = "=A5*2"                             # the "inconsistent" one is actually fine
        rep = analyze(Workbook.from_dict({"S": cells}))
        self.assertNotIn("S!C5", [f.cell for f in rep.fixes])
        d = next(d for d in rep.diagnostics if d.cell == "S!C5" and d.code == "SG401")
        self.assertIn("Not applied automatically", d.fix)

    def test_stray_far_cell_does_not_explode_whole_column_ranges(self):
        import time
        cells = {"A1": 1, "A2": 4, "Z1048576": "stray"}
        for r in range(1, 41):
            cells[f"B{r}"] = "=SUM(A:A)+COUNT(A:A)"
        wb = Workbook.from_dict({"S": cells})
        t = time.perf_counter()
        j = wb.to_json()
        self.assertIn("cells", j["sheets"][0])               # sparse form, no 1M x 26 grid
        d = api.analyze_payload({"workbook": j, "return_workbook": True})
        self.assertLess(time.perf_counter() - t, 10)
        self.assertEqual(d["cells"]["S!B1"]["value"], 7.0)
        self.assertEqual(Workbook.from_json(j).cell("S!Z1048576").raw, "stray")

    def test_dense_grid_is_linear(self):
        import time
        wb = Workbook.from_dict({"S": {f"A{r}": r for r in range(1, 20001)}})
        t = time.perf_counter()
        wb.to_json()
        self.assertLess(time.perf_counter() - t, 3)

    def test_operands_are_coerced_left_to_right(self):
        v = values({"B1": '="x"/0', "B2": "=1/0", "B3": '="x"*(1/0)'})
        self.assertEqual((v["B1"], v["B2"], v["B3"]), ("#VALUE!", "#DIV/0!", "#DIV/0!"))

    @unittest.skipUnless(HAS_XL, "openpyxl not installed")
    def test_exported_names_are_absolute(self):
        import openpyxl
        from sheetguard.fixes import export_xlsx
        wb = Workbook.from_dict({"My S": {"A1": 2, "B1": "=Rate*2"}}, names={"Rate": "'My S'!A1"})
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        path = os.path.join(d, "n.xlsx")
        export_xlsx(wb, path)
        self.assertEqual(openpyxl.load_workbook(path).defined_names["Rate"].attr_text, "'My S'!$A$1")


class CliTests(unittest.TestCase):
    def run_cli(self, *args):
        p = subprocess.run([sys.executable, "-m", "sheetguard", *args], cwd=ROOT,
                           capture_output=True, text=True, encoding="utf-8", timeout=120,
                           env=dict(os.environ, NO_COLOR="1", PYTHONIOENCODING="utf-8"))
        return p.returncode, p.stdout + p.stderr

    def test_friendly_errors(self):
        rc, out = self.run_cli("analyze", "no_such_file.xlsx")
        self.assertEqual(rc, 2)
        self.assertIn("file not found", out)
        self.assertNotIn("Traceback", out)
        rc, out = self.run_cli("compile", "--target", "nope")
        self.assertEqual(rc, 2)
        self.assertIn("not a formula cell", out)

    def test_deep_formula_through_the_cli(self):
        rc, out = self.run_cli("parse", "=" + "(" * 90 + "1" + ")" * 90)
        self.assertEqual(rc, 0, out)
        self.assertNotIn("Traceback", out)

    def test_doctor(self):
        rc, out = self.run_cli("doctor", "--port", "8799")
        self.assertEqual(rc, 0, out)
        self.assertIn("ready", out)


if __name__ == "__main__":
    unittest.main()
