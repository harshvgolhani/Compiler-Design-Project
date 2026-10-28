"""Tests for phases 5-6: type/unit checker, smell detector, auto-fix."""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sheetguard.demo import build_demo_workbook
from sheetguard.fixes import apply_fixes, export_xlsx
from sheetguard.parser import parse
from sheetguard.pipeline import analyze
from sheetguard.transforms import r1c1, shift
from sheetguard.typecheck import parse_numeric_text
from sheetguard.workbook import Workbook

USD = '"$"#,##0.00'


def codes(rep, cell=None):
    return sorted(d.code for d in rep.diagnostics if cell is None or d.cell == cell)


class TransformTests(unittest.TestCase):
    def test_r1c1_makes_copied_formulas_equal(self):
        self.assertEqual(r1c1(parse("=B6*C6"), 4, 6), r1c1(parse("=B7*C7"), 4, 7))
        self.assertNotEqual(r1c1(parse("=B6*C5"), 4, 6), r1c1(parse("=B7*C7"), 4, 7))

    def test_shift_respects_absolute_refs(self):
        self.assertEqual(shift(parse("=A1*$B$1+SUM(C1:C3)"), 1, 2).unparse(),
                         "B3*$B$1+SUM(D3:D5)")
        self.assertEqual(shift(parse("=A1"), 0, -5).unparse(), "#REF!")


class TypeCheckerTests(unittest.TestCase):
    def wb(self, cells, fmts=None, names=None):
        return analyze(Workbook.from_dict({"S": cells}, names=names, formats=fmts))

    def test_unit_inference(self):
        rep = self.wb({"A1": 100, "A2": 0.1, "A3": "=A1*(1+A2)", "A4": "=A1/A1"},
                      {"S!A1": USD, "S!A2": "0%"})
        self.assertEqual(str(rep.types["S!A3"]), "Number[currency]")
        self.assertEqual(str(rep.types["S!A4"]), "Number")

    def test_currency_plus_percent_is_flagged_with_fix(self):
        rep = self.wb({"A1": 100, "A2": 0.1, "A3": "=A1+A2"}, {"S!A1": USD, "S!A2": "0%"})
        d = next(d for d in rep.diagnostics if d.code == "SG302")
        self.assertEqual(d.suggested_formula, "=A1*(1+A2)")

    def test_money_times_money(self):
        rep = self.wb({"A1": 5, "A2": 6, "A3": "=A1*A2"}, {"S!A1": USD, "S!A2": USD})
        self.assertIn("SG302", codes(rep, "S!A3"))

    def test_text_in_arithmetic(self):
        rep = self.wb({"A1": "=\"abc\"*2"})
        self.assertIn("SG301", codes(rep))

    def test_number_stored_as_text(self):
        rep = self.wb({"A1": "$1,200.50", "A2": "=A1*2"})
        d = next(d for d in rep.diagnostics if d.code == "SG305")
        self.assertEqual(d.cell, "S!A1")
        self.assertEqual(d.suggested_formula, "1200.5")
        self.assertAlmostEqual(parse_numeric_text("12%"), 0.12)

    def test_division_by_empty_and_zero(self):
        rep = self.wb({"A1": 5, "A2": "=A1/B9", "A3": "=A1/0"})
        self.assertEqual(codes(rep), ["SG304", "SG304"])

    def test_vlookup_column_out_of_range(self):
        rep = self.wb({"A1": 1, "B1": 2, "C1": "=VLOOKUP(1,A1:B5,3,FALSE)"})
        self.assertIn("SG303", codes(rep, "S!C1"))

    def test_sumifs_shape_mismatch(self):
        rep = self.wb({"A1": 1, "A2": "=SUMIFS(B1:B10,C1:C9,\"x\")"})
        self.assertIn("SG303", codes(rep, "S!A2"))


class SmellTests(unittest.TestCase):
    def column(self, formulas):
        cells = {f"B{r}": r for r in range(1, 7)}
        cells.update({f"C{r}": f for r, f in zip(range(1, 7), formulas)})
        return analyze(Workbook.from_dict({"S": cells}))

    def test_copy_paste_error(self):
        rep = self.column(["=B1*2", "=B2*2", "=B3*2", "=B2*2", "=B5*2", "=B6*2"])
        d = next(d for d in rep.diagnostics if d.code == "SG401")
        self.assertEqual((d.cell, d.suggested_formula), ("S!C4", "=B4*2"))

    def test_overwritten_formula(self):
        rep = self.column(["=B1*2", "=B2*2", "=B3*2", 8, "=B5*2", "=B6*2"])
        d = next(d for d in rep.diagnostics if d.code == "SG404")
        self.assertEqual((d.cell, d.suggested_formula), ("S!C4", "=B4*2"))

    def test_consistent_column_is_clean(self):
        rep = self.column([f"=B{r}*2" for r in range(1, 7)])
        self.assertEqual(rep.diagnostics, [])

    def test_off_by_one(self):
        rep = analyze(Workbook.from_dict({"S": {"A1": "Qty", "A2": 1, "A3": 2, "A4": 3,
                                                "A5": "=SUM(A2:A3)"}}))
        d = next(d for d in rep.diagnostics if d.code == "SG402")
        self.assertEqual(d.suggested_formula, "=SUM(A2:A4)")

    def test_double_counting(self):
        rep = analyze(Workbook.from_dict({"S": {"A1": 1, "A2": 2, "A3": "=SUM(A1:A2)",
                                                "A4": "=AVERAGE(A1:A3)"}}))
        d = next(d for d in rep.diagnostics if d.code == "SG405")
        self.assertEqual(d.suggested_formula, "=AVERAGE(A1:A2)")

    def test_magic_number_matching_an_input(self):
        rep = analyze(Workbook.from_dict({"In": {"A1": 0.18}, "S": {"A1": 10, "B1": "=A1*0.18"}},
                                         names={"Tax": "In!$A$1"}))
        d = next(d for d in rep.diagnostics if d.code == "SG403")
        self.assertEqual(d.suggested_formula, "=A1*Tax")


class DemoEndToEndTests(unittest.TestCase):
    EXPECTED = {
        "Sales!D6": "SG401", "Sales!F8": "SG403", "Sales!D9": "SG404", "Sales!B10": "SG402",
        "Sales!I4": "SG302", "Sales!C7": "SG305", "Sales!D11": "SG405", "Summary!B4": "SG201",
        "Summary!B5": "SG201", "Summary!B7": "SG101", "Summary!B9": "SG103",
        "Summary!B8": "SG304", "Summary!B13": "SG002",
        # optimizer findings: dead inputs that are *symptoms* of the bugs above,
        # a stale assumption, and a constant expression
        "Sales!C6": "SG502", "Sales!B9": "SG502", "Sales!C9": "SG502", "Inputs!B6": "SG502",
        "Summary!B10": "SG501",
    }

    def test_every_planted_bug_is_found(self):
        rep = analyze(build_demo_workbook())
        for cell, code in self.EXPECTED.items():
            self.assertIn(code, codes(rep, cell), cell)

    def test_no_false_positives_on_clean_cells(self):
        rep = analyze(build_demo_workbook())
        flagged = {d.cell for d in rep.diagnostics}
        self.assertEqual(flagged - set(self.EXPECTED), set())

    def test_autofix_removes_all_fixable_issues(self):
        rep = analyze(build_demo_workbook())
        self.assertEqual(len(rep.fixes), 10)
        after = analyze(apply_fixes(rep.workbook, rep.fixes))
        self.assertEqual(sorted({d.code for d in after.diagnostics}),
                         ["SG201", "SG304", "SG403", "SG501", "SG502"])
        self.assertEqual([d.cell for d in after.diagnostics if d.code == "SG502"], ["Inputs!B6"])

    def test_xlsx_round_trip(self):
        try:
            import openpyxl  # noqa: F401
        except ImportError:
            self.skipTest("openpyxl not installed")
        wb = build_demo_workbook()
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "demo.xlsx")
            export_xlsx(wb, p)
            rep = analyze(Workbook.from_xlsx(p))
            self.assertEqual(rep.counts(), analyze(build_demo_workbook()).counts())


if __name__ == "__main__":
    unittest.main()
