"""Tests for the JSON API and the HTTP server used by both front-ends."""
import json
import os
import sys
import threading
import unittest
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sheetguard import api
from sheetguard.demo import build_demo_workbook

try:
    import openpyxl  # noqa: F401
    HAS_XL = True
except ImportError:
    HAS_XL = False


class ApiTests(unittest.TestCase):
    def test_analyze_demo(self):
        d = api.analyze_payload({"demo": True})
        self.assertEqual(d["counts"]["error"], 7)
        self.assertIn("Best region revenue", d["summary"]["text"])
        self.assertIn("Sales!D6", d["cells"])
        self.assertIn("tree", d["cells"]["Sales!D6"])
        self.assertEqual(d["health"]["grade"], "F")
        self.assertTrue(d["impact"]["changes"])
        json.dumps(d)                                   # must be JSON-serialisable

    def test_analyze_workbook_json_round_trip(self):
        wb = build_demo_workbook().to_json()
        d = api.analyze_payload({"workbook": wb, "return_workbook": True})
        self.assertEqual(d["counts"], api.analyze_payload({"demo": True})["counts"])
        self.assertEqual(d["source_workbook"]["name"], wb["name"])

    def test_parse_in_context(self):
        d = api.parse_payload({"formula": "=SUM(Sales!B2:B9)*(1+TaxRate)", "demo": True})
        self.assertEqual(d["value"], "8743.8")
        self.assertEqual(d["diagnostics"], [])
        e = api.parse_payload({"formula": "=SUM(A1:A3"})
        self.assertEqual(e["error"]["phase"], "syntax")

    @unittest.skipUnless(HAS_XL, "openpyxl not installed")
    def test_export(self):
        x = api.export_payload({"demo": True})
        self.assertTrue(x["xlsx_base64"].startswith("UEsDB"))   # a zip (xlsx) file

    def test_compile(self):
        c = api.compile_payload({"demo": True, "target": "Summary!B6"})
        self.assertIn("def recalc(I):", c["source"])


class TheoryApiTests(unittest.TestCase):
    def test_grammar_and_theory(self):
        g = api.grammar_payload()
        self.assertTrue(g["ll1"]["is_ll1"])
        self.assertTrue(g["slr"]["is_slr1"])
        t = api.theory_payload({"formula": "=IF(A1>0,A1*2,0)", "demo": True})
        self.assertTrue(t["ll1"]["accepted"] and t["slr"]["accepted"])
        self.assertEqual(len(t["tac"]["blocks"]), 4)
        self.assertIn("workbook_tac", t)
        json.dumps(t)


class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from http.server import ThreadingHTTPServer
        from sheetguard.server import Handler
        Handler.log_message = lambda *a, **k: None
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.port = cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def call(self, path, body=None, headers=None):
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}",
                                     data=None if body is None else json.dumps(body).encode(),
                                     headers=headers or {}, method="GET" if body is None else "POST")
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            with e:
                return e.code, e.read()

    def test_health_and_static(self):
        code, body = self.call("/api/health")
        self.assertEqual(code, 200)
        self.assertTrue(json.loads(body)["ok"])
        code, body = self.call("/")
        self.assertEqual(code, 200)
        self.assertIn(b"SheetGuard", body)

    def test_analyze_over_http(self):
        code, body = self.call("/api/analyze", {"demo": True})
        self.assertEqual(code, 200)
        self.assertEqual(json.loads(body)["counts"]["error"], 7)

    def test_api_key(self):
        os.environ["SHEETGUARD_KEY"] = "secret"
        try:
            self.assertEqual(self.call("/api/analyze", {"demo": True})[0], 401)
            self.assertEqual(self.call("/api/analyze", {"demo": True},
                                       {"X-SheetGuard-Key": "secret"})[0], 200)
        finally:
            del os.environ["SHEETGUARD_KEY"]

    def test_bad_requests(self):
        self.assertEqual(self.call("/api/nope", {})[0], 404)
        self.assertEqual(self.call("/api/analyze", {})[0], 400)


if __name__ == "__main__":
    unittest.main()
