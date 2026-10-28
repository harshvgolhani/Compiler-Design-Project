"""Tests for the extensions: JS/SQL back-ends, semantic diff, fuzz testing."""
import os
import shutil
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sheetguard.bench import make_sales_workbook
from sheetguard.demo import build_demo_workbook
from sheetguard.diff import ast_changes, diff_demo, diff_workbooks
from sheetguard.fixes import apply_fixes
from sheetguard.fuzz import fuzz_workbook
from sheetguard.multitarget import JSGenerator, SQLGenerator, verify_js, verify_sql
from sheetguard.parser import parse
from sheetguard.pipeline import analyze
from sheetguard.workbook import Workbook


def fixed_demo():
    wb = build_demo_workbook()
    return apply_fixes(wb, analyze(wb).fixes)


class MultiTargetTests(unittest.TestCase):
    def test_sql_matches_interpreter_on_fixed_demo_and_synthetic_model(self):
        for wb in (fixed_demo(), make_sales_workbook(40)):
            rep = analyze(wb)
            v = verify_sql(wb, rep, SQLGenerator(wb, rep.symbols, rep.graph).generate())
            self.assertTrue(v["ok"], v)

    def test_sql_differences_on_buggy_demo_are_all_explained(self):
        wb = build_demo_workbook()
        rep = analyze(wb)
        v = verify_sql(wb, rep, SQLGenerator(wb, rep.symbols, rep.graph).generate())
        self.assertTrue(v["ok_except_flagged"])
        self.assertGreater(v["matching"], 40)

    def test_sql_shape(self):
        wb = Workbook.from_dict({"S": {"A1": 4, "A2": "=A1*2", "A3": "=A2+A1", "A4": "=IFERROR(A1/0,7)"}})
        rep = analyze(wb)
        src = SQLGenerator(wb, rep.symbols, rep.graph).generate()
        self.assertIn("WITH inputs AS", src)
        self.assertIn("COALESCE", src)
        self.assertTrue(verify_sql(wb, rep, src)["ok"])

    @unittest.skipUnless(shutil.which("node"), "Node.js not installed")
    def test_js_matches_interpreter_including_errors(self):
        for wb in (build_demo_workbook(), fixed_demo()):
            rep = analyze(wb)
            v = verify_js(wb, rep, JSGenerator(wb, rep.symbols, rep.graph).generate())
            self.assertTrue(v["ok"], v)


class DiffTests(unittest.TestCase):
    def test_ast_level_changes(self):
        ch = ast_changes(parse("=SUM(B2:B8)*TaxRate"), parse("=SUM(B2:B9)*TaxRate"))
        self.assertEqual([(c["kind"], c["from"], c["to"]) for c in ch],
                         [("range resized", "B2:B8", "B2:B9")])
        ch = ast_changes(parse("=E8*0.18"), parse("=E8*TaxRate"))
        self.assertEqual(ch[0]["kind"], "constant → reference")
        self.assertEqual(ast_changes(parse("=(A1+B1)"), parse("=A1+B1")), [])

    def test_demo_diff(self):
        d = diff_demo()
        st = {c["cell"]: c["status"] for c in d["cells"]}
        self.assertEqual(st["Summary!B3"], "cosmetic")
        self.assertEqual(st["Sales!D9"], "value → formula")
        self.assertEqual(len(d["issues"]["introduced"]), 0)
        self.assertGreaterEqual(len(d["issues"]["resolved"]), 13)
        self.assertTrue(any(r["cell"] == "Summary!B11" for r in d["ripple"]))

    def test_moved_formula(self):
        old = Workbook.from_dict({"S": {"A1": 1, "B1": 2, "C1": "=A1+B1"}})
        new = Workbook.from_dict({"S": {"A1": 1, "B1": 2, "C2": "=A2+B2"}})
        d = diff_workbooks(old, new)
        self.assertEqual({c["cell"]: c["status"] for c in d["cells"]}["S!C2"], "moved")


class FuzzTests(unittest.TestCase):
    def test_finds_the_two_latent_division_bugs(self):
        wb = build_demo_workbook()
        r = fuzz_workbook(wb, analyze(wb))
        cells = {f["cell"]: f for f in r["plausible"]}
        self.assertEqual(set(cells), {"Summary!B14", "Summary!B15"})
        self.assertIn("Discount", cells["Summary!B14"]["witness"])
        self.assertTrue(cells["Summary!B14"]["suggested_formula"].startswith("=IF(Sales!E10=0"))

    def test_guarded_formula_survives_fuzzing(self):
        wb = Workbook.from_dict({"S": {"A1": 5, "A2": 2, "A3": "=IF(A2=0,0,A1/A2)", "A4": "=A1/A2"}})
        r = fuzz_workbook(wb, analyze(wb))
        self.assertEqual([f["cell"] for f in r["plausible"]], ["S!A4"])


if __name__ == "__main__":
    unittest.main()
