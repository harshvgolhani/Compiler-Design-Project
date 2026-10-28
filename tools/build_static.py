"""Build the hosted demo: one self-contained HTML page that replays the real
compiler's recorded output (no server needed).

    python tools/build_static.py          ->  dist/SheetGuard_Demo.html
"""
import datetime
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from sheetguard import __version__, api  # noqa: E402
from sheetguard.demo import build_demo_workbook  # noqa: E402
from sheetguard.pipeline import analyze  # noqa: E402


def read(*p):
    with open(os.path.join(ROOT, *p), encoding="utf-8") as fh:
        return fh.read()


def main():
    wb = build_demo_workbook()
    base = api.analyze_payload({"demo": True, "return_workbook": True})
    snap = {"version": __version__, "analyze": {"": base}, "compile": {}, "parse": {}, "bench": {},
            "captured": datetime.date.today().strftime("%d %b %Y")}
    rep = analyze(wb)
    fixes = rep.fixes

    def with_fixes(selected):
        w = json.loads(json.dumps(base["source_workbook"]))
        from sheetguard.workbook import Workbook
        wbk = Workbook.from_json(w)
        for f in selected:
            sh, _, a = f.cell.rpartition("!")
            from sheetguard.refs import parse_cell
            c, r, _, _ = parse_cell(a)
            old = wbk.sheets[sh].get(c, r)
            wbk.sheets[sh].set(c, r, f.after, fmt=old.fmt if old else "")
        return api.analyze_payload({"workbook": wbk.to_json(), "return_workbook": True})

    for f in fixes:                                         # every single fix
        snap["analyze"][f.cell] = with_fixes([f])
    snap["analyze"]["|".join(sorted(f.cell for f in fixes))] = with_fixes(fixes)   # apply all
    print("analyze snapshots:", len(snap["analyze"]))

    snap["compile"]["py:"] = api.compile_payload({"demo": True})
    snap["compile"]["numpy:"] = api.compile_payload({"demo": True, "numpy": True})
    for n in base["graph"]["nodes"]:
        if n["is_formula"] and n["out_degree"] == 0 and n["id"].startswith("Summary!"):
            snap["compile"]["py:" + n["id"]] = api.compile_payload({"demo": True, "target": n["id"]})
    print("compile snapshots:", len(snap["compile"]))

    js = read("web", "app.js")
    examples = json.loads("[" + re.search(r"const PLAY_EXAMPLES = \[(.*?)\];", js).group(1) + "]")
    examples += ["=SUM(Sales!D2:D9"]
    for f in list(examples):
        r = api.parse_payload({"formula": f, "demo": True, "sheet": "Sales"})
        snap["parse"][f] = r
        if r.get("repair"):
            snap["parse"][r["repair"]] = api.parse_payload({"formula": r["repair"], "demo": True,
                                                            "sheet": "Sales"})
    print("parse snapshots:", len(snap["parse"]))

    snap["grammar"] = api.grammar_payload()
    snap["theory"] = {}
    lab = json.loads("[" + re.search(r"const LAB_EXAMPLES = \[(.*?)\];", js, re.S).group(1) + "]")
    for f in lab:
        for n in (2, 4, 8):
            snap["theory"][f"{f}|{n}"] = api.theory_payload(
                {"formula": f, "nregs": n, "demo": True, "sheet": "Sales",
                 "workbook_tac": f == lab[0] and n == 4})
    print("theory snapshots:", len(snap["theory"]))

    snap["ext"] = {"multitarget": api.multitarget_payload({"demo": True}),
                   "diff": api.diff_payload({"demo": True}),
                   "fuzz": api.fuzz_payload({"demo": True})}

    for rows in (500, 2000, 5000):
        snap["bench"][str(rows)] = api.bench_payload({"rows": rows, "repeat": 3})
        print("bench", rows, [r["speedup"] for r in snap["bench"][str(rows)]["results"]])

    data = json.dumps(snap, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    css = read("web", "app.css")
    page = f"""<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>SheetGuard Demo</title>
<meta name="description" content="SheetGuard: a compiler-based verification engine for spreadsheet formulas (BCSE307P Compiler Design Lab, VIT Vellore)">
<style>
{css}
</style>
<script type="application/json" id="sg-snapshot">{data}</script>
<script>
window.SG_MODE = "web";
window.SG_SNAPSHOT = JSON.parse(document.getElementById("sg-snapshot").textContent);
</script>
<script>
{js.replace("</script>", "<\\/script>")}
</script>
"""
    os.makedirs(os.path.join(ROOT, "dist"), exist_ok=True)
    out = os.path.join(ROOT, "dist", "SheetGuard_Demo.html")
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(page)
    print(f"wrote {out}  ({len(page) / 1024 / 1024:.2f} MB)")


if __name__ == "__main__":
    main()
