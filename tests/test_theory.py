"""Tests for the syllabus lab: formal grammar (LL(1), SLR(1)) and three-address code."""
import os
import random
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sheetguard.demo import build_demo_workbook
from sheetguard.errors import SheetGuardError
from sheetguard.grammar import EPS, END, LL1, SLR_PARSER, ll1_parse
from sheetguard.parser import parse
from sheetguard.pipeline import analyze
from sheetguard.tac import Quad, basic_blocks, optimize_tac, run_tac, tac_report, translate, \
    triples, workbook_tac_report

CORPUS_OK = [
    "=1", "=A1", "=-A1", "=--A1", "=A1%", "=1+2*3", "=(1+2)*3", "=2^3^2", "=-2^2",
    "=A1&B1=C1", "=A1<=B1", "=SUM(A1:A3)", "=SUM()", "=SUM(A1,B1,C1)", "=Sales!B2",
    "='Q1 Data'!A1:B2", "=SUM(A:A)", "=SUM(2:3)", "=IF(A1>0,\"yes\",\"no\")",
    "=TaxRate*B2", "=#N/A", "=TRUE", "=ROUND(A1*(1+B1),2)", "=SUM(Sales!B2:B9)*(1+TaxRate)",
    "=IFERROR(VLOOKUP($A2,Rates!$A$1:$C$50,3,FALSE),0)", "=((((A1))))",
]
CORPUS_BAD = ["=SUM(A1:A3", "=A1+*2", "=(A1+2))", "=A1 B1", "=SUM(A1:)", "=A1+", "=*2",
              "=SUM(,", "=)"]


def rd_accepts(f):
    try:
        parse(f)
        return True
    except SheetGuardError:
        return False


class GrammarTests(unittest.TestCase):
    def test_grammar_is_ll1_and_slr1(self):
        self.assertEqual(LL1.ll1_table()[1], [])
        self.assertEqual(SLR_PARSER.conflicts, [])

    def test_first_and_follow(self):
        first, follow = LL1.first_sets(), LL1.follow_sets()
        self.assertEqual(first["A'"], {"+", "-", EPS})
        self.assertIn("num", first["E"])
        self.assertNotIn(EPS, first["E"])
        self.assertEqual(follow["E"], {END, ")", ","})
        self.assertEqual(follow["Args"], {")"})
        self.assertIn("%", follow["X"])

    def test_three_parsers_agree(self):
        for f in CORPUS_OK + CORPUS_BAD:
            rd = rd_accepts(f)
            self.assertEqual(ll1_parse(f)["accepted"], rd, f)
            self.assertEqual(SLR_PARSER.parse(f)["accepted"], rd, f)

    def test_demo_workbook_formulas_parse_with_all_three(self):
        wb = build_demo_workbook()
        for c in wb.formula_cells():
            rd = rd_accepts(c.raw)
            self.assertEqual(ll1_parse(c.raw)["accepted"], rd, c.raw)
            self.assertEqual(SLR_PARSER.parse(c.raw)["accepted"], rd, c.raw)

    def test_traces(self):
        r = ll1_parse("=1+2")
        self.assertEqual(r["trace"][-1]["action"], "accept")
        self.assertIn("match num", " ".join(x["action"] for x in r["trace"]))
        s = SLR_PARSER.parse("=1+2")
        self.assertTrue(any(x["action"].startswith("reduce by A → A + T") for x in s["trace"]))


def random_formula(rnd, depth=0):
    """Random arithmetic / IF formulas over A1..D1 for property-based testing."""
    if depth > 3 or rnd.random() < 0.3:
        return rnd.choice(["A1", "B1", "C1", "D1", "2", "0.5", "10", "1", "0"])
    k = rnd.random()
    if k < 0.6:
        op = rnd.choice(["+", "-", "*", "/", "^"])
        return f"({random_formula(rnd, depth + 1)}{op}{random_formula(rnd, depth + 1)})"
    if k < 0.75:
        return f"-{random_formula(rnd, depth + 1)}"
    if k < 0.85:
        return f"{random_formula(rnd, depth + 1)}%"
    return (f"IF({random_formula(rnd, depth + 1)}>{random_formula(rnd, depth + 1)},"
            f"{random_formula(rnd, depth + 1)},{random_formula(rnd, depth + 1)})")


class TACTests(unittest.TestCase):
    def test_translation_and_triples(self):
        code = translate("=B2*C2+B2*C2", "D2")
        self.assertEqual([q.text().strip() for q in code],
                         ["t1 = B2 * C2", "t2 = B2 * C2", "t3 = t1 + t2", "D2 = t3"])
        tr = triples(code)
        self.assertEqual(tr[2]["arg1"], "(0)")
        self.assertEqual(tr[2]["arg2"], "(1)")

    def test_if_creates_basic_blocks(self):
        code = translate("=IF(A1>0,A1,0)", "B1")
        blocks = basic_blocks(code)
        self.assertEqual(len(blocks), 4)
        self.assertEqual(blocks[0]["succ"], ["B2", "B3"])

    def test_optimizations(self):
        r = tac_report("=B2*C2*(1+10%)+B2*C2", "D2")
        text = " ".join(r["log"])
        for p in ("constant folding", "common subexpression", "dead code"):
            self.assertIn(p, text)
        self.assertLess(r["counts"]["after"], r["counts"]["before"])
        self.assertLessEqual(r["target"]["stats"]["instructions"],
                             r["target_unoptimized"]["instructions"])

    def test_optimizer_preserves_meaning_on_random_formulas(self):
        rnd = random.Random(42)
        for _ in range(300):
            f = "=" + random_formula(rnd)
            code = translate(f, "OUT")
            opt, _, _ = optimize_tac([Quad(q.op, q.arg1, q.arg2, q.result) for q in code])
            for _ in range(3):
                env = {x: rnd.choice([0.0, 1.0, -2.0, 3.5, 7.0]) for x in ("A1", "B1", "C1", "D1")}
                a, b = run_tac(code, env)["OUT"], run_tac(opt, env)["OUT"]
                if isinstance(a, float) and isinstance(b, float):
                    self.assertAlmostEqual(a, b, places=6, msg=f)
                else:
                    self.assertEqual(str(a), str(b), f)

    def test_workbook_tac(self):
        rep = analyze(build_demo_workbook())
        w = workbook_tac_report(rep.workbook, rep.symbols, rep.graph)
        self.assertLess(w["after"], w["before"])
        self.assertGreaterEqual(w["pass_counts"].get("common subexpression", 0), 7)
        self.assertIn("    Summary!B10 = 330000", w["optimized"])


if __name__ == "__main__":
    unittest.main()
