"""Tests for phases 3-4: symbol table and dependency graph."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sheetguard.demo import build_demo_workbook
from sheetguard.pipeline import analyze
from sheetguard.workbook import Workbook


def codes(report, cell=None):
    return sorted(d.code for d in report.diagnostics if cell is None or d.cell == cell)


class SymbolTableTests(unittest.TestCase):
    def test_named_range_resolution_and_scoping(self):
        wb = Workbook.from_dict({"In": {"A1": 0.2}, "S": {"A1": 100, "B1": "=A1*Rate"}},
                                names={"Rate": "In!$A$1"})
        rep = analyze(wb)
        self.assertEqual(rep.diagnostics, [])
        self.assertIn("In!A1", rep.graph.precedents("S!B1"))
        self.assertEqual(rep.symbols.links[("S", "In")], 1)

    def test_sheet_scope_shadows_workbook_scope(self):
        wb = Workbook.from_dict({"A": {"A1": 1, "A2": 2, "B1": "=X"}})
        wb.define_name("X", "A!$A$1")
        wb.define_name("X", "A!$A$2", scope="A")
        rep = analyze(wb)
        self.assertEqual(rep.graph.precedents("A!B1"), ["A!A2"])

    def test_unknown_function_with_suggestion(self):
        rep = analyze(Workbook.from_dict({"S": {"A1": "=SUMM(B1:B2)"}}))
        d = rep.diagnostics[0]
        self.assertEqual(d.code, "SG101")
        self.assertEqual(d.suggested_formula, "=SUM(B1:B2)")

    def test_arity_check(self):
        rep = analyze(Workbook.from_dict({"S": {"A1": "=ROUND(B1)", "A2": "=IF(B1,1,2,3)"}}))
        self.assertEqual([c for c in codes(rep) if c.startswith("SG1")], ["SG102", "SG102", "SG105", "SG105"])

    def test_undefined_name_and_missing_sheet(self):
        rep = analyze(Workbook.from_dict({"S": {"A1": "=Nope*2", "A2": "=Ghost!A1"}}))
        self.assertEqual(codes(rep), ["SG103", "SG104"])

    def test_syntax_errors_become_diagnostics(self):
        rep = analyze(Workbook.from_dict({"S": {"A1": "=SUM(B1:B2", "A2": '="open'}}))
        self.assertEqual(codes(rep), ["SG001", "SG002"])


class GraphTests(unittest.TestCase):
    def test_topological_levels(self):
        wb = Workbook.from_dict({"S": {"A1": 1, "A2": "=A1+1", "A3": "=A2*2", "B1": "=A1+A3"}})
        g = analyze(wb).graph
        self.assertEqual(g.formula_levels(), [["S!A2"], ["S!A3"], ["S!B1"]])
        order = g.recalc_order()
        self.assertLess(order.index("S!A3"), order.index("S!B1"))

    def test_cycle_detection_and_blocking(self):
        wb = Workbook.from_dict({"S": {"A1": "=B1+1", "B1": "=C1+1", "C1": "=A1+1",
                                       "D1": "=C1*2", "E1": "=E1+1", "F1": 5}})
        rep = analyze(wb)
        cycles = sorted(sorted(c) for c in rep.graph.cycles)
        self.assertEqual(cycles, [["S!A1", "S!B1", "S!C1"], ["S!E1"]])
        self.assertIn("S!D1", rep.graph.blocked)          # downstream of a cycle
        self.assertEqual(codes(rep).count("SG201"), 4)

    def test_ranges_expand_to_existing_cells(self):
        wb = Workbook.from_dict({"S": {"A1": 1, "A3": 3, "B1": "=SUM(A:A)"}})
        g = analyze(wb).graph
        self.assertEqual(g.precedents("S!B1", transitive=False), ["S!A1", "S!A3"])

    def test_impact_analysis(self):
        wb = Workbook.from_dict({"S": {"A1": 1, "A2": "=A1", "A3": "=A2", "A4": 7}})
        g = analyze(wb).graph
        self.assertEqual(g.dependents("S!A1"), ["S!A2", "S!A3"])
        self.assertEqual(g.dependents("S!A4"), [])


class DemoWorkbookTests(unittest.TestCase):
    def test_demo_finds_planted_front_end_bugs(self):
        rep = analyze(build_demo_workbook())
        self.assertIn("SG201", codes(rep, "Summary!B4"))
        self.assertIn("SG101", codes(rep, "Summary!B7"))
        self.assertIn("SG103", codes(rep, "Summary!B9"))
        self.assertIn("Summary!B8", rep.graph.blocked)

    def test_json_round_trip(self):
        wb = build_demo_workbook()
        wb2 = Workbook.from_json(wb.to_json())
        self.assertEqual(wb.stats(), wb2.stats())
        self.assertEqual(wb2.cell("Sales!D6").raw, "=B6*C5")


if __name__ == "__main__":
    unittest.main()
