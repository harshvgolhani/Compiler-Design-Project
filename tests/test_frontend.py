"""Tests for phases 1-2: lexer and recursive-descent parser."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sheetguard.ast_nodes import BinOp, CellRef, FuncCall, Missing, NameRef, RangeRef, UnaryOp
from sheetguard.errors import LexError, ParseError
from sheetguard.lexer import tokenize
from sheetguard.parser import parse
from sheetguard.tokens import T


def types(formula):
    return [t.type for t in tokenize(formula)][:-1]   # drop EOF


class LexerTests(unittest.TestCase):
    def test_basic_stream(self):
        self.assertEqual(types("=SUM(A1:B2)*1.5"),
                         [T.FUNC, T.LPAREN, T.CELL, T.COLON, T.CELL, T.RPAREN, T.OP, T.NUMBER])

    def test_identifier_classification_uses_lookahead(self):
        toks = tokenize("=LOG10(LOG10)")
        self.assertEqual(toks[0].type, T.FUNC)      # followed by '('
        self.assertEqual(toks[2].type, T.CELL)      # column LOG, row 10

    def test_sheet_prefixes(self):
        toks = tokenize("='Q1 Data'!A1+Sales!B2")
        self.assertEqual((toks[0].type, toks[0].value), (T.SHEET, "Q1 Data"))
        self.assertEqual((toks[3].type, toks[3].value), (T.SHEET, "Sales"))

    def test_absolute_refs_and_names(self):
        toks = tokenize("=$A$1*TaxRate")
        self.assertEqual(toks[0].type, T.CELL)
        self.assertEqual(toks[2].type, T.NAME)

    def test_string_with_escaped_quote(self):
        toks = tokenize('="say ""hi"""')
        self.assertEqual(toks[0].value, 'say "hi"')

    def test_numbers(self):
        for src, val in (("=42", "42"), ("=3.14", "3.14"), ("=.5", ".5"), ("=1e-3", "1e-3")):
            self.assertEqual(tokenize(src)[0].value, val)

    def test_whole_column_and_row_ranges(self):
        self.assertEqual(types("=SUM(A:C)")[2], T.COL_RANGE)
        self.assertEqual(types("=SUM(2:5)")[2], T.ROW_RANGE)

    def test_two_char_operators(self):
        vals = [t.value for t in tokenize("=A1<=B1<>C1>=D1") if t.type is T.OP]
        self.assertEqual(vals, ["<=", "<>", ">="])

    def test_error_literals(self):
        self.assertEqual(tokenize("=#DIV/0!")[0].value, "#DIV/0!")
        self.assertEqual(tokenize("=#N/A")[0].value, "#N/A")

    def test_positions_point_into_source(self):
        toks = tokenize("=A1 + B22")
        self.assertEqual((toks[2].pos, toks[2].end), (6, 9))

    def test_lex_errors(self):
        for bad in ('="unterminated', "=12abc", "=A1 @ B1", "=#BOGUS", "=$A$"):
            with self.assertRaises(LexError, msg=bad):
                tokenize(bad)


class ParserTests(unittest.TestCase):
    def test_precedence(self):
        tree = parse("=1+2*3")
        self.assertIsInstance(tree, BinOp)
        self.assertEqual(tree.op, "+")
        self.assertEqual(tree.right.op, "*")

    def test_left_associativity(self):
        self.assertEqual(parse("=10-4-3").left.op, "-")
        self.assertEqual(parse("=2^3^2").left.op, "^")       # Excel: (2^3)^2

    def test_unary_minus_binds_tighter_than_power(self):
        tree = parse("=-2^2")
        self.assertEqual(tree.op, "^")
        self.assertIsInstance(tree.left, UnaryOp)

    def test_comparison_is_lowest(self):
        tree = parse("=A1&B1=C1")
        self.assertEqual(tree.op, "=")

    def test_references(self):
        r = parse("=Sales!$B$2:B9")
        self.assertIsInstance(r, RangeRef)
        self.assertEqual((r.sheet, r.col1, r.row1, r.row2, r.abs_col1, r.abs_col2),
                         ("Sales", 2, 2, 9, True, False))
        self.assertEqual(r.shape, (8, 1))
        self.assertIsInstance(parse("=TaxRate"), NameRef)
        self.assertIsInstance(parse("=A1"), CellRef)

    def test_function_calls(self):
        f = parse("=IF(A1>0,SUM(B1:B3),)")
        self.assertIsInstance(f, FuncCall)
        self.assertEqual(len(f.args), 3)
        self.assertIsInstance(f.args[2], Missing)
        self.assertEqual(parse("=PI()").args, [])

    def test_round_trip_unparse(self):
        for src in ("=SUM(Sales!B2:B9)*(1+TaxRate)", "=IF(A1>=10%,\"ok\",-2^2)",
                    "=1-(2-3)", "=2^(3^2)", "={1,2;3,-4}", "='Q1 Data'!A1:A3",
                    "=IFERROR(VLOOKUP($A2,Rates!$A$1:$C$50,3,FALSE),0)", "=SUM(A:A)+SUM(2:3)"):
            self.assertEqual("=" + parse(src).unparse(), src)

    def test_redundant_parens_removed(self):
        self.assertEqual(parse("=((A1))+(B1*C1)").unparse(), "A1+B1*C1")

    def test_spans(self):
        tree = parse("=SUM(A1:A3)+B1")
        self.assertEqual(tree.left.span, (1, 11))
        self.assertEqual(tree.right.span, (12, 14))

    def test_syntax_errors_have_positions(self):
        cases = {
            "=SUM(A1:A3": 10,      # missing ')'
            "=A1+*2": 4,           # operator without operand
            "=(A1+2))": 7,         # unbalanced
            "=A1 B1": 4,           # missing operator
            "=SUM(A1:)": 8,        # incomplete range
            "=": 1,                # empty
        }
        for src, pos in cases.items():
            with self.assertRaises(ParseError, msg=src) as cm:
                parse(src)
            self.assertEqual(cm.exception.pos, pos, src)
            self.assertIn("^", cm.exception.pretty())


class RecoveryTests(unittest.TestCase):
    def test_phrase_level_repair(self):
        from sheetguard.parser import repair
        cases = {"=SUM(A1:A3": "=SUM(A1:A3)", "=A1+*2": "=A1+2", "=(A1+2))": "=(A1+2)",
                 '="abc': '="abc"', "=SUM(IF(A1>0,1,2)": "=SUM(IF(A1>0,1,2))",
                 '=COUNTIF(J2:J11,"S)': '=COUNTIF(J2:J11,"S")'}
        for bad, good in cases.items():
            try:
                parse(bad)
            except (LexError, ParseError) as e:
                self.assertEqual(repair(bad, e), good)


if __name__ == "__main__":
    unittest.main()
