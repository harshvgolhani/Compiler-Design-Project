"""Tests for phases 7-8: runtime library, interpreter, optimizer, code generators."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sheetguard import runtime as rt
from sheetguard.bench import _same, make_sales_workbook
from sheetguard.codegen import CodeGenerator, prepare_inputs
from sheetguard.demo import build_demo_workbook
from sheetguard.evaluator import Interpreter
from sheetguard.fixes import apply_fixes
from sheetguard.pipeline import analyze
from sheetguard.workbook import Workbook

try:
    import numpy  # noqa: F401
    HAVE_NUMPY = True
except ImportError:
    HAVE_NUMPY = False


def run(cells, names=None):
    wb = Workbook.from_dict({"S": cells}, names=names)
    rep = analyze(wb)
    return Interpreter(wb, rep.symbols, rep.graph).run()


class RuntimeTests(unittest.TestCase):
    def test_arithmetic_and_errors(self):
        v = run({"A1": 6, "A2": 0, "A3": "=A1/A2", "A4": "=A3+1", "A5": "=IFERROR(A3,-1)",
                 "A6": "=-2^2", "A7": '="5"*2', "A8": '="abc"*2', "A9": "=A1&\"x\""})
        self.assertEqual(v["S!A3"], rt.DIV0)
        self.assertEqual(v["S!A4"], rt.DIV0)          # errors propagate
        self.assertEqual(v["S!A5"], -1.0)
        self.assertEqual(v["S!A6"], 4.0)              # unary minus binds tighter than ^
        self.assertEqual(v["S!A7"], 10.0)             # numeric text is coerced
        self.assertEqual(v["S!A8"], rt.VALUE)
        self.assertEqual(v["S!A9"], "6x")

    def test_functions(self):
        v = run({"A1": "a", "A2": "b", "A3": "a", "B1": 1, "B2": 2, "B3": 3,
                 "C1": '=SUMIF(A1:A3,"a",B1:B3)', "C2": '=COUNTIF(B1:B3,">1")',
                 "C3": "=VLOOKUP(\"b\",A1:B3,2,FALSE)", "C4": "=ROUND(2.5,0)",
                 "C5": "=INDEX(B1:B3,3)", "C6": "=MATCH(2,B1:B3,0)", "C7": "=AVERAGE(B1:B3)",
                 "C8": "=IF(B1>0,\"pos\",\"neg\")", "C9": "=SUM(A1:B3)",
                 "D1": "=VLOOKUP(\"z\",A1:B3,2,FALSE)", "D2": "=DATE(2026,10,7)",
                 "D3": "=YEAR(D2)", "D4": "=PMT(0.01,12,1000)"})
        self.assertEqual(v["S!C1"], 4.0)
        self.assertEqual(v["S!C2"], 2.0)
        self.assertEqual(v["S!C3"], 2.0)
        self.assertEqual(v["S!C4"], 3.0)              # spreadsheet rounding, not banker's
        self.assertEqual(v["S!C5"], 3.0)
        self.assertEqual(v["S!C6"], 2.0)
        self.assertEqual(v["S!C7"], 2.0)
        self.assertEqual(v["S!C8"], "pos")
        self.assertEqual(v["S!C9"], 6.0)              # SUM skips text in ranges
        self.assertEqual(v["S!D1"], rt.NA)
        self.assertEqual(v["S!D3"], 2026.0)
        self.assertAlmostEqual(v["S!D4"], -88.8488, places=3)

    def test_cycles_give_circ_error(self):
        v = run({"A1": "=A2+1", "A2": "=A1+1", "A3": "=A1*2"})
        self.assertEqual(v["S!A1"], rt.CIRC)
        self.assertEqual(v["S!A3"], rt.CIRC)


class InterpreterTests(unittest.TestCase):
    def test_demo_matches_libreoffice(self):
        """Values cross-checked by recalculating the demo .xlsx in LibreOffice."""
        wb = build_demo_workbook()
        rep = analyze(wb)
        v = Interpreter(wb, rep.symbols, rep.graph).run()
        expected = {"Sales!D6": 67100, "Sales!B10": 6690, "Sales!D10": 370342,
                    "Sales!G10": 415153.382, "Sales!I4": 63961.047, "Sales!D7": 30150,
                    "Summary!B3": 41515.3382, "Summary!B11": 67100, "Summary!B10": 330000}
        for k, x in expected.items():
            self.assertAlmostEqual(v[k], x, places=6, msg=k)
        self.assertEqual(v["Summary!B7"], rt.NAME)
        self.assertEqual(v["Summary!B4"], rt.CIRC)

    def test_incremental_recalc(self):
        wb = make_sales_workbook(30)
        rep = analyze(wb)
        it = Interpreter(wb, rep.symbols, rep.graph)
        it.run()
        touched = it.recalc({"Sales!B2": 999})
        self.assertIn("Sales!D2", touched)
        self.assertNotIn("Sales!D3", touched)
        wb.sheets["Sales"].set(2, 2, 999)
        rep2 = analyze(wb)
        full = Interpreter(wb, rep2.symbols, rep2.graph).run()
        for k in touched:
            self.assertTrue(_same(it.values[k], full[k]), k)


class OptimizerTests(unittest.TestCase):
    def test_passes(self):
        wb = Workbook.from_dict({"S": {"A1": 5, "A2": "=A1*1+0", "A3": "=IF(1>2,A1,A2*(2+3))",
                                       "A4": "=ROUND(PI()*2,2)+A1", "A5": "=RAND()*2"}})
        rep = analyze(wb)
        after = {c.key: c.after for c in rep.optimization.changes}
        self.assertEqual(after["S!A2"], "=A1")
        self.assertEqual(after["S!A3"], "=A2*5")
        self.assertEqual(after["S!A4"], "=6.28+A1")
        self.assertNotIn("S!A5", after)               # volatile: never folded

    def test_text_is_not_simplified(self):
        wb = Workbook.from_dict({"S": {"A1": "abc", "A2": "=A1*1"}})
        rep = analyze(wb)
        self.assertEqual(rep.optimization.changes, [])   # "abc"*1 must stay #VALUE!

    def test_slicing(self):
        from sheetguard.optimizer import Optimizer
        wb = Workbook.from_dict({"S": {"A1": 1, "A2": "=A1", "B1": 2, "B2": "=B1"}})
        rep = analyze(wb)
        self.assertEqual(Optimizer(wb, rep.symbols, rep.graph).slice_for(["S!A2"]),
                         ["S!A1", "S!A2"])


class CodegenTests(unittest.TestCase):
    def check(self, wb, **kw):
        rep = analyze(wb)
        ref = Interpreter(wb, rep.symbols, rep.graph).run()
        cg = CodeGenerator(wb, rep.symbols, rep.graph, rep.types, rep.optimization.asts, **kw)
        out = cg.compile()(prepare_inputs(wb))
        for c in wb.formula_cells():
            self.assertTrue(_same(out[c.key], ref[c.key]), (c.key, out[c.key], ref[c.key]))
        return cg

    def test_demo_buggy_and_fixed(self):
        wb = build_demo_workbook()
        cg = self.check(wb)
        self.assertGreater(cg.stats["fast_cells"], 30)
        self.assertGreaterEqual(cg.stats["cse_reuses"], 6)
        rep = analyze(wb)
        self.check(apply_fixes(wb, rep.fixes))
        self.check(wb, specialize=False, cse=False)

    def test_error_semantics_survive_specialization(self):
        self.check(Workbook.from_dict({"S": {"A1": 0, "A2": 5, "A3": "=A2/A1", "A4": "=A3*2",
                                             "A5": "=A9*3", "A6": "=IFERROR(A4,0)+1"}}))

    @unittest.skipUnless(HAVE_NUMPY, "numpy not installed")
    def test_numpy_backend_and_deopt(self):
        from sheetguard.vectorize import VectorCompiler
        wb = make_sales_workbook(50)
        rep = analyze(wb)
        ref = Interpreter(wb, rep.symbols, rep.graph).run()
        cg = CodeGenerator(wb, rep.symbols, rep.graph, rep.types)
        vc = VectorCompiler(wb, rep.symbols, rep.graph, rep.types, rep.optimization.asts)
        f = vc.compile(fallback=cg.compile())
        self.assertEqual(vc.stats["vector_groups"], 5)
        inputs = prepare_inputs(wb)
        vals = f.materialize(f(inputs))
        for c in wb.formula_cells():
            self.assertTrue(_same(vals[c.key], ref[c.key]), c.key)
        inputs["Sales!C9"] = "oops"                     # non-numeric input -> deoptimize
        out = f(inputs)
        self.assertTrue(out.get("deoptimized"))
        self.assertEqual(out["scalars"]["Sales!D9"], rt.VALUE)


if __name__ == "__main__":
    unittest.main()
