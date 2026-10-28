/* SheetGuard UI — one code base for three shells:
 *   SG_MODE = "web"      standalone app served by `python -m sheetguard serve`
 *   SG_MODE = "sidebar"  Google Sheets sidebar (talks to Apps Script via google.script.run)
 *   SG_MODE = "graph"    full-screen dependency-graph dialog inside Google Sheets
 */
(function boot() {
  "use strict";
  // the hosted/offline single-file build may run this before <body> exists
  if (!document.body) {
    if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
    else setTimeout(boot, 0);
    return;
  }
  const MODE = window.SG_MODE || "web";
  const IS_GS = typeof google !== "undefined" && google.script && google.script.run;
  if (MODE !== "web") document.body.classList.add(MODE);

  // ------------------------------------------------------------------ DOM helpers
  function h(tag, attrs, ...kids) {
    const el = document.createElement(tag);
    setAttrs(el, attrs);
    append(el, kids);
    return el;
  }
  const SVGNS = "http://www.w3.org/2000/svg";
  function s(tag, attrs, ...kids) {
    const el = document.createElementNS(SVGNS, tag);
    setAttrs(el, attrs, true);
    append(el, kids);
    return el;
  }
  function setAttrs(el, attrs, svg) {
    if (!attrs) return;
    for (const k in attrs) {
      const v = attrs[k];
      if (v == null || v === false) continue;
      if (k === "class") svg ? el.setAttribute("class", v) : (el.className = v);
      else if (k === "html") el.innerHTML = v;
      else if (k.slice(0, 2) === "on") el.addEventListener(k.slice(2), v);
      else if (k === "style" && typeof v === "object") Object.assign(el.style, v);
      else el.setAttribute(k, v === true ? "" : v);
    }
  }
  function append(el, kids) {
    for (const k of kids.flat(Infinity)) {
      if (k == null || k === false) continue;
      el.append(k.nodeType ? k : document.createTextNode(String(k)));
    }
  }
  const esc = (t) => String(t == null ? "" : t).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  const fmtInt = (n) => (n == null ? "" : Number(n).toLocaleString("en-US"));
  function copyText(text, msg) {
    try {
      const p = navigator.clipboard && navigator.clipboard.writeText(text);
      if (p && p.then) p.then(() => toast(msg), () => toast("Copy blocked by the browser — select the text instead", true));
      else toast(msg);
    } catch (e) { toast("Copy blocked by the browser — select the text instead", true); }
  }
  function toast(msg, err) {
    const t = h("div", { class: "toast" + (err ? " err" : "") }, msg);
    document.body.append(t);
    setTimeout(() => t.remove(), err ? 6000 : 2600);
  }

  // ------------------------------------------------------------------ cell refs
  function colToIdx(c) { let n = 0; for (const ch of c.toUpperCase()) n = n * 26 + ch.charCodeAt(0) - 64; return n; }
  function idxToCol(n) { let s = ""; while (n > 0) { const r = (n - 1) % 26; s = String.fromCharCode(65 + r) + s; n = Math.floor((n - 1) / 26); } return s; }
  function splitKey(key) {
    const i = key.lastIndexOf("!");
    const sheet = key.slice(0, i), a1 = key.slice(i + 1);
    const m = /^([A-Z]+)(\d+)$/.exec(a1);
    return { sheet, a1, col: m ? colToIdx(m[1]) : 0, row: m ? +m[2] : 0 };
  }

  // ------------------------------------------------------------------ transport
  const SNAP = window.SG_SNAPSHOT || null;      // hosted demo: recorded responses of the real compiler
  function snapshotApi(name, payload) {
    const P = payload || {};
    return new Promise((resolve, reject) => setTimeout(() => {
      if (name === "analyze") {
        const key = P.demo ? "" : Array.from(S.fixed).sort().join("|");
        const r = SNAP.analyze[key];
        if (r) return resolve(JSON.parse(JSON.stringify(r)));
        return reject(new Error("The hosted demo has recorded results for single fixes and for “Apply all”. Run SheetGuard locally to combine fixes freely."));
      }
      if (name === "compile") { const r = SNAP.compile[(P.numpy ? "numpy" : "py") + ":" + (P.numpy ? "" : P.target || "")]; return r ? resolve(r) : reject(new Error("Not recorded in the hosted demo.")); }
      if (name === "bench") { const r = SNAP.bench[String(P.rows)] || SNAP.bench["2000"]; return resolve(r); }
      if (name === "parse") {
        const r = SNAP.parse[P.formula];
        if (r) return resolve(r);
        return resolve({ formula: P.formula, error: { phase: "hosted demo", pretty: "This hosted page replays recorded compiler output, so only the example formulas can be compiled here.\nRun START_SheetGuard.bat to compile any formula live." } });
      }
      if (name === "grammar") return SNAP.grammar ? resolve(SNAP.grammar) : reject(new Error("Not recorded."));
      if (name === "theory") {
        const r = SNAP.theory[P.formula + "|" + (P.nregs || 4)];
        return r ? resolve(JSON.parse(JSON.stringify(r))) : reject(new Error("The hosted page has recorded results for the example formulas only (2, 4 or 8 registers). Run SheetGuard locally to try any formula."));
      }
      if (name === "multitarget" || name === "diff" || name === "fuzz") {
        const r = SNAP.ext && SNAP.ext[name];
        return r ? resolve(JSON.parse(JSON.stringify(r))) : reject(new Error("Not recorded in the hosted demo."));
      }
      if (name === "health") return resolve({ ok: true });
      reject(new Error("This action needs the local SheetGuard backend (START_SheetGuard.bat)."));
    }, name === "bench" ? 600 : 60));
  }
  function api(name, payload) {
    if (SNAP) return snapshotApi(name, payload);
    if (IS_GS && MODE !== "web") {
      return new Promise((resolve, reject) => {
        google.script.run
          .withSuccessHandler((txt) => {
            let d;
            try { d = typeof txt === "string" ? JSON.parse(txt) : txt; }
            catch (e) { return reject(new Error("Backend did not return JSON: " + String(txt).slice(0, 160))); }
            if (d && typeof d.error === "string") reject(new Error(d.error)); else resolve(d);
          })
          .withFailureHandler((e) => reject(e))
          .sgApi(name, JSON.stringify(payload || {}));
      });
    }
    return fetch((window.SG_API_BASE || "") + "/api/" + name, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload || {}),
    }).then(async (r) => {
      let d;
      try { d = await r.json(); } catch (e) { throw new Error("Server error " + r.status); }
      if (!r.ok || typeof d.error === "string") throw new Error(d.error || r.statusText);
      return d;
    });
  }
  function gs(fn, ...args) {
    return new Promise((resolve, reject) => {
      if (!IS_GS) return reject(new Error("Only available inside Google Sheets"));
      google.script.run.withSuccessHandler(resolve).withFailureHandler(reject)[fn](...args);
    });
  }

  // ------------------------------------------------------------------ state
  const S = {
    report: null, before: null, loading: false, error: null,
    tab: MODE === "sidebar" ? "verify" : "overview",
    selected: null, filter: "all", expanded: new Set(), source: null, workbookJson: null,
    fixed: new Set(), sheet: null, showFormulas: false, code: null, codeOpts: { numpy: false, target: "" },
    bench: null, benchRows: 2000, dagAll: false, config: null, highlight: true, copyUrl: null,
    play: { formula: "=SUM(Sales!B2:B9)*(1+TaxRate)", result: null },
    lab: { formula: "=IF(B2>0,B2*C2*(1+10%)+B2*C2,0)", result: null, section: "grammar", nregs: 4, state: 0, block: 0 },
    grammar: null,
    ext: { section: "targets", lang: "sql" },
  };
  window.SG_STATE = S;

  // ------------------------------------------------------------------ formatting
  function fmtValue(v, fmt) {
    if (v == null || v === "") return "";
    if (typeof v === "boolean") return v ? "TRUE" : "FALSE";
    if (typeof v !== "number") return String(v);
    fmt = fmt || "";
    const dec = (() => { const m = /\.([0#]+)/.exec(fmt); return m ? m[1].length : 0; })();
    if (fmt.includes("%")) return (v * 100).toFixed(dec) + "%";
    if (fmt.includes("₹")) return "₹" + Math.round(v).toLocaleString("en-IN");
    if (fmt.includes("$")) return (v < 0 ? "-$" : "$") + Math.abs(v).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    if (Number.isInteger(v)) return v.toLocaleString("en-US");
    return v.toLocaleString("en-US", { maximumFractionDigits: 4 });
  }
  const isErr = (v) => typeof v === "string" && /^#[A-Z/0!?]+[!?A]?$/.test(v) && v.length <= 9;
  const SEV_ORDER = { error: 0, warning: 1, info: 2 };

  function gridCell(key) {
    if (!S.report) return null;
    const k = splitKey(key);
    const sh = S.report.grid.find((g) => g.name === k.sheet);
    return sh ? sh.cells[k.a1] || null : null;
  }
  function diagsFor(key) { return S.report ? S.report.diagnostics.filter((d) => d.cell === key) : []; }

  // ------------------------------------------------------------------ logo & ring
  function logo() {
    return s("svg", { class: "sg-logo", viewBox: "0 0 32 32" },
      s("defs", null, s("linearGradient", { id: "sgg", x1: "0", y1: "0", x2: "1", y2: "1" },
        s("stop", { offset: "0", "stop-color": "#6366f1" }), s("stop", { offset: "1", "stop-color": "#4338ca" }))),
      s("path", { d: "M16 2 L28 7 V15 C28 22.5 22.8 28.4 16 30 C9.2 28.4 4 22.5 4 15 V7 Z", fill: "url(#sgg)" }),
      s("path", { d: "M10 11h12M10 16h12M10 21h7", stroke: "#fff", "stroke-width": "2", "stroke-linecap": "round", opacity: ".55" }),
      s("path", { d: "M17.5 20.5l2.4 2.4 4.6-5.2", stroke: "#fff", "stroke-width": "2.4", fill: "none", "stroke-linecap": "round", "stroke-linejoin": "round" }));
  }
  function ring(score, grade, size) {
    size = size || 44;
    const r = size / 2 - 4, c = 2 * Math.PI * r;
    const col = score >= 75 ? "var(--ok)" : score >= 50 ? "var(--warn)" : "var(--err)";
    return s("svg", { class: "ring", width: size, height: size, viewBox: `0 0 ${size} ${size}` },
      s("title", null, `Health score ${score}/100 (grade ${grade})`),
      s("circle", { cx: size / 2, cy: size / 2, r, fill: "none", stroke: "#e5e7eb", "stroke-width": 4 }),
      s("circle", { cx: size / 2, cy: size / 2, r, fill: "none", stroke: col, "stroke-width": 4, "stroke-linecap": "round",
        "stroke-dasharray": `${(c * score) / 100} ${c}`, transform: `rotate(-90 ${size / 2} ${size / 2})` }),
      s("text", { x: size / 2, y: size / 2 + 1, "text-anchor": "middle", "dominant-baseline": "middle", "font-size": size * 0.3, fill: col }, grade));
  }
  function countChips(c) {
    return h("div", { class: "counts" },
      h("span", { class: "chip err", title: "errors" }, "● ", c.error, MODE === "sidebar" ? "" : " errors"),
      h("span", { class: "chip warn", title: "warnings" }, "● ", c.warning, MODE === "sidebar" ? "" : " warnings"),
      h("span", { class: "chip info", title: "notes" }, "● ", c.info, MODE === "sidebar" ? "" : " notes"));
  }

  // ------------------------------------------------------------------ formula with token colours
  function formulaView(raw, tokens) {
    const box = h("div", { class: "formula-box" });
    if (!tokens || typeof raw !== "string") { box.textContent = raw == null ? "" : String(raw); return box; }
    let pos = 0;
    for (const t of tokens) {
      if (t.type === "EOF") continue;
      if (t.pos > pos) box.append(raw.slice(pos, t.pos));
      box.append(h("span", { class: "tok tok-" + t.type, title: t.type }, raw.slice(t.pos, t.end)));
      pos = t.end;
    }
    if (pos < raw.length) box.append(raw.slice(pos));
    return box;
  }
  function tokenChips(tokens) {
    return h("div", { class: "tokens" }, tokens.filter((t) => t.type !== "EOF").map((t) =>
      h("span", { class: "tk" }, h("i", null, t.type), h("b", { class: "tok-" + t.type }, t.value))));
  }

  // ------------------------------------------------------------------ AST (tidy tree, SVG)
  function astInfo(n) {
    const lab = n.label || n.kind, rest = lab.indexOf(" ") > 0 ? lab.slice(lab.indexOf(" ") + 1) : lab;
    switch (n.kind) {
      case "BinOp": return { text: rest, cls: "op", cap: "binary op", round: true };
      case "UnaryOp": return { text: rest, cls: "op", cap: "unary op", round: true };
      case "Percent": return { text: "%", cls: "op", cap: "postfix %", round: true };
      case "FuncCall": return { text: rest.split("/")[0] + "( )", cls: "fn", cap: "call, " + rest.split("/")[1] + " arg(s)" };
      case "CellRef": return { text: rest, cls: "ref", cap: "cell reference" };
      case "RangeRef": { const m = /^(\S+)\s+\[(.+)\]$/.exec(rest); return { text: m ? m[1] : rest, cls: "ref", cap: "range " + (m ? m[2] : "") }; }
      case "NameRef": return { text: rest, cls: "name", cap: "named range" };
      case "Number": return { text: rest, cls: "lit", cap: "number literal" };
      case "String": return { text: rest.length > 18 ? rest.slice(0, 16) + '…"' : rest, cls: "lit", cap: "text literal" };
      case "Bool": return { text: rest, cls: "lit", cap: "boolean" };
      case "Error": return { text: rest, cls: "err", cap: "error literal" };
      case "Missing": return { text: "∅", cls: "lit", cap: "omitted argument" };
      case "Array": return { text: "{ }", cls: "lit", cap: rest };
      default: return { text: lab, cls: "lit", cap: n.kind };
    }
  }
  const KCOL = { op: "#4f46e5", fn: "#7c3aed", ref: "#0d9488", lit: "#64748b", name: "#c2410c", err: "#dc2626" };
  const KFILL = { op: "#4f46e5", fn: "#f5f3ff", ref: "#f0fdfa", lit: "#f8fafc", name: "#fff7ed", err: "#fef2f2" };

  function astSvg(ast) {
    const NH = 26, LH = 58, GAP = 12, PAD = 14;
    let nextX = PAD, maxDepth = 0;
    function lay(n, depth) {
      maxDepth = Math.max(maxDepth, depth);
      const inf = astInfo(n);
      const capW = inf.cap.length * 4.9 + 8;
      const w = Math.max(inf.round ? 30 : Math.max(36, inf.text.length * 7.3 + 18), capW);
      const kids = (n.children || []).map((c) => lay(c, depth + 1));
      let cx;
      if (!kids.length) { cx = nextX + w / 2; nextX += w + GAP; }
      else {
        cx = (kids[0].cx + kids[kids.length - 1].cx) / 2;
        const right = cx + w / 2 + GAP;
        if (right > nextX) nextX = right;
      }
      return { n, inf, w, cx, cy: PAD + depth * LH + NH / 2, kids };
    }
    const root = lay(ast, 0);
    const W = Math.max(nextX + PAD - GAP, 120), H = PAD * 2 + maxDepth * LH + NH + 14;
    const svg = s("svg", { width: W, height: H, viewBox: `0 0 ${W} ${H}` });
    const edges = s("g"), nodes = s("g");
    (function draw(t) {
      for (const k of t.kids) {
        const y1 = t.cy + NH / 2, y2 = k.cy - NH / 2, my = (y1 + y2) / 2;
        edges.append(s("path", { d: `M${t.cx},${y1} C${t.cx},${my} ${k.cx},${my} ${k.cx},${y2}`, fill: "none", stroke: "#cbd5e1", "stroke-width": 1.5 }));
        draw(k);
      }
      const col = KCOL[t.inf.cls];
      const g = s("g", null, s("title", null, `${t.n.kind}: ${t.n.label}\n(${t.inf.cap})`));
      if (t.inf.round) {
        g.append(s("circle", { cx: t.cx, cy: t.cy, r: 15, fill: col }));
        g.append(s("text", { x: t.cx, y: t.cy + 1, "text-anchor": "middle", "dominant-baseline": "middle", fill: "#fff", "font-size": 14, "font-weight": 700, "font-family": "var(--mono)" }, t.inf.text));
      } else {
        const bw = Math.max(36, t.inf.text.length * 7.3 + 18);
        g.append(s("rect", { x: t.cx - bw / 2, y: t.cy - NH / 2, width: bw, height: NH, rx: 7, fill: KFILL[t.inf.cls], stroke: col, "stroke-width": 1.4 }));
        g.append(s("text", { x: t.cx, y: t.cy + 1, "text-anchor": "middle", "dominant-baseline": "middle", fill: col, "font-size": 12, "font-weight": 600, "font-family": "var(--mono)" }, t.inf.text));
      }
      g.append(s("text", { x: t.cx, y: t.cy + NH / 2 + 10, "text-anchor": "middle", fill: "#94a3b8", "font-size": 9 }, t.inf.cap));
      nodes.append(g);
    })(root);
    svg.append(edges, nodes);
    return h("div", { class: "svg-wrap" }, svg);
  }

  // ------------------------------------------------------------------ dependency DAG
  function namedTargets(rep) {
    const set = new Set();
    for (const sy of (rep.symbols && rep.symbols.symbols) || []) {
      if (sy.kind !== "range") continue;
      const m = /^'?(.*?)'?!\$?([A-Z]+)\$?(\d+)$/.exec(sy.definition || "");
      if (m) set.add(`${m[1]}!${m[2]}${m[3]}`);
    }
    return set;
  }
  function dagView(rep, opts) {
    opts = opts || {};
    const g = rep.graph;
    const named = namedTargets(rep);
    const sevOf = {};
    for (const d of rep.diagnostics) if (d.cell && (!sevOf[d.cell] || SEV_ORDER[d.severity] < SEV_ORDER[sevOf[d.cell]])) sevOf[d.cell] = d.severity;
    const showAll = opts.all != null ? opts.all : S.dagAll;
    const visible = g.nodes.filter((n) => n.is_formula || n.in_cycle || (n.out_degree > 0 && (showAll || named.has(n.id))));
    const vis = new Set(visible.map((n) => n.id));
    const maxLevel = Math.max(1, ...visible.filter((n) => n.level != null).map((n) => n.level));
    const colOf = (n) => (!n.is_formula ? 0 : n.level == null ? maxLevel + 1 : Math.max(1, n.level));
    const sheets = rep.grid.map((x) => x.name);
    const NW = 98, NH = 22, COLW = 136, ROWH = 29, PAD = 16, TOP = 30, LANE_HEAD = 20;
    const lanes = [];
    let y = TOP;
    const pos = {};
    for (const sh of sheets) {
      const mine = visible.filter((n) => splitKey(n.id).sheet === sh);
      if (!mine.length) continue;
      const byCol = {};
      for (const n of mine) (byCol[colOf(n)] = byCol[colOf(n)] || []).push(n);
      let rows = 0;
      for (const c in byCol) {
        byCol[c].sort((a, b) => { const A = splitKey(a.id), B = splitKey(b.id); return A.row - B.row || A.col - B.col; });
        byCol[c].forEach((n, i) => { pos[n.id] = { x: PAD + c * COLW, y: y + LANE_HEAD + i * ROWH, n }; });
        rows = Math.max(rows, byCol[c].length);
      }
      const hgt = LANE_HEAD + rows * ROWH + 8;
      lanes.push({ sh, y, h: hgt });
      y += hgt + 10;
    }
    const W = PAD * 2 + (maxLevel + 2) * COLW, H = y + 6;
    const edges = g.edges.filter((e) => vis.has(e.from) && vis.has(e.to));
    const preds = {}, succs = {};
    for (const e of edges) { (preds[e.to] = preds[e.to] || []).push(e.from); (succs[e.from] = succs[e.from] || []).push(e.to); }
    const cyc = new Set(g.nodes.filter((n) => n.in_cycle).map((n) => n.id));
    const sel = opts.selected || S.selected;
    const selP = new Set(sel ? (preds[sel] || []) : []), selD = new Set(sel ? (succs[sel] || []) : []);

    const svg = s("svg", { viewBox: `0 0 ${W} ${H}`, preserveAspectRatio: "xMidYMid meet" });
    const defs = s("defs");
    for (const [id, col] of [["a-n", "#94a3b8"], ["a-r", "#dc2626"], ["a-p", "#0ea5e9"], ["a-d", "#f97316"]])
      defs.append(s("marker", { id, viewBox: "0 0 10 10", refX: 9, refY: 5, markerWidth: 6, markerHeight: 6, orient: "auto-start-reverse" },
        s("path", { d: "M0,0 L10,5 L0,10 z", fill: col })));
    svg.append(defs);
    const gl = s("g"), ge = s("g"), gn = s("g");
    lanes.forEach((L, i) => {
      gl.append(s("rect", { x: 4, y: L.y, width: W - 8, height: L.h, rx: 10, fill: i % 2 ? "#f8fafc" : "#f1f5ff", stroke: "#e2e8f0" }));
      gl.append(s("text", { x: 14, y: L.y + 14, "font-size": 11, "font-weight": 700, fill: "#64748b" }, "SHEET  " + L.sh));
    });
    for (let c = 0; c <= maxLevel + 1; c++) {
      const label = c === 0 ? "inputs" : c === maxLevel + 1 ? "cycle / blocked" : "level " + c;
      if (c === maxLevel + 1 && !visible.some((n) => n.is_formula && n.level == null)) continue;
      gl.append(s("text", { x: PAD + c * COLW + NW / 2, y: 18, "text-anchor": "middle", "font-size": 10.5, fill: "#94a3b8", "font-weight": 600 }, label.toUpperCase()));
    }
    const edgeEls = [];
    for (const e of edges) {
      const a = pos[e.from], b = pos[e.to];
      if (!a || !b) continue;
      const x1 = a.x + NW, y1 = a.y + NH / 2, x2 = b.x, y2 = b.y + NH / 2;
      let d;
      if (x2 > x1 + 4) { const mx = (x1 + x2) / 2; d = `M${x1},${y1} C${mx},${y1} ${mx},${y2} ${x2 - 2},${y2}`; }
      else { const lift = 26 + Math.abs(y2 - y1) * 0.2; d = `M${a.x + NW / 2},${a.y} C${a.x + NW / 2},${a.y - lift} ${b.x + NW / 2},${b.y - lift} ${b.x + NW / 2},${b.y - 1}`; }
      const red = cyc.has(e.from) && cyc.has(e.to);
      let col = red ? "#dc2626" : "#cbd5e1", mk = red ? "a-r" : "a-n", wdt = red ? 2 : 1.1;
      if (sel && e.to === sel) { col = "#0ea5e9"; mk = "a-p"; wdt = 2.2; }
      if (sel && e.from === sel) { col = "#f97316"; mk = "a-d"; wdt = 2.2; }
      const p = s("path", { d, fill: "none", stroke: col, "stroke-width": wdt, "marker-end": `url(#${mk})` });
      p.dataset.f = e.from; p.dataset.t = e.to;
      edgeEls.push(p);
      ge.append(p);
    }
    const nodeEls = [];
    for (const id in pos) {
      const { x, y: yy, n } = pos[id];
      const k = splitKey(id), sev = sevOf[id];
      let fill = n.is_formula ? "#eef2ff" : "#ffffff", stroke = n.is_formula ? "#a5b4fc" : "#cbd5e1";
      if (n.in_cycle) { fill = "#fee2e2"; stroke = "#ef4444"; }
      if (sev === "error") stroke = "#dc2626"; else if (sev === "warning") stroke = "#d97706";
      if (selP.has(id)) { fill = "#e0f2fe"; } if (selD.has(id)) { fill = "#ffedd5"; }
      const isSel = id === sel;
      const gnode = s("g", { class: "node", transform: `translate(${x},${yy})` },
        s("title", null, `${id}${n.formula ? "\n" + n.formula : ""}${sev ? "\n⚠ " + sev : ""}`),
        s("rect", { width: NW, height: NH, rx: 6, fill, stroke: isSel ? "#4f46e5" : stroke, "stroke-width": isSel ? 2.6 : sev ? 1.8 : 1.1 }),
        s("text", { x: 8, y: NH / 2 + 1, "dominant-baseline": "middle", "font-size": 11.5, "font-weight": 700, "font-family": "var(--mono)", fill: "#1e293b" }, k.a1),
        s("text", { x: NW - 7, y: NH / 2 + 1, "dominant-baseline": "middle", "text-anchor": "end", "font-size": 9.5, fill: "#64748b" },
          n.is_formula ? (n.formula || "").replace(/^=/, "").slice(0, 9) : "input"));
      if (sev) gnode.append(s("circle", { cx: NW - 2, cy: 2, r: 4, fill: sev === "error" ? "#dc2626" : sev === "warning" ? "#d97706" : "#2563eb" }));
      gnode.addEventListener("click", (ev) => { ev.stopPropagation(); (opts.onSelect || select)(id); });
      gnode.addEventListener("mouseenter", () => {
        const keep = new Set([id, ...(preds[id] || []), ...(succs[id] || [])]);
        nodeEls.forEach((ne) => ne.el.classList.toggle("dim", !keep.has(ne.id)));
        edgeEls.forEach((pe) => pe.classList.toggle("dim", !(pe.dataset.f === id || pe.dataset.t === id)));
      });
      gnode.addEventListener("mouseleave", () => { nodeEls.forEach((ne) => ne.el.classList.remove("dim")); edgeEls.forEach((pe) => pe.classList.remove("dim")); });
      nodeEls.push({ id, el: gnode });
      gn.append(gnode);
    }
    svg.append(gl, ge, gn);

    // pan & zoom by manipulating the viewBox
    let vb = [0, 0, W, H];
    const apply = () => svg.setAttribute("viewBox", vb.join(" "));
    const wrap = h("div", { class: "svg-wrap dag-wrap" }, svg);
    wrap.addEventListener("wheel", (ev) => {
      ev.preventDefault();
      const r = svg.getBoundingClientRect(), f = ev.deltaY > 0 ? 1.12 : 1 / 1.12;
      const px = vb[0] + ((ev.clientX - r.left) / r.width) * vb[2], py = vb[1] + ((ev.clientY - r.top) / r.height) * vb[3];
      vb = [px - (px - vb[0]) * f, py - (py - vb[1]) * f, vb[2] * f, vb[3] * f];
      apply();
    }, { passive: false });
    let drag = null;
    wrap.addEventListener("mousedown", (ev) => { drag = { x: ev.clientX, y: ev.clientY, vb: vb.slice() }; wrap.classList.add("drag"); });
    window.addEventListener("mouseup", () => { drag = null; wrap.classList.remove("drag"); });
    window.addEventListener("mousemove", (ev) => {
      if (!drag) return;
      const r = svg.getBoundingClientRect(), sc = Math.max(vb[2] / r.width, vb[3] / r.height);
      vb = [drag.vb[0] - (ev.clientX - drag.x) * sc, drag.vb[1] - (ev.clientY - drag.y) * sc, vb[2], vb[3]];
      apply();
    });
    const zoom = (f) => { const cx = vb[0] + vb[2] / 2, cy = vb[1] + vb[3] / 2; vb = [cx - (vb[2] * f) / 2, cy - (vb[3] * f) / 2, vb[2] * f, vb[3] * f]; apply(); };
    wrap.append(h("div", { class: "dag-tools" },
      h("button", { class: "btn sm", onclick: (e) => { e.stopPropagation(); zoom(1 / 1.3); }, title: "Zoom in" }, "+"),
      h("button", { class: "btn sm", onclick: (e) => { e.stopPropagation(); zoom(1.3); }, title: "Zoom out" }, "−"),
      h("button", { class: "btn sm", onclick: (e) => { e.stopPropagation(); vb = [0, 0, W, H]; apply(); }, title: "Fit" }, "Fit"),
      opts.expand ? h("button", { class: "btn sm", onclick: (e) => { e.stopPropagation(); opts.expand(); }, title: "Open large" }, "⤢") : null));
    return wrap;
  }
  function dagLegend() {
    return h("div", { class: "dag-legend" },
      h("span", { style: "--c:#eef2ff;--b:#a5b4fc" }, "formula"), h("span", { style: "--c:#fff;--b:#cbd5e1" }, "named input"),
      h("span", { style: "--c:#fee2e2;--b:#ef4444" }, "circular"), h("span", { style: "--c:#e0f2fe" }, "precedent of selection"),
      h("span", { style: "--c:#ffedd5" }, "dependent of selection"));
  }

  // ------------------------------------------------------------------ issue cards
  function currentRaw(key) {
    const c = gridCell(key);
    return c ? c.raw : "";
  }
  function issueCard(d, compact) {
    const id = d.code + "|" + d.cell + "|" + d.message;
    const open = S.expanded.has(id) || compact === "open";
    const applied = d.cell && S.fixed.has(d.cell) && d.suggested_formula;
    const card = h("div", { class: `issue ${d.severity}${applied ? " applied" : ""}` });
    const head = h("div", { class: "issue-h", onclick: () => { open ? S.expanded.delete(id) : S.expanded.add(id); if (d.cell) S.selected = d.cell; render(); } },
      h("span", { class: "sev-dot " + d.severity }),
      h("div", { style: "flex:1;min-width:0" },
        h("div", { class: "row", style: "gap:6px" }, h("span", { class: "issue-title" }, d.title),
          h("span", { class: "chip tiny" }, d.code),
          d.cell ? h("span", { class: "chip link tiny mono", onclick: (e) => { e.stopPropagation(); goto(d.cell); } }, d.cell) : null,
          d.suggested_formula ? h("span", { class: "chip ok tiny" }, applied ? "✓ fixed" : "auto-fix") : null),
        h("div", { class: "issue-msg" }, d.message)));
    card.append(head);
    if (open) {
      const body = h("div", { class: "issue-b" });
      const expl = d.explanation || "";
      const caretAt = expl.indexOf("\n");
      if (caretAt > 0 && /\^/.test(expl)) {
        body.append(h("p", null, expl.slice(0, caretAt)), h("pre", { class: "caret" }, expl.slice(caretAt + 1)));
      } else body.append(h("div", { class: "lbl" }, "Why it matters"), h("p", null, expl));
      if (d.fix) body.append(h("div", { class: "lbl" }, "How to fix"), h("p", null, d.fix));
      if (d.suggested_formula) {
        body.append(h("div", { class: "diff" }, h("div", { class: "del" }, "− " + (currentRaw(d.cell) || "")), h("div", { class: "add" }, "+ " + d.suggested_formula)));
      }
      if (d.related && d.related.length)
        body.append(h("div", { class: "row small", style: "margin:6px 0" }, h("span", { class: "faint" }, "Related:"),
          d.related.slice(0, 8).map((r) => h("span", { class: "chip link mono tiny", onclick: () => goto(r) }, r))));
      body.append(h("div", { class: "row", style: "margin-top:8px" },
        d.cell ? h("button", { class: "btn sm", onclick: () => goto(d.cell) }, MODE === "sidebar" ? "Go to cell" : "Inspect cell") : null,
        d.suggested_formula && !applied ? h("button", { class: "btn sm ok", onclick: () => applyFix(d) }, "Apply fix") : null));
      card.append(body);
    }
    return card;
  }
  function issueList(diags, withFilter) {
    const counts = { all: diags.length, error: 0, warning: 0, info: 0 };
    diags.forEach((d) => counts[d.severity]++);
    const shown = diags.filter((d) => S.filter === "all" || d.severity === S.filter);
    const wrap = h("div", { class: "stack" });
    if (withFilter)
      wrap.append(h("div", { class: "row" },
        h("div", { class: "seg" }, ["all", "error", "warning", "info"].map((f) =>
          h("button", { class: S.filter === f ? "on" : "", onclick: () => { S.filter = f; render(); } },
            { all: "All", error: "Errors", warning: "Warnings", info: "Notes" }[f] + " " + counts[f]))),
        h("span", { class: "sg-spacer" }),
        S.report && S.report.fixes.length ? h("button", { class: "btn sm ok", onclick: applyAll }, `Apply all ${S.report.fixes.length} fixes`) : null));
    if (!shown.length) wrap.append(h("div", { class: "empty" }, diags.length ? "Nothing in this filter." : "✓ No issues found."));
    shown.forEach((d) => wrap.append(issueCard(d)));
    return wrap;
  }

  // ------------------------------------------------------------------ cell inspector
  function inspector(key) {
    const rep = S.report;
    if (!key) return h("div", { class: "empty" }, MODE === "sidebar" ? "Click any cell in the sheet (or a node in the graph) to see how the compiler sees it." : "Select a cell in the grid, an issue, or a node in the graph.");
    const info = rep.cells[key];
    const gc = gridCell(key);
    const node = rep.graph.nodes.find((n) => n.id === key);
    const wrap = h("div", { class: "stack" });
    wrap.append(h("div", { class: "row" }, h("b", { class: "mono", style: "font-size:15px" }, key),
      info && info.type ? h("span", { class: "chip brand" }, info.type) : gc && gc.type ? h("span", { class: "chip brand" }, gc.type) : null,
      node && node.level != null ? h("span", { class: "chip", title: "recalculation level in the topological order" }, "level " + node.level) : null,
      node && node.in_cycle ? h("span", { class: "chip err" }, "in a cycle") : null));
    if (!info) {
      if (!gc) return wrap.append(h("div", { class: "muted" }, "Empty cell.")), wrap;
      wrap.append(h("dl", { class: "kv" }, h("dt", null, "Constant"), h("dd", { class: "mono" }, String(gc.raw)),
        h("dt", null, "Displayed"), h("dd", null, fmtValue(gc.value, gc.fmt))));
      const deps = rep.graph.edges.filter((e) => e.from === key).map((e) => e.to);
      wrap.append(h("div", null, h("div", { class: "lbl faint small" }, "Read by"),
        deps.length ? h("div", { class: "row" }, deps.map((r) => h("span", { class: "chip link mono tiny", onclick: () => select(r) }, r))) : h("span", { class: "faint small" }, "nothing — this input is unused")));
      diagsFor(key).forEach((d) => wrap.append(issueCard(d)));
      return wrap;
    }
    wrap.append(formulaView(info.raw, info.tokens));
    const val = info.value;
    wrap.append(h("dl", { class: "kv" },
      h("dt", null, "Value"), h("dd", { class: isErr(val) ? "mono" : "", style: isErr(val) ? "color:var(--err);font-weight:700" : "" }, fmtValue(val, info.fmt) || "(blank)"),
      h("dt", null, "R1C1 form"), h("dd", { class: "mono small" }, info.r1c1 || ""),
      h("dt", null, "Reads"), h("dd", null, info.precedents.length ? info.precedents.slice(0, 12).map((r) => h("span", { class: "chip link mono tiny", style: "margin:1px", onclick: () => select(r) }, r.split("!")[0] === key.split("!")[0] ? r.split("!")[1] : r)) : "—"),
      h("dt", null, "Read by"), h("dd", null, info.dependents.length ? info.dependents.slice(0, 12).map((r) => h("span", { class: "chip link mono tiny", style: "margin:1px", onclick: () => select(r) }, r.split("!")[0] === key.split("!")[0] ? r.split("!")[1] : r)) : "— (output)"),
      h("dt", null, "Impact"), h("dd", null, `changing this cell changes ${info.impact_count == null ? (info.impact.length + "+") : info.impact_count} other cell(s)`)));
    if (info.ast) {
      wrap.append(h("div", null, h("div", { class: "row small faint", style: "margin-bottom:4px" }, "Abstract syntax tree", h("span", { class: "sg-spacer" }), info.ast_stats ? `${info.ast_stats.nodes} nodes · depth ${info.ast_stats.depth}` : ""), astSvg(info.ast)));
      if (MODE !== "sidebar") wrap.append(h("details", null, h("summary", { class: "small faint", style: "cursor:pointer" }, "Token stream (lexer output)"), h("div", { style: "margin-top:6px" }, tokenChips(info.tokens))));
    }
    if (info.error) wrap.append(h("pre", { class: "caret" }, info.error.pretty));
    const ds = diagsFor(key);
    if (ds.length) { wrap.append(h("div", { class: "lbl small faint" }, "Issues in this cell")); ds.forEach((d) => wrap.append(issueCard(d, "open"))); }
    return wrap;
  }

  // ------------------------------------------------------------------ grid
  function gridView() {
    const rep = S.report;
    if (!rep.grid || !rep.grid.length) return h("div", { class: "empty" }, "This workbook has no sheets.");
    if (!S.sheet || !rep.grid.find((g) => g.name === S.sheet)) S.sheet = rep.grid[0].name;
    const sh = rep.grid.find((g) => g.name === S.sheet);
    const selInfo = S.selected && rep.cells[S.selected];
    const prec = new Set(selInfo ? selInfo.precedents : []), dep = new Set(selInfo ? selInfo.dependents : []);
    const MAXR = Math.min(sh.rows, 400), cols = Math.min(Math.max(sh.cols, 1), 60);   // keep the DOM small
    const thead = h("tr", null, h("th", { class: "rh" }, ""), Array.from({ length: cols }, (_, i) => h("th", null, idxToCol(i + 1))));
    const tbody = h("tbody");
    for (let r = 1; r <= MAXR; r++) {
      const tr = h("tr", null, h("th", { class: "rh" }, r));
      for (let c = 1; c <= cols; c++) {
        const a1 = idxToCol(c) + r, key = `${sh.name}!${a1}`, cell = sh.cells[a1];
        const cls = [];
        let text = "";
        if (cell) {
          text = S.showFormulas && cell.f ? cell.raw : cell.f ? fmtValue(cell.value, cell.fmt) : fmtValue(cell.raw, cell.fmt);
          if (typeof (cell.f ? cell.value : cell.raw) === "number") cls.push("num");
          if (cell.f) cls.push("f");
          if (S.fixed.has(key)) cls.push("fixed"); else if (cell.sev) cls.push(cell.sev);
          if (isErr(cell.value)) cls.push("errv");
        }
        if (key === S.selected) cls.push("sel");
        if (prec.has(key)) cls.push("prec");
        if (dep.has(key)) cls.push("dep");
        tr.append(h("td", { class: cls.join(" "), title: cell ? (cell.f ? cell.raw : "") + (cell.type ? "  ::  " + cell.type : "") : "", onclick: () => select(key) }, text));
      }
      tbody.append(tr);
    }
    return h("div", null,
      h("div", { class: "row", style: "margin-bottom:8px" },
        h("div", { class: "seg" }, h("button", { class: S.showFormulas ? "" : "on", onclick: () => { S.showFormulas = false; render(); } }, "Values"),
          h("button", { class: S.showFormulas ? "on" : "", onclick: () => { S.showFormulas = true; render(); } }, "Formulas")),
        h("span", { class: "sg-spacer" }),
        h("span", { class: "chip err" }, "error"), h("span", { class: "chip warn" }, "warning"), h("span", { class: "chip info" }, "note"), h("span", { class: "chip ok" }, "fixed"),
        h("span", { class: "chip", style: "box-shadow: inset 0 0 0 2px #0ea5e9" }, "reads"), h("span", { class: "chip", style: "box-shadow: inset 0 0 0 2px #f97316" }, "read by")),
      h("div", { class: "grid-wrap" }, h("table", { class: "grid" }, h("thead", null, thead), tbody)),
      sh.rows > MAXR ? h("div", { class: "small faint", style: "margin-top:4px" }, `Showing first ${MAXR} of ${sh.rows} rows.`) : null,
      h("div", { class: "sheet-tabs" }, rep.grid.map((g) => h("button", { class: g.name === S.sheet ? "on" : "", onclick: () => { S.sheet = g.name; render(); } }, g.name))));
  }

  // ------------------------------------------------------------------ summary / impact
  function phaseStrip(rep) {
    const t = rep.timings_ms || {};
    const by = (codes) => rep.diagnostics.filter((d) => codes.some((c) => d.code.startsWith(c))).length;
    const phases = [
      ["Lexer + Parser", t["lex+parse"], by(["SG0"]) + " syntax errors"],
      ["Symbol table", t.symbols, by(["SG1"]) + " name errors"],
      ["Dependency DAG", t.graph, rep.graph.stats.cycles + " cycle(s)"],
      ["Type checker", t.types, by(["SG3"]) + " type/unit issues"],
      ["Smell detector", t.smells, by(["SG4"]) + " smells"],
      ["Optimizer", t.optimizer, (rep.optimizer ? rep.optimizer.stats.folded : 0) + " folds, " + by(["SG502"]) + " dead"],
    ];
    return h("div", { class: "phase-strip" }, phases.map(([n, ms, what]) =>
      h("div", { class: "phase" + (/^0 /.test(what) ? "" : " hit") }, h("b", null, n), h("span", null, (ms != null ? ms.toFixed(1) + " ms · " : "") + what))));
  }
  function summaryCard(rep, withKpis) {
    const f = rep.summary.features, st = rep.workbook;
    return h("div", { class: "card" },
      h("div", { class: "card-h" }, "Plain-English summary", h("span", { class: "sub" }, "generated from compiler analyses"), h("span", { class: "sg-spacer" }),
        h("button", { class: "btn sm ghost", onclick: () => copyText(rep.summary.text, "Summary copied") }, "Copy")),
      h("div", { class: "card-b stack" },
        h("div", { class: "summary-text" }, rep.summary.text),
        withKpis ? h("div", { class: "kpis" },
          kpi(st.formulas, "formulas"), kpi(f.inputs, "numeric inputs"), kpi(st.sheets, "sheets"),
          kpi(f.levels, "DAG levels"), kpi(rep.graph.stats.edges, "dependencies"), kpi(rep.fixes.length, "auto-fixable")) : null));
  }
  const kpi = (v, l) => h("div", { class: "kpi" }, h("b", null, typeof v === "number" ? fmtInt(v) : String(v)), h("span", null, l));
  function beforeAfterCard() {
    const b = S.before, a = S.report;
    if (!b || b === a) return null;
    const cell = (lbl, x, y) => h("div", { class: "kpi" }, h("b", null, h("span", { class: "strike", style: "font-size:14px;margin-right:6px" }, x), y), h("span", null, lbl));
    return h("div", { class: "card" }, h("div", { class: "card-h" }, "Before → after the fixes", h("span", { class: "sub" }, `${S.fixed.size} cell(s) rewritten`)),
      h("div", { class: "card-b stack" },
        h("div", { class: "row" }, ring(b.health.score, b.health.grade, 46), h("span", { class: "muted" }, `${b.health.score}/100`), h("span", { class: "faint", style: "font-size:20px" }, "→"),
          ring(a.health.score, a.health.grade, 46), h("span", { class: "muted" }, `${a.health.score}/100`)),
        h("div", { class: "kpis" }, cell("errors", b.counts.error, a.counts.error), cell("warnings", b.counts.warning, a.counts.warning), cell("notes", b.counts.info, a.counts.info)),
        a.diagnostics.length ? h("div", { class: "small muted" }, "Still open (need a human decision): ", a.diagnostics.filter((d) => d.severity === "error").map((d) => `${d.title.toLowerCase()} at ${d.cell}`).join("; ") || "notes only") : null));
  }
  function impactCard(rep, limit) {
    if (S.before && S.before !== rep && !(rep.impact && rep.impact.changes.length)) rep = S.before;
    const imp = rep.impact;
    if (!imp || !imp.changes.length) return h("div", { class: "card" }, h("div", { class: "card-h" }, "Impact of the bugs"), h("div", { class: "card-b empty" }, "Fixes do not change any computed value."));
    const rows = imp.changes.filter((c) => c.output).slice(0, limit || 50);
    const after = imp.after_score;
    return h("div", { class: "card" },
      h("div", { class: "card-h" }, "What the bugs are costing you", h("span", { class: "sub" }, "values now vs. after auto-fix")),
      h("div", { class: "card-b stack" },
        after ? h("div", { class: "row" }, ring(rep.health.score, rep.health.grade, 40), h("span", { class: "muted" }, `${rep.health.score}/100 now`), h("span", { class: "faint" }, "→"),
          ring(after.score, after.grade, 40), h("span", { class: "muted" }, `${after.score}/100 after ${rep.fixes.length} fixes`),
          h("span", { class: "small faint" }, `(${imp.after_counts.error} errors left need a human decision)`)) : null,
        h("table", { class: "t" }, h("thead", null, h("tr", null, h("th", null, "Reported figure"), h("th", { class: "r" }, "Shows now"), h("th", { class: "r" }, "Correct value"), h("th", { class: "r" }, "Δ"))),
          h("tbody", null, rows.map((c) => h("tr", null,
            h("td", null, h("div", null, c.label), h("span", { class: "chip link mono tiny", onclick: () => select(c.cell) }, c.cell)),
            h("td", { class: "r strike" }, c.before), h("td", { class: "r", style: "font-weight:600" }, c.after),
            h("td", { class: "r " + (c.delta_pct == null ? "" : c.delta_pct > 0 ? "up" : "down") }, c.delta_pct == null ? "fixed" : (c.delta_pct > 0 ? "+" : "") + c.delta_pct.toFixed(1) + "%")))))));
  }

  // ------------------------------------------------------------------ code view
  function highlightPy(src) {
    const re = /(#[^\n]*)|('(?:[^'\\\n]|\\.)*'|"(?:[^"\\\n]|\\.)*")|\b(def|return|try|except|if|else|not|raise|lambda|for|in|import|from|as|and|or|None|True|False|with)\b|\b(\d+\.?\d*(?:e[-+]?\d+)?)\b|\b(np\.[a-z_]+|_fin|_v|_r|mul|add|sub|div|call|call_lazy|compare|truthy|scalar|pct|neg|power|concat|Range|sum|min|max|float)\b(?=\()/g;
    let out = "", last = 0, m;
    while ((m = re.exec(src))) {
      out += esc(src.slice(last, m.index));
      const cls = m[1] ? "c" : m[2] ? "s" : m[3] ? "k" : m[4] ? "n" : "f";
      out += `<span class="${cls}">${esc(m[0])}</span>`;
      last = re.lastIndex;
    }
    return out + esc(src.slice(last));
  }
  function codeView() {
    const rep = S.report;
    let outputs = rep.graph.nodes.filter((n) => n.is_formula && n.out_degree === 0).map((n) => n.id);
    if (SNAP) outputs = outputs.filter((o) => SNAP.compile["py:" + o]);
    const load = () => {
      S.code = { loading: true };
      render();
      api("compile", Object.assign({}, S.source, { numpy: S.codeOpts.numpy, target: S.codeOpts.numpy ? null : S.codeOpts.target || null }))
        .then((d) => { S.code = d; render(); }).catch((e) => { S.code = { error: e.message }; render(); });
    };
    if (!S.code) setTimeout(load, 0);
    const st = (S.code && S.code.stats) || {};
    return h("div", { class: "stack" },
      h("div", { class: "card" }, h("div", { class: "card-b row" },
        h("div", { class: "seg" }, h("button", { class: S.codeOpts.numpy ? "" : "on", onclick: () => { S.codeOpts.numpy = false; S.code = null; render(); } }, "Python back-end"),
          h("button", { class: S.codeOpts.numpy ? "on" : "", onclick: () => { S.codeOpts.numpy = true; S.code = null; render(); } }, "NumPy vectorizer")),
        !S.codeOpts.numpy ? h("label", { class: "row small" }, "Compile only the slice needed for",
          h("select", { onchange: (e) => { S.codeOpts.target = e.target.value; S.code = null; render(); } },
            h("option", { value: "" }, "— whole workbook —"), outputs.map((o) => h("option", { value: o, selected: o === S.codeOpts.target ? "selected" : null }, o)))) : null,
        h("span", { class: "sg-spacer" }),
        S.code && S.code.source ? h("button", { class: "btn sm", onclick: () => copyText(S.code.source, "Code copied") }, "Copy") : null)),
      S.code && S.code.stats ? h("div", { class: "row" }, S.codeOpts.numpy ? [
        h("span", { class: "chip brand" }, `${st.vector_groups || 0} vectorized loops`), h("span", { class: "chip" }, `${st.vector_cells || 0} cells as array ops`),
        h("span", { class: "chip" }, `${st.scalar_cells || 0} scalar cells`), h("span", { class: "chip" }, `${st.numpy_reductions || 0} NumPy reductions`)] : [
        h("span", { class: "chip brand" }, `${st.formula_cells} formula cells`), h("span", { class: "chip ok" }, `${st.fast_cells || 0} type-specialized`),
        h("span", { class: "chip" }, `${st.slow_cells || 0} generic`), h("span", { class: "chip" }, `CSE: ${st.cse_temps || 0} temps, ${st.cse_reuses || 0} reuses`),
        h("span", { class: "chip" }, `${st.inputs_skipped || 0} unused inputs eliminated`), h("span", { class: "chip" }, `${st.lines} lines · compiled in ${st.compile_ms} ms`)]) : null,
      S.code && S.code.loading ? h("div", { class: "empty" }, h("span", { class: "spinner" }), " generating…") : null,
      S.code && S.code.error ? h("div", { class: "card card-b", style: "color:var(--err)" }, S.code.error) : null,
      S.code && S.code.source ? h("pre", { class: "code", html: highlightPy(S.code.source) }) : null);
  }

  // ------------------------------------------------------------------ benchmark
  function runBench() {
    S.bench = { loading: true };
    render();
    api("bench", { rows: S.benchRows }).then((d) => { S.bench = d; render(); }).catch((e) => { S.bench = { error: e.message }; render(); });
  }
  function benchView() {
    const run = runBench;
    if (!S.bench && MODE === "web") setTimeout(runBench, 0);
    const b = S.bench;
    const body = h("div", { class: "card-b stack" });
    if (!b) body.append(h("div", { class: "muted" }, "Recalculate the same synthetic sales model with five execution engines and compare. Every engine's output is verified cell-by-cell against the interpreter."));
    if (b && b.loading) body.append(h("div", { class: "empty" }, h("span", { class: "spinner" }), ` running ${fmtInt(S.benchRows)}-row benchmark…`));
    if (b && b.error) body.append(h("div", { style: "color:var(--err)" }, b.error));
    if (b && b.results) {
      const maxSp = Math.max(...b.results.map((r) => r.speedup || 1));
      body.append(h("div", { class: "muted small" }, `${b.workbook}: ${fmtInt(b.formulas)} formulas, ${fmtInt(b.cells)} cells. Bars use a log scale.`));
      b.results.forEach((r, i) => {
        const w = Math.max(3, (Math.log10(Math.max(r.speedup, 1)) / Math.log10(Math.max(maxSp, 10))) * 100);
        const colors = ["#94a3b8", "#818cf8", "#6366f1", "#4f46e5", "#059669"];
        body.append(h("div", { class: "bar-row" }, h("div", null, h("b", null, r.engine), h("div", { class: "tiny faint" }, r.description)),
          h("div", null, h("div", { class: "bar", style: { width: w + "%", background: colors[i % colors.length] } }, r.speedup + "×")),
          h("div", { class: "r mono small", style: "text-align:right" }, r.ms.toFixed(2) + " ms", h("div", { class: "tiny " + (r.verified ? "up" : "down") }, r.verified ? "✓ identical" : "✗ differs"))));
      });
      body.append(h("div", { class: "lbl small faint", style: "margin-top:10px" }, "Incremental recalculation (only cells reachable in the DAG)"),
        h("table", { class: "t" }, h("tbody", null, b.incremental.map((i) => h("tr", null, h("td", null, "change " + i.change),
          h("td", { class: "r" }, `${fmtInt(i.recomputed)} of ${fmtInt(b.formulas)} recomputed`), h("td", { class: "r mono" }, i.ms.toFixed(2) + " ms"))))));
    }
    return h("div", { class: "card" },
      h("div", { class: "card-h" }, "Execution-engine benchmark", h("span", { class: "sg-spacer" }),
        h("select", { onchange: (e) => (S.benchRows = +e.target.value) }, [500, 2000, 5000, 10000].map((n) => h("option", { value: n, selected: n === S.benchRows ? "selected" : null }, fmtInt(n) + " rows"))),
        h("button", { class: "btn sm primary", onclick: run, disabled: b && b.loading }, "Run benchmark")),
      body);
  }

  // ------------------------------------------------------------------ playground
  let playTimer = null;
  function playgroundView() {
    const P = S.play;
    const ctxSheet = S.sheet || (S.report && S.report.grid[0] && S.report.grid[0].name);
    const run = () => api("parse", Object.assign({}, S.source || {}, { formula: P.formula, sheet: ctxSheet })).then((d) => { P.result = d; renderPlayResult(); }).catch((e) => { P.result = { error: { pretty: e.message } }; renderPlayResult(); });
    const input = h("input", { id: "play-in", class: "formula-box", style: "width:100%;outline:none", value: P.formula, spellcheck: "false",
      oninput: (e) => { P.formula = e.target.value; clearTimeout(playTimer); playTimer = setTimeout(run, 220); } });
    const out = h("div", { id: "play-out", class: "stack" });
    if (!P.result) setTimeout(run, 0);
    setTimeout(renderPlayResult, 0);
    const examples = PLAY_EXAMPLES;
    return h("div", { class: "sg-cols" },
      h("div", { class: "card" }, h("div", { class: "card-h" }, "Formula playground", h("span", { class: "sub" }, `type any formula: it is compiled on every keystroke, in the context of sheet “${ctxSheet}”`)),
        h("div", { class: "card-b stack" }, input,
          h("div", { class: "row small" }, h("span", { class: "faint" }, "Try:"), examples.map((x) => h("span", { class: "chip link mono tiny", onclick: () => { P.formula = x; input.value = x; run(); } }, x))),
          out)),
      h("div", { class: "card" }, h("div", { class: "card-h" }, "Grammar (recursive descent)"), h("div", { class: "card-b" },
        h("pre", { class: "code", style: "font-size:11.5px", html: highlightPy(GRAMMAR) }))));
  }
  const PLAY_EXAMPLES = ["=SUM(Sales!B2:B9)*(1+TaxRate)", "=IF(A1>=10%,\"ok\",-2^2)", "=12*25000*(1+10%)", "=VLOOKUP($A2,Rates!$A$1:$C$50,3,FALSE)", "=SUM(A1:A3", "=A1+*2", "=LOG10(LOG10)", "=G4+H4", "=SUMM(B2:B9)*TaxRte"];
  const GRAMMAR = `# lowest precedence first: one parse function per rule
formula    ::= '=' expr EOF
expr       ::= comparison
comparison ::= concat   (('='|'<>'|'<'|'>'|'<='|'>=') concat)*
concat     ::= additive ('&' additive)*
additive   ::= term     (('+'|'-') term)*
term       ::= power    (('*'|'/') power)*
power      ::= unary    ('^' unary)*      # left-assoc, like Excel
unary      ::= ('-'|'+') unary | postfix  # binds tighter than ^ : -2^2 = 4
postfix    ::= primary '%'*
primary    ::= NUMBER | STRING | BOOL | ERROR | reference
             | FUNC '(' [args] ')' | NAME | '(' expr ')' | '{' array '}'
reference  ::= [SHEET] CELL [':' CELL] | [SHEET] COL_RANGE | [SHEET] ROW_RANGE
args       ::= [expr] ((','|';') [expr])*`;
  function renderPlayResult() {
    const out = document.getElementById("play-out");
    const r = S.play.result;
    if (!out || !r) return;
    out.innerHTML = "";
    if (r.tokens) out.append(h("div", null, h("div", { class: "lbl small faint" }, "1 · Lexer → tokens"), tokenChips(r.tokens)));
    if (r.error) {
      out.append(h("div", null, h("div", { class: "lbl small faint" }, r.error.phase ? `${r.error.phase} error` : "error"), h("pre", { class: "caret" }, r.error.pretty)));
      if (r.repair) out.append(h("div", { class: "row small" }, h("span", { class: "faint" }, "Error recovery (minimal repair):"),
        h("span", { class: "chip ok mono link", onclick: () => { S.play.formula = r.repair; const i = document.querySelector("#play-in"); if (i) i.value = r.repair; api("parse", Object.assign({}, S.source || {}, { formula: r.repair })).then((d) => { S.play.result = d; renderPlayResult(); }); } }, r.repair + "  ↵")));
      return;
    }
    out.append(h("div", null, h("div", { class: "lbl small faint" }, "2 · Parser → abstract syntax tree"), astSvg(r.ast)));
    out.append(h("dl", { class: "kv" },
      h("dt", null, "Inferred type"), h("dd", null, h("span", { class: "chip brand" }, r.type || "?")),
      h("dt", null, "Value"), h("dd", { class: "mono" }, r.value === "" ? "(blank)" : r.value),
      h("dt", null, "Re-generated"), h("dd", { class: "mono" }, r.unparsed),
      h("dt", null, "R1C1 form"), h("dd", { class: "mono" }, r.r1c1),
      r.optimized ? [h("dt", null, "Optimized"), h("dd", null, h("span", { class: "mono" }, r.optimized), h("div", { class: "tiny faint" }, (r.passes || []).join(" · ")))] : null));
    (r.diagnostics || []).forEach((d) => out.append(issueCard(d, "open")));
  }


  // ------------------------------------------------------------------ syllabus lab
  const LAB_EXAMPLES = ["=IF(B2>0,B2*C2*(1+10%)+B2*C2,0)", "=SUM(Sales!B2:B9)*(1+TaxRate)", "=(A1+B1)*(A1+B1)-C1/D1+E1*F1-G1",
    "=1+2*3", "=-2^2", "=A1&B1=C1", "=SUM(A1:A3", "=A1+*2"];
  const LAB_SECTIONS = [["grammar", "Grammar · FIRST/FOLLOW"], ["ll1", "LL(1) parsing"], ["slr", "SLR(1) parsing"], ["ir", "Intermediate code"], ["target", "Target code"]];
  let labTimer = null;
  function labLoad() {
    const L = S.lab;
    if (!S.grammar && !S.grammarLoading) {
      S.grammarLoading = true;
      api("grammar", {}).then((g) => { S.grammar = g; render(); }).catch((e) => toast(e.message, true));
    }
    const sheet = S.sheet || "Sales";
    api("theory", Object.assign({}, S.source || {}, { formula: L.formula, nregs: L.nregs, sheet, workbook_tac: !L.result }))
      .then((d) => {
        if (L.result && L.result.workbook_tac && !d.workbook_tac) d.workbook_tac = L.result.workbook_tac;
        L.result = d;
        if (d.tac && d.tac.dags) { let best = 0; d.tac.dags.forEach((g, i) => { if (g.nodes.length > d.tac.dags[best].nodes.length) best = i; }); L.block = best; }
        render();
      })
      .catch((e) => { L.result = { error: e.message }; render(); });
  }
  function setTable(headers, rows, opts) {
    opts = opts || {};
    return h("div", { class: "tbl-wrap", style: opts.maxH ? `max-height:${opts.maxH}px` : "" },
      h("table", { class: "t lab" + (opts.mono ? " mono-t" : "") + (opts.wrap ? " wrap" : "") }, h("thead", null, h("tr", null, headers.map((x) => h("th", null, x)))),
        h("tbody", null, rows.map((r) => h("tr", { class: r._cls || "" }, r.map((c) => h("td", null, c)))))));
  }
  function setStr(arr) { return "{ " + arr.join(", ") + " }"; }
  function traceTable(res, title) {
    if (!res) return null;
    const cut = (txt, n, fromEnd) => { const p = txt.split(" "); return p.length <= n ? txt : fromEnd ? "… " + p.slice(-n).join(" ") : p.slice(0, n).join(" ") + " …"; };
    const rows = res.trace.map((r) => { const row = [r.step, cut(r.stack, 12, true), cut(r.input, 8, false), r.action]; row._cls = /^error/.test(r.action) ? "bad" : r.action === "accept" ? "good" : /^(reduce|match)/.test(r.action) ? "red" : ""; return row; });
    return h("div", { class: "stack" },
      h("div", { class: "row" }, h("b", null, title), res.accepted ? h("span", { class: "chip ok" }, `accepted in ${res.trace.length} steps`) : h("span", { class: "chip err" }, "rejected: " + (res.error || ""))),
      setTable(["#", "Stack", "Input", "Action"], rows, { mono: true, maxH: 520 }));
  }
  function dagSvgBlock(dag) {
    const nodes = dag.nodes;
    if (!nodes.length) return h("div", { class: "faint small" }, "(empty block)");
    const byId = {}; nodes.forEach((n) => (byId[n.id] = n));
    const depth = {};
    const d = (n) => (depth[n.id] != null ? depth[n.id] : (depth[n.id] = n.kids.length ? 1 + Math.max(...n.kids.map((k) => d(byId[k]))) : 0));
    nodes.forEach(d);
    const maxD = Math.max(...Object.values(depth));
    const levels = {}; nodes.forEach((n) => (levels[depth[n.id]] = levels[depth[n.id]] || []).push(n));
    const W = 120, H = 70, PAD = 30;
    const width = Math.max(...Object.values(levels).map((l) => l.length)) * W + PAD * 2, height = (maxD + 1) * H + PAD * 2;
    const pos = {};
    Object.entries(levels).forEach(([lv, list]) => list.forEach((n, i) => { pos[n.id] = { x: PAD + i * W + W / 2 + (width - PAD * 2 - list.length * W) / 2, y: PAD + (maxD - lv) * H + 10 }; }));
    const svg = s("svg", { width, height, viewBox: `0 0 ${width} ${height}` });
    nodes.forEach((n) => n.kids.forEach((k) => { const a = pos[n.id], b = pos[k]; svg.append(s("line", { x1: a.x, y1: a.y, x2: b.x, y2: b.y, stroke: "#94a3b8", "stroke-width": 1.4 })); }));
    nodes.forEach((n) => {
      const p = pos[n.id], op = n.kind === "op";
      const g = s("g", null, s("title", null, `node ${n.id}: ${n.label}${n.names.length ? "\nattached: " + n.names.join(", ") : ""}`));
      if (op) g.append(s("circle", { cx: p.x, cy: p.y, r: 16, fill: "#4f46e5" }), s("text", { x: p.x, y: p.y + 1, "text-anchor": "middle", "dominant-baseline": "middle", fill: "#fff", "font-size": 12, "font-weight": 700 }, n.label.replace("call ", "")));
      else g.append(s("rect", { x: p.x - 38, y: p.y - 12, width: 76, height: 24, rx: 6, fill: "#f0fdfa", stroke: "#0d9488" }), s("text", { x: p.x, y: p.y + 1, "text-anchor": "middle", "dominant-baseline": "middle", fill: "#0f766e", "font-size": 11, "font-family": "var(--mono)" }, n.label.length > 10 ? n.label.slice(0, 9) + "…" : n.label));
      if (n.names.length) g.append(s("text", { x: p.x, y: p.y + (op ? 30 : 26), "text-anchor": "middle", fill: "#c2410c", "font-size": 10.5, "font-family": "var(--mono)" }, n.names.join(", ")));
      svg.append(g);
    });
    return h("div", { class: "svg-wrap" }, svg);
  }
  function viewLab() {
    const L = S.lab;
    if (!L.result && !L.loading) { L.loading = true; setTimeout(labLoad, 0); }
    const input = h("input", { id: "lab-in", class: "formula-box", style: "width:100%;outline:none", value: L.formula, spellcheck: "false",
      oninput: (e) => { L.formula = e.target.value; clearTimeout(labTimer); labTimer = setTimeout(labLoad, 250); } });
    const head = h("div", { class: "card" }, h("div", { class: "card-h" }, "Syllabus lab", h("span", { class: "sub" }, "the same formula language, treated exactly as in BCSE307L")),
      h("div", { class: "card-b stack" }, input,
        h("div", { class: "row small" }, h("span", { class: "faint" }, "Try:"), LAB_EXAMPLES.map((x) => h("span", { class: "chip link mono tiny", onclick: () => { L.formula = x; input.value = x; labLoad(); } }, x))),
        h("div", { class: "seg" }, LAB_SECTIONS.map(([id, label]) => h("button", { class: L.section === id ? "on" : "", onclick: () => { L.section = id; render(); } }, label)))));
    const r = L.result, G = S.grammar;
    let body;
    if (!r || (L.section === "grammar" && !G)) body = h("div", { class: "empty" }, h("span", { class: "spinner" }), " computing…");
    else if (r.error) body = h("div", { class: "card card-b", style: "color:var(--err)" }, r.error);
    else if (L.section === "grammar") body = labGrammar(G);
    else if (L.section === "ll1") body = h("div", { class: "sg-cols" }, h("div", { class: "card card-b" }, traceTable(r.ll1, "Table-driven predictive parse (LL(1))")),
      h("div", { class: "card" }, h("div", { class: "card-h" }, "How it works"), h("div", { class: "card-b small stack" },
        h("p", null, "The parser keeps a stack of grammar symbols, starting with ", h("span", { class: "kbd" }, "$ E"), ". If the top is a terminal, it must match the next input token. If it is a non-terminal A, the table entry M[A, a] for the lookahead token a says which production to expand — no backtracking, one token of lookahead."),
        G ? h("p", null, `Our LL(1) table has ${G.ll1.nonterminals.length} non-terminals × ${G.ll1.terminals.length} terminals and `, h("b", null, G.ll1.is_ll1 ? "no conflicts" : "conflicts"), ", which proves the grammar is LL(1).") : null,
        r.ll1.productions ? h("div", null, h("div", { class: "lbl faint" }, "Leftmost derivation used"), h("ol", { class: "mono small", style: "margin:4px 0;padding-left:22px" }, r.ll1.productions.map((p) => h("li", null, p)))) : null)));
    else if (L.section === "slr") body = labSlr(G, r);
    else if (L.section === "ir") body = labIR(r);
    else body = labTarget(r);
    return h("div", { class: "stack" }, head, body);
  }
  function labGrammar(G) {
    const g = G.ll1;
    const nts = g.nonterminals, terms = g.terminals;
    const ff = setTable(["Non-terminal", "FIRST", "FOLLOW"], nts.map((nt) => [h("b", { class: "mono" }, nt), h("span", { class: "mono small" }, setStr(g.first[nt])), h("span", { class: "mono small" }, setStr(g.follow[nt]))]), { maxH: 600, wrap: true });
    const matrix = h("div", { class: "tbl-wrap", style: "max-height:520px" }, h("table", { class: "t lab matrix" },
      h("thead", null, h("tr", null, h("th", null, "M[A,a]"), terms.map((t) => h("th", { class: "mono" }, t)))),
      h("tbody", null, nts.map((nt) => h("tr", null, h("th", { class: "mono" }, nt), terms.map((t) => {
        const p = g.table[nt][t];
        return h("td", { class: p != null ? "hit" : "", title: p != null ? g.productions[p] : "" }, p != null ? String(p) : "");
      }))))));
    return h("div", { class: "stack" },
      h("div", { class: "sg-cols" },
        h("div", { class: "card" }, h("div", { class: "card-h" }, "LL(1) grammar", h("span", { class: "sub" }, g.note)),
          h("div", { class: "card-b stack" }, h("pre", { class: "code", style: "font-size:12px" }, g.bnf.join("\n")),
            h("div", { class: "row" }, h("span", { class: "chip " + (g.is_ll1 ? "ok" : "err") }, g.is_ll1 ? "✓ no LL(1) conflicts — the grammar is LL(1)" : `${g.conflicts.length} conflicts`),
              h("span", { class: "chip " + (G.slr.is_slr1 ? "ok" : "err") }, G.slr.is_slr1 ? `✓ SLR(1): ${G.slr.states} states, no conflicts` : "SLR conflicts")))),
        h("div", { class: "card" }, h("div", { class: "card-h" }, "FIRST and FOLLOW sets", h("span", { class: "sub" }, "computed by fixed-point iteration")), h("div", { class: "card-b" }, ff))),
      h("div", { class: "card" }, h("div", { class: "card-h" }, "Predictive parsing table M[A, a]", h("span", { class: "sub" }, "hover a cell to see the production; empty = syntax error")),
        h("div", { class: "card-b stack" }, matrix, h("details", null, h("summary", { class: "small faint", style: "cursor:pointer" }, "Numbered productions"),
          h("ol", { start: 0, class: "mono small", style: "columns:2;margin-top:6px" }, g.productions.map((p) => h("li", null, p)))))));
  }
  function labSlr(G, r) {
    const st = G.slr, L = S.lab;
    const sel = L.state || 0;
    const terms = st.terminals, nts = st.nonterminals;
    const table = h("div", { class: "tbl-wrap", style: "max-height:520px" }, h("table", { class: "t lab matrix" },
      h("thead", null, h("tr", null, h("th", null, "state"), terms.map((t) => h("th", { class: "mono" }, t)), nts.map((t) => h("th", { class: "mono nt" }, t)))),
      h("tbody", null, st.rows.map((row) => h("tr", { class: row.state === sel ? "selrow" : "", onclick: () => { L.state = row.state; render(); } },
        h("th", null, row.state),
        terms.map((t) => { const a = row.action[t]; return h("td", { class: a ? (a[0] === "s" ? "hit" : a === "acc" ? "acc" : "red") : "" }, a || ""); }),
        nts.map((t) => h("td", { class: row.goto[t] != null ? "go" : "" }, row.goto[t] != null ? row.goto[t] : "")))))));
    return h("div", { class: "stack" },
      h("div", { class: "sg-cols" }, h("div", { class: "card card-b" }, traceTable(r.slr, "Shift-reduce parse (SLR(1))")),
        h("div", { class: "stack" },
          h("div", { class: "card" }, h("div", { class: "card-h" }, "LR grammar", h("span", { class: "sub" }, st.note)), h("div", { class: "card-b" }, h("pre", { class: "code", style: "font-size:12px" }, st.bnf.join("\n")))),
          h("div", { class: "card" }, h("div", { class: "card-h" }, `Item set I${sel}`, h("span", { class: "sub" }, "LR(0) items (click a row of the table)")),
            h("div", { class: "card-b" }, h("pre", { class: "code", style: "font-size:12px;max-height:340px" }, st.rows[sel].items.join("\n")))))),
      h("div", { class: "card" }, h("div", { class: "card-h" }, `SLR(1) ACTION / GOTO table`, h("span", { class: "sub" }, `${st.states} states · sN = shift and go to N · rN = reduce by production N · acc = accept`)),
        h("div", { class: "card-b stack" }, table, h("details", null, h("summary", { class: "small faint", style: "cursor:pointer" }, "Productions (rN)"),
          h("ol", { class: "mono small", style: "list-style:none;padding:0;columns:2;margin-top:6px" }, st.productions.map((p) => h("li", null, p)))))));
  }
  function labIR(r) {
    const t = r.tac;
    if (t.error) return h("div", { class: "card card-b" }, h("pre", { class: "caret" }, t.error.pretty));
    const L = S.lab;
    const blk = L.block || 0;
    const quads = setTable(["#", "op", "arg1", "arg2", "result"], t.quadruples.map((q) => [q.i, q.op, q.arg1, q.arg2, q.result]), { mono: true, maxH: 420 });
    const trip = setTable(["#", "op", "arg1", "arg2"], t.triples.map((q) => [`(${q.i})`, q.op, q.arg1, q.arg2]), { mono: true, maxH: 420 });
    const ind = setTable(["statement", "triple"], t.indirect.map((q) => [q.statement, q.points_to]), { mono: true, maxH: 420 });
    return h("div", { class: "stack" },
      h("div", { class: "card" }, h("div", { class: "card-h" }, "Three-address code", h("span", { class: "sub" }, "syntax-directed translation of the AST; IF becomes conditional jumps")),
        h("div", { class: "card-b lab-3" }, h("div", null, h("div", { class: "lbl small faint" }, "Quadruples"), quads), h("div", null, h("div", { class: "lbl small faint" }, "Triples"), trip),
          h("div", null, h("div", { class: "lbl small faint" }, "Indirect triples"), ind))),
      h("div", { class: "sg-cols" },
        h("div", { class: "card" }, h("div", { class: "card-h" }, "Basic blocks & flow graph", h("span", { class: "sub" }, "leaders: first instruction, jump targets, instructions after jumps")),
          h("div", { class: "card-b stack" }, t.blocks.map((b, i) => h("div", { class: "bblock" + (i === blk ? " on" : ""), onclick: () => { L.block = i; render(); } },
            h("div", { class: "row" }, h("b", null, b.id), h("span", { class: "faint small" }, "→ " + (b.succ.join(", ") || "exit"))), h("pre", { class: "mono small", style: "margin:4px 0 0" }, b.code.join("\n")))))),
        h("div", { class: "card" }, h("div", { class: "card-h" }, `DAG of block ${t.dags[blk] ? t.dags[blk].block : ""}`, h("span", { class: "sub" }, "built by value numbering; orange = names attached to a node")),
          h("div", { class: "card-b stack" }, t.dags[blk] ? dagSvgBlock(t.dags[blk]) : null,
            h("div", { class: "small faint" }, "A node with several attached names is a common subexpression; a leaf labelled with a constant shows constant folding.")))),
      h("div", { class: "card" }, h("div", { class: "card-h" }, "Optimized three-address code", h("span", { class: "sub" }, `${t.counts.before} → ${t.counts.after} instructions`)),
        h("div", { class: "card-b sg-cols" }, h("pre", { class: "code" }, t.optimized.map((q) => q.text).join("\n")),
          h("div", null, h("div", { class: "lbl small faint" }, "Transformations applied"), t.log.length ? h("ul", { class: "small", style: "margin:4px 0;padding-left:18px" }, t.log.map((x) => h("li", null, x))) : h("div", { class: "faint small" }, "nothing to optimize")))),
      r.workbook_tac ? workbookTacCard(r.workbook_tac) : null);
  }
  function workbookTacCard(w) {
    const pc = w.pass_counts || {};
    return h("div", { class: "card" }, h("div", { class: "card-h" }, "Whole workbook as one TAC program", h("span", { class: "sub" }, "every formula cell, in topological order")),
      h("div", { class: "card-b stack" },
        h("div", { class: "kpis" }, kpi(w.before, "TAC instructions"), kpi(w.after, "after optimization"), kpi(pc["common subexpression"] || 0, "common subexpressions"),
          kpi(pc["constant folding"] || 0, "constant folds"), kpi(pc["dead code"] || 0, "dead instructions"), kpi(w.asm_stats.instructions, "machine instructions")),
        h("details", null, h("summary", { class: "small faint", style: "cursor:pointer" }, "Optimized TAC listing"), h("pre", { class: "code", style: "margin-top:6px" }, w.optimized.join("\n")))));
  }
  function labTarget(r) {
    const t = r.tac, L = S.lab;
    if (t.error) return h("div", { class: "card card-b" }, h("pre", { class: "caret" }, t.error.pretty));
    const st = t.target.stats, un = t.target_unoptimized;
    return h("div", { class: "sg-cols" },
      h("div", { class: "card" }, h("div", { class: "card-h" }, "Target code", h("span", { class: "sub" }, `load/store machine with ${t.target.registers} registers`), h("span", { class: "sg-spacer" }),
        h("select", { onchange: (e) => { L.nregs = +e.target.value; labLoad(); } }, [2, 3, 4, 8].map((n) => h("option", { value: n, selected: n === L.nregs ? "selected" : null }, n + " registers")))),
        h("div", { class: "card-b" }, h("pre", { class: "code", html: highlightAsm(t.target.asm.join("\n")) }))),
      h("div", { class: "stack" },
        h("div", { class: "card" }, h("div", { class: "card-h" }, "Effect of optimization on the generated code"),
          h("div", { class: "card-b" }, setTable(["", "instructions", "loads", "stores", "spills"], [["unoptimized TAC", un.instructions, un.loads, un.stores, un.spills], ["optimized TAC", st.instructions, st.loads, st.stores, st.spills]]))),
        h("div", { class: "card" }, h("div", { class: "card-h" }, "The simple code generator"), h("div", { class: "card-b small stack" },
          h("p", null, h("b", null, "Register descriptors"), " record which variables each register holds; ", h("b", null, "address descriptors"), " record where the current value of each variable lives (register and/or memory)."),
          h("p", null, h("b", null, "getReg"), " reuses a register already holding an operand, then an empty one, then a register whose value is safe in memory or dead (next-use information); otherwise it ", h("b", null, "spills"), " with ST. At the end of a basic block, live values are stored."),
          h("p", null, "Try 2 registers to watch spills appear.")))));
  }
  function highlightAsm(src) {
    return esc(src).replace(/^(\s*)([A-Z]{2,4})\b/gm, '$1<span class="k">$2</span>').replace(/\b(R\d+)\b/g, '<span class="f">$1</span>')
      .replace(/(#[-\d.]+)/g, '<span class="n">$1</span>').replace(/^(L\d+:)/gm, '<span class="c">$1</span>');
  }


  // ------------------------------------------------------------------ extensions (deck's "future work", built)
  const EXT_SECTIONS = [["targets", "JavaScript & SQL back-ends"], ["diff", "Semantic diff"], ["fuzz", "Fuzz testing"]];
  function extLoad(kind) {
    const X = S.ext;
    X[kind] = { loading: true };
    render();
    const payload = Object.assign({}, S.source || { demo: true });
    const name = { targets: "multitarget", diff: "diff", fuzz: "fuzz" }[kind];
    if (kind === "diff" && X.diffFiles && X.diffFiles.old && X.diffFiles.new) {
      api("diff", { old: X.diffFiles.old, new: X.diffFiles.new }).then((d) => { X.diff = d; render(); }).catch((e) => { X.diff = { error: e.message }; render(); });
      return;
    }
    api(name, payload).then((d) => { X[kind] = d; render(); }).catch((e) => { X[kind] = { error: e.message }; render(); });
  }
  function verifyChip(v) {
    if (!v) return null;
    if (!v.ran) return h("span", { class: "chip warn" }, "not executed: " + v.reason);
    if (v.cells == null) return h("span", { class: "chip err" }, "failed: " + v.reason);
    if (v.ok) return h("span", { class: "chip ok" }, `✓ executed: ${v.matching}/${v.cells} cells identical to the interpreter`);
    if (v.ok_except_flagged) return h("span", { class: "chip warn" }, `executed: ${v.matching}/${v.cells} identical; every difference traces to a cell SheetGuard already flagged`);
    return h("span", { class: "chip err" }, `executed: ${v.matching}/${v.cells} identical`);
  }
  function viewExt() {
    const X = S.ext;
    const sec = X.section;
    if (!X[sec]) setTimeout(() => extLoad(sec), 0);
    const head = h("div", { class: "card" }, h("div", { class: "card-h" }, "Extensions", h("span", { class: "sub" }, "the “future work” from our proposal, implemented")),
      h("div", { class: "card-b row" }, h("div", { class: "seg" }, EXT_SECTIONS.map(([id, label]) => h("button", { class: sec === id ? "on" : "", onclick: () => { X.section = id; render(); } }, label))),
        h("span", { class: "sg-spacer" }), h("button", { class: "btn sm", onclick: () => extLoad(sec) }, "↻ Re-run")));
    const d = X[sec];
    let body;
    if (!d || d.loading) body = h("div", { class: "empty" }, h("span", { class: "spinner" }), " working…");
    else if (d.error) body = h("div", { class: "card card-b", style: "color:var(--err)" }, d.error);
    else if (sec === "targets") body = extTargets(d);
    else if (sec === "diff") body = extDiff(d);
    else body = extFuzz(d);
    return h("div", { class: "stack" }, head, body);
  }
  function extTargets(d) {
    const X = S.ext, which = X.lang || "sql";
    const t = which === "sql" ? d.sql : d.javascript;
    const diffs = t.verification && t.verification.diffs || [];
    return h("div", { class: "stack" },
      h("div", { class: "sg-cols" },
        h("div", { class: "card" }, h("div", { class: "card-h" }, "One IR, more targets"), h("div", { class: "card-b small stack" },
          h("p", null, "The same optimized ASTs and the same topological order drive three back-ends: Python/NumPy (Compiler output tab), ", h("b", null, "JavaScript"), " and ", h("b", null, "SQL"), "."),
          h("p", null, h("b", null, "SQL: "), `every level of the topological sort becomes one common table expression (${d.sql.levels} CTEs). A cell may only read columns of earlier CTEs, which is exactly what topological order guarantees. Errors become NULL, which propagates like an error, and IFERROR becomes COALESCE.`),
          h("p", null, h("b", null, "Verification by execution: "), "the generated JavaScript is run with Node.js and the SQL with SQLite, and every formula cell is compared with the interpreter."))),
        h("div", { class: "card" }, h("div", { class: "card-h" }, "Execution results"), h("div", { class: "card-b stack" },
          h("div", { class: "row" }, h("b", { style: "width:90px" }, "JavaScript"), verifyChip(d.javascript.verification)),
          h("div", { class: "row" }, h("b", { style: "width:90px" }, "SQL"), verifyChip(d.sql.verification)),
          Object.keys(t.unsupported || {}).length ? h("div", { class: "small faint" }, "Not translatable (shown as errors / NULL): " + Object.entries(t.unsupported).map(([k, v]) => `${k} (${v})`).join(", ")) : null,
          diffs.length ? setTable(["Cell", "Interpreter", which.toUpperCase(), "Why"], diffs.map((x) => [x.cell, x.interpreter, x.target, x.explained || "—"]), { maxH: 240, wrap: true }) : null))),
      h("div", { class: "card" }, h("div", { class: "card-h" }, "Generated code", h("span", { class: "sg-spacer" }),
        h("div", { class: "seg" }, h("button", { class: which === "sql" ? "on" : "", onclick: () => { X.lang = "sql"; render(); } }, `SQL · ${d.sql.lines} lines`),
          h("button", { class: which === "js" ? "on" : "", onclick: () => { X.lang = "js"; render(); } }, `JavaScript · ${d.javascript.lines} lines`)),
        h("button", { class: "btn sm", onclick: () => copyText(t.source, "Code copied") }, "Copy")),
        h("div", { class: "card-b" }, h("pre", { class: "code", html: which === "sql" ? highlightSql(t.source) : highlightPy(t.source) }))));
  }
  function highlightSql(src) {
    return esc(src).replace(/(\/\*.*?\*\/|--[^\n]*)/g, '<span class="c">$1</span>')
      .replace(/\b(WITH|AS|SELECT|FROM|CASE|WHEN|THEN|ELSE|END|NULL|AND|OR|NOT|CAST|TEXT|INTEGER|COALESCE|POWER|ROUND|MIN|MAX)\b/g, '<span class="k">$1</span>')
      .replace(/(&quot;[^&]*?&quot;)/g, '<span class="f">$1</span>');
  }
  function extDiff(d) {
    const X = S.ext;
    const pick = (slot) => h("input", { type: "file", accept: ".xlsx,.xlsm", onchange: (e) => {
      const f = e.target.files[0]; if (!f) return;
      const rd = new FileReader(); rd.onload = () => { X.diffFiles = X.diffFiles || {}; X.diffFiles[slot] = { xlsx_base64: String(rd.result).split(",")[1], name: f.name }; if (X.diffFiles.old && X.diffFiles.new) extLoad("diff"); else render(); }; rd.readAsDataURL(f);
    } });
    const statusChip = (st) => h("span", { class: "chip " + ({ "formula changed": "brand", "cosmetic": "", "value changed": "info", "value → formula": "ok", "formula → value": "warn", added: "ok", removed: "err", moved: "info" }[st] || "") }, st);
    const iss = d.issues;
    return h("div", { class: "stack" },
      h("div", { class: "card" }, h("div", { class: "card-h" }, "Semantic diff", h("span", { class: "sub" }, `${d.old} → ${d.new}`)),
        h("div", { class: "card-b stack" }, h("div", { class: "summary-text" }, d.summary),
          h("div", { class: "kpis" }, Object.entries(d.counts).map(([k, v]) => kpi(v, k)), kpi(d.ripple.length, "changed as a consequence"), kpi(iss.resolved.length, "issues resolved"), kpi(iss.introduced.length, "issues introduced")),
          SNAP ? null : h("div", { class: "row small" }, h("span", { class: "faint" }, "Compare your own versions:"), "old", pick("old"), "new", pick("new")))),
      h("div", { class: "card" }, h("div", { class: "card-h" }, "Changed cells", h("span", { class: "sub" }, "ASTs are aligned top-down; only the smallest changed subtrees are reported")),
        h("div", { class: "card-b" }, setTable(["Cell", "Change", "Before", "After", "What changed (AST level)", "Value"], d.cells.map((c) => [
          h("span", { class: "chip link mono tiny", onclick: () => select(c.cell) }, c.cell), statusChip(c.status),
          h("span", { class: "mono small strike" }, c.old == null ? "" : String(c.old)), h("span", { class: "mono small", style: "color:var(--ok);font-weight:600" }, c.new == null ? "" : String(c.new)),
          h("div", { class: "small" }, (c.changes || []).map((x) => h("div", null, h("b", null, x.kind), ": ", h("span", { class: "mono" }, x.from), " → ", h("span", { class: "mono" }, x.to), x.detail ? h("span", { class: "faint" }, " (" + x.detail + ")") : null)), c.note ? h("div", { class: "faint" }, c.note) : null),
          h("span", { class: "mono small" }, c.old_value != null ? `${c.old_value} → ${c.new_value}` : "")]), { wrap: true }))),
      h("div", { class: "sg-cols" },
        h("div", { class: "card" }, h("div", { class: "card-h" }, "Ripple effect", h("span", { class: "sub" }, "cells whose formula did not change but whose value did")),
          h("div", { class: "card-b" }, d.ripple.length ? setTable(["Cell", "Before", "After", "Because of"], d.ripple.map((r) => [r.cell, r.old_value, r.new_value, r.because_of.join(", ")]), { maxH: 360, mono: true }) : h("div", { class: "faint" }, "none"))),
        h("div", { class: "card" }, h("div", { class: "card-h" }, "Issues", h("span", { class: "sub" }, `${iss.old_counts.error}E/${iss.old_counts.warning}W → ${iss.new_counts.error}E/${iss.new_counts.warning}W`)),
          h("div", { class: "card-b stack" }, h("div", null, h("div", { class: "lbl small faint" }, `Resolved (${iss.resolved.length})`), iss.resolved.map((x) => h("div", { class: "small" }, h("span", { class: "chip ok tiny" }, x.code), " ", x.cell, " — ", x.title))),
            h("div", null, h("div", { class: "lbl small faint" }, `Introduced (${iss.introduced.length})`), iss.introduced.length ? iss.introduced.map((x) => h("div", { class: "small" }, h("span", { class: "chip err tiny" }, x.code), " ", x.cell, " — ", x.title)) : h("div", { class: "small faint" }, "none"))))));
  }
  function extFuzz(d) {
    return h("div", { class: "stack" },
      h("div", { class: "card" }, h("div", { class: "card-h" }, "Type-guided fuzz testing", h("span", { class: "sub" }, "dynamic analysis: find inputs that break formulas which look fine today")),
        h("div", { class: "card-b stack" },
          h("div", { class: "kpis" }, kpi(d.inputs, "inputs fuzzed"), kpi(d.runs, "test cases run"), kpi(d.ms + " ms", "total time"), kpi((d.runs_per_second || 0).toLocaleString(), "runs / second"), kpi(d.sensitivity_coverage + "%", "formulas exercised"), kpi(d.plausible.length, "latent bugs found")),
          h("div", { class: "small muted" }, "Test values come from each input's inferred type: percentages get 0 %, 100 %, 99.9 %; money gets 0, 1 cent, 10 million; whole columns are set to 0 (“nothing sold”). Every case runs through the compiled code, which is why hundreds of runs take milliseconds."))),
      h("div", { class: "card" }, h("div", { class: "card-h" }, "Latent errors reachable with ordinary inputs"),
        h("div", { class: "card-b stack" }, d.plausible.length ? d.plausible.map((f) => h("div", { class: "issue warning" },
          h("div", { class: "issue-h" }, h("span", { class: "sev-dot warning" }), h("div", { style: "flex:1" },
            h("div", { class: "row" }, h("span", { class: "issue-title" }, f.label), h("span", { class: "chip link mono tiny", onclick: () => select(f.cell) }, f.cell), h("span", { class: "chip err tiny" }, f.error), h("span", { class: "chip tiny" }, "SG601")),
            h("div", { class: "issue-msg" }, h("span", { class: "mono" }, f.formula), " fails when ", h("b", null, f.witness)))),
          h("div", { class: "issue-b" }, h("div", { class: "diff" }, h("div", { class: "del" }, "− " + f.formula), h("div", { class: "add" }, "+ " + f.suggested_formula)),
            h("div", { class: "small faint" }, `${f.triggers} generated input(s) trigger this.`)))) : h("div", { class: "empty" }, "✓ none found"))),
      d.adversarial_groups.length ? h("div", { class: "card" }, h("div", { class: "card-h" }, "Fragile to unusual inputs", h("span", { class: "sub" }, "text or negative values typed where numbers are expected")),
        h("div", { class: "card-b small stack" }, d.adversarial_groups.map((g) => h("div", null, h("b", null, `${g.count} formula(s) → ${g.error}`), `, e.g. ${g.example.cell} when ${g.example.witness}.`)),
          h("div", { class: "faint" }, "Expected behaviour for a spreadsheet; data validation on input cells prevents it."))) : null);
  }

  // ------------------------------------------------------------------ actions
  function select(key) {
    S.selected = key;
    if (key) S.sheet = splitKey(key).sheet;
    if (MODE === "web" && !["workbook", "structure", "verify"].includes(S.tab)) S.tab = "workbook";
    render();
  }
  function goto(key) {
    S.selected = key;
    if (MODE === "sidebar") { gs("jumpTo", key).catch(() => {}); if (S.tab === "deliver") S.tab = "structure"; }
    else if (S.tab !== "workbook" && S.tab !== "structure" && S.tab !== "verify") S.tab = "workbook";
    if (MODE === "web") { const k = splitKey(key); S.sheet = k.sheet; }
    render();
  }
  function setCellInWorkbook(wb, key, raw) {
    const k = splitKey(key);
    const sh = wb.sheets.find((x) => x.name === k.sheet);
    if (!sh) return;
    if (Array.isArray(sh.cells)) {                     // sparse form: [row, col, raw, value, fmt]
      const i = sh.cells.findIndex((e) => e[0] === k.row && e[1] === k.col);
      const fmt = i >= 0 ? sh.cells[i][4] : "";
      const entry = [k.row, k.col, raw, null, fmt];
      if (i >= 0) sh.cells[i] = entry; else sh.cells.push(entry);
      return;
    }
    for (const f of ["formulas", "values", "formats"]) {
      sh[f] = sh[f] || [];
      while (sh[f].length < k.row) sh[f].push([]);
      while (sh[f][k.row - 1].length < k.col) sh[f][k.row - 1].push("");
    }
    if (typeof raw === "string" && raw.startsWith("=")) { sh.formulas[k.row - 1][k.col - 1] = raw; sh.values[k.row - 1][k.col - 1] = ""; }
    else { sh.formulas[k.row - 1][k.col - 1] = ""; sh.values[k.row - 1][k.col - 1] = raw; }
  }
  async function applyFix(d) {
    const after = d.suggested_formula;
    if (!S.before) S.before = S.report;
    if (SNAP && S.fixed.size) { toast("The hosted demo replays one fix at a time. Use “Apply all fixes”, or run SheetGuard locally.", true); return; }
    try {
      if (MODE === "sidebar") { await gs("applyFix", d.cell, String(after)); toast(`Fixed ${d.cell}`); }
      else { setCellInWorkbook(S.workbookJson, d.cell, typedLiteral(after)); }
      S.fixed.add(d.cell);
      await analyze(true);
    } catch (e) { toast(e.message || String(e), true); }
  }
  // the workbook JSON is typed (a string "78" means text), so a fix that turns
  // text into a number must store a real number
  function typedLiteral(v) {
    return typeof v === "string" && v.trim() !== "" && !v.startsWith("=") && !isNaN(+v) ? +v : v;
  }
  async function applyAll() {
    const fixes = S.report.fixes.slice();
    if (!fixes.length) return;
    if (!S.before) S.before = S.report;
    try {
      if (MODE === "sidebar") { await gs("applyFixes", JSON.stringify(fixes)); }
      else fixes.forEach((f) => setCellInWorkbook(S.workbookJson, f.cell, typedLiteral(f.after)));
      fixes.forEach((f) => S.fixed.add(f.cell));
      toast(`Applied ${fixes.length} fixes`);
      await analyze(true);
    } catch (e) { toast(e.message || String(e), true); }
  }
  async function downloadXlsx() {
    if (SNAP) { toast("Downloads are not available on the hosted page. Run SheetGuard locally to export the annotated .xlsx.", true); return; }
    try {
      toast("Building annotated workbook…");
      const d = await api("export", Object.assign({}, MODE === "sidebar" ? { useSheet: true } : S.source, { fix: true }));
      const bin = atob(d.xlsx_base64), arr = new Uint8Array(bin.length);
      for (let i = 0; i < bin.length; i++) arr[i] = bin.charCodeAt(i);
      const url = URL.createObjectURL(new Blob([arr], { type: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" }));
      const a = h("a", { href: url, download: d.filename });
      document.body.append(a); a.click(); a.remove();
    } catch (e) { toast(e.message || String(e), true); }
  }
  async function createCopy() {
    try {
      toast("Creating corrected copy in your Drive…");
      const notes = S.report.diagnostics.filter((d) => d.cell).map((d) => ({ cell: d.cell, sev: d.severity, note: `${d.title}: ${d.message}\n${d.explanation}\nFix: ${d.fix}` }));
      S.copyUrl = await gs("createCorrectedCopy", JSON.stringify(S.report.fixes), JSON.stringify(notes));
      render();
    } catch (e) { toast(e.message || String(e), true); }
  }
  function highlightSheet() {
    if (MODE !== "sidebar" || !S.report) return;
    const items = S.report.diagnostics.filter((d) => d.cell).map((d) => ({ cell: d.cell, sev: d.severity, note: `SheetGuard ${d.code} — ${d.title}\n${d.message}\n\nWhy: ${d.explanation}\nFix: ${d.fix}` }));
    (S.highlight ? gs("highlight", JSON.stringify(items)) : gs("clearHighlights")).catch((e) => toast(e.message || String(e), true));
  }

  async function analyze(keepSelection) {
    S.loading = true; S.error = null;
    render();
    try {
      let payload;
      if (MODE === "sidebar") payload = S.sourceDemo ? { demo: true } : { useSheet: true };
      else payload = S.source || { demo: true };
      payload = Object.assign({}, payload, { return_workbook: MODE === "web" });
      const rep = await api("analyze", payload);
      S.report = rep;
      if (MODE === "web" && rep.source_workbook) { S.workbookJson = rep.source_workbook; S.source = { workbook: S.workbookJson }; }
      S.code = null;
      if (!keepSelection || (S.selected && !rep.cells[S.selected] && !gridCell(S.selected))) {
        const firstErr = rep.diagnostics.find((d) => d.cell && rep.cells[d.cell]);
        S.selected = keepSelection ? S.selected : firstErr ? firstErr.cell : null;
      }
      if (S.selected) S.sheet = splitKey(S.selected).sheet;
      if (MODE === "sidebar") highlightSheet();
    } catch (e) {
      S.error = e.message || String(e);
    }
    S.loading = false;
    render();
  }
  function readFile(file) {
    const rd = new FileReader();
    if (/\.json$/i.test(file.name)) {
      rd.onload = () => { try { S.source = { workbook: JSON.parse(rd.result) }; S.fixed.clear(); S.before = null; analyze(); } catch (e) { toast("Invalid JSON workbook", true); } };
      rd.readAsText(file);
      return;
    }
    rd.onload = () => {
      const b64 = String(rd.result).split(",")[1];
      S.source = { xlsx_base64: b64, name: file.name };
      S.fixed.clear(); S.before = null; S.selected = null;
      analyze();
    };
    rd.readAsDataURL(file);
  }

  // ------------------------------------------------------------------ views
  function viewOverview() {
    const rep = S.report;
    return h("div", { class: "stack" },
      h("div", { class: "card" }, h("div", { class: "card-h" }, "Compiler pipeline", h("span", { class: "sub" }, `analysed in ${rep.elapsed_ms} ms`)), phaseStrip(rep)),
      h("div", { class: "sg-cols" },
        h("div", { class: "stack" }, summaryCard(rep, true),
          h("div", { class: "card" }, h("div", { class: "card-h" }, "Most serious findings", h("span", { class: "sg-spacer" }), h("button", { class: "btn sm", onclick: () => { S.tab = "verify"; render(); } }, "All issues →")),
            h("div", { class: "card-b stack" }, rep.diagnostics.filter((d) => d.severity !== "info").slice(0, 5).map((d) => issueCard(d))))),
        h("div", { class: "stack" }, impactCard(rep, 8), dropZone())));
  }
  function dropZone() {
    if (SNAP) return h("div", { class: "drop" }, h("div", { style: "font-weight:600;margin-bottom:4px" }, "Analyze your own workbook"),
      h("div", { class: "small" }, "This hosted page replays the real compiler's output for the demo workbook. To analyze your own .xlsx, run ", h("span", { class: "kbd" }, "START_SheetGuard.bat"), " from the project folder."));
    const inp = h("input", { type: "file", accept: ".xlsx,.xlsm,.json", style: "display:none", onchange: (e) => e.target.files[0] && readFile(e.target.files[0]) });
    const z = h("div", { class: "drop" }, h("div", { style: "font-weight:600;margin-bottom:4px" }, "Analyze your own workbook"),
      h("div", { class: "small" }, "Drop an .xlsx file here, or ", h("a", { href: "#", onclick: (e) => { e.preventDefault(); inp.click(); } }, "browse"), ". It is processed by your local backend only."), inp);
    z.addEventListener("dragover", (e) => { e.preventDefault(); z.classList.add("over"); });
    z.addEventListener("dragleave", () => z.classList.remove("over"));
    z.addEventListener("drop", (e) => { e.preventDefault(); z.classList.remove("over"); e.dataTransfer.files[0] && readFile(e.dataTransfer.files[0]); });
    return z;
  }
  function inspectorCard() {
    return h("div", { class: "card", style: MODE === "web" ? "position:sticky;top:104px;max-height:calc(100vh - 120px);overflow:auto" : "" },
      h("div", { class: "card-h" }, "Cell inspector", h("span", { class: "sub" }, "how the compiler sees this cell")), h("div", { class: "card-b" }, inspector(S.selected)));
  }
  function viewWorkbook() { return h("div", { class: "sg-cols" }, h("div", { class: "card card-b" }, gridView()), inspectorCard()); }
  function viewStructure() {
    const rep = S.report;
    const dagCard = h("div", { class: "card" },
      h("div", { class: "card-h" }, "Dependency DAG", h("span", { class: "sub" }, `${rep.graph.stats.nodes} nodes · ${rep.graph.stats.edges} edges · ${rep.graph.stats.cycles} cycle(s)`),
        h("span", { class: "sg-spacer" }),
        h("label", { class: "small row", style: "gap:4px" }, h("input", { type: "checkbox", checked: S.dagAll ? "checked" : null, onchange: (e) => { S.dagAll = e.target.checked; render(); } }), "all inputs")),
      h("div", { class: "card-b stack" },
        dagView(rep, { expand: MODE === "sidebar" ? openGraph : null }), dagLegend(),
        rep.graph.cycles.length ? h("div", { class: "small", style: "color:var(--err)" }, "Cycle: ", rep.graph.cycles.map((c) => c.concat(c[0]).join(" → ")).join(";  ")) : null,
        MODE === "web" ? h("details", null, h("summary", { class: "small faint", style: "cursor:pointer" }, "Topological recalculation order (each level can run in parallel)"),
          h("div", { class: "small mono", style: "margin-top:6px" }, rep.graph.levels.map((lv, i) => h("div", null, h("b", null, `L${i + 1}: `), lv.join(", "))))) : null));
    const symCard = h("div", { class: "card" }, h("div", { class: "card-h" }, "Symbol table", h("span", { class: "sub" }, "scopes, named ranges, cross-sheet links")),
      h("div", { class: "card-b" }, h("table", { class: "t" }, h("thead", null, h("tr", null, h("th", null, "Symbol"), h("th", null, "Kind"), h("th", null, "Scope"), h("th", null, "Definition"), h("th", { class: "r" }, "Uses"))),
        h("tbody", null, rep.symbols.symbols.filter((x) => MODE === "web" || x.kind !== "function").map((x) => h("tr", null, h("td", { class: "mono" }, x.name), h("td", null, x.kind), h("td", { class: "small" }, x.scope), h("td", { class: "small mono" }, x.definition), h("td", { class: "r" }, x.uses))))),
        rep.symbols.links.length ? h("div", { class: "small", style: "margin-top:8px" }, h("b", null, "Link map: "), rep.symbols.links.map((l) => `${l.from} → ${l.to} (${l.count})`).join(" · ")) : null));
    if (MODE === "sidebar") return h("div", { class: "stack" }, inspectorCard(), dagCard);
    return h("div", { class: "stack" }, dagCard, h("div", { class: "sg-cols" }, inspectorCard(), symCard));
  }
  function viewVerify() {
    const list = h("div", { class: "card" }, h("div", { class: "card-h" }, "Verification report", h("span", { class: "sub" }, "every finding has a cause and a fix")),
      h("div", { class: "card-b" }, issueList(S.report.diagnostics, true)));
    if (MODE === "sidebar" && window.innerWidth < 900) return list;     // the 300 px sidebar
    return h("div", { class: "sg-cols" }, list, inspectorCard());         // web app + large view
  }
  function viewDeliver() {
    const rep = S.report;
    const actions = h("div", { class: "card" }, h("div", { class: "card-h" }, "Deliverables"),
      h("div", { class: "card-b stack" },
        h("div", { class: "row" },
          rep.fixes.length ? h("button", { class: "btn ok", onclick: applyAll }, `Apply all ${rep.fixes.length} fixes${MODE === "sidebar" ? " to this sheet" : ""}`) : h("span", { class: "chip ok" }, "Nothing left to auto-fix"),
          MODE === "sidebar" ? h("button", { class: "btn", onclick: createCopy }, "Create corrected copy") : null,
          h("button", { class: "btn", onclick: downloadXlsx }, "Download annotated .xlsx")),
        S.copyUrl ? h("div", { class: "small" }, "✓ Corrected copy created: ", h("a", { href: S.copyUrl, target: "_blank" }, "open it")) : null,
        MODE === "sidebar" ? h("label", { class: "row small" }, h("input", { type: "checkbox", checked: S.highlight ? "checked" : null, onchange: (e) => { S.highlight = e.target.checked; highlightSheet(); } }), "Highlight flagged cells in the sheet") : null,
        h("div", { class: "small faint" }, "The annotated workbook colours every flagged cell, adds an explanatory comment to each one, applies the safe fixes (in green) and starts with a full “SheetGuard Report” sheet.")));
    const fixTable = rep.fixes.length ? h("div", { class: "card" }, h("div", { class: "card-h" }, "Planned automatic fixes"),
      h("div", { class: "card-b" }, h("table", { class: "t" }, h("thead", null, h("tr", null, h("th", null, "Cell"), h("th", null, "Before"), h("th", null, "After"), h("th", { class: "hide-sb" }, "Reason"))),
        h("tbody", null, rep.fixes.map((f) => h("tr", null, h("td", null, h("span", { class: "chip link mono tiny", onclick: () => goto(f.cell) }, f.cell)),
          h("td", { class: "mono small strike" }, String(f.before)), h("td", { class: "mono small", style: "color:var(--ok);font-weight:600" }, String(f.after)), h("td", { class: "small hide-sb" }, f.title))))))) : null;
    if (MODE === "sidebar") return h("div", { class: "stack" }, beforeAfterCard(), summaryCard(rep, false), actions, impactCard(rep, 10), fixTable);
    return h("div", { class: "sg-cols" }, h("div", { class: "stack" }, beforeAfterCard(), summaryCard(rep, true), impactCard(rep)), h("div", { class: "stack" }, actions, fixTable));
  }

  // ------------------------------------------------------------------ sidebar: settings & selection sync
  function settingsCard() {
    const c = S.config || {};
    const url = h("input", { value: c.backend || "", placeholder: "https://xxxx.trycloudflare.com", style: "width:100%;padding:6px;border:1px solid var(--border-2);border-radius:6px" });
    const key = h("input", { value: "", placeholder: c.hasKey ? "(key saved — leave blank to keep)" : "optional access key", style: "width:100%;padding:6px;border:1px solid var(--border-2);border-radius:6px" });
    return h("div", { class: "card" }, h("div", { class: "card-h" }, "Connect to the SheetGuard backend"),
      h("div", { class: "card-b stack small" },
        h("div", { class: "muted" }, "The compiler runs on your SheetGuard server. Start it with ", h("span", { class: "kbd" }, "START_SheetGuard.bat"), " and a tunnel (see SETUP guide), then paste its public URL."),
        url, key,
        h("div", { class: "row" }, h("button", { class: "btn primary sm", onclick: async () => {
          try { await gs("saveConfig", url.value.trim(), key.value.trim()); S.config = await gs("getConfig"); const hl = await api("health", {}); toast(`Connected · SheetGuard ${hl.version}`); S.showSettings = false; render(); }
          catch (e) { toast("Could not reach backend: " + (e.message || e), true); }
        } }, "Save & test"), S.config && S.config.backend ? h("button", { class: "btn sm", onclick: () => { S.showSettings = false; render(); } }, "Cancel") : null)));
  }
  let pollTimer = null;
  function startSelectionSync() {
    if (MODE !== "sidebar" || pollTimer) return;
    pollTimer = setInterval(() => {
      if (!S.report || S.tab !== "structure" || document.hidden) return;
      gs("getActiveCellKey").then((k) => { if (k && k !== S.selected) { S.selected = k; render(); } }).catch(() => {});
    }, 1500);
  }
  function openGraph() {
    const r = S.report;
    gs("openGraphDialog", JSON.stringify({ graph: r.graph, symbols: r.symbols, diagnostics: r.diagnostics.map((d) => ({ cell: d.cell, severity: d.severity })), grid: r.grid.map((g) => ({ name: g.name, cells: {} })), selected: S.selected })).catch((e) => toast(e.message || String(e), true));
  }

  // ------------------------------------------------------------------ guided demo tour (presenter mode)
  const TOUR = [
    { tab: "overview", title: "1 · The spreadsheet is the source program",
      text: "SheetGuard compiled a 3-sheet sales report through six classical phases in a few milliseconds. The health grade, the plain-English summary and the bug-impact table are all derived from compiler analyses — no AI guesswork." },
    { tab: "workbook", select: "Sales!D6", title: "2 · A real bug, hiding in plain sight",
      text: "Central's revenue is =B6*C5 — it multiplies by the row above. The sheet shows no error, yet the 'Best region' on the summary is wrong. Blue outlines = cells this formula reads, orange = cells that read it." },
    { tab: "play", play: "=SUM(Sales!B2:B9)*(1+TaxRate)", title: "3 · Lexer → Parser → AST",
      text: "Every formula is tokenized by a hand-written scanner (one-character lookahead tells LOG10( from the cell LOG10) and parsed by recursive descent into an abstract syntax tree. Edit the formula — it recompiles on every keystroke." },
    { tab: "play", play: "=SUM(Sales!D2:D9", title: "4 · Syntax errors with positions and recovery",
      text: "Like gcc, errors point at the exact column with a caret. Phrase-level error recovery then searches for the smallest edit that makes the formula valid." },
    { tab: "structure", select: "Summary!B4", title: "5 · Symbol table + dependency DAG",
      text: "Names like TaxRate are resolved through nested scopes (cross-sheet references are 'linked' like object files). Cells form a DAG: Tarjan's algorithm finds the red circular reference, Kahn's topological sort gives the recalculation levels." },
    { tab: "verify", select: "Sales!I4", title: "6 · Semantic analysis: types & units",
      text: "The type checker infers Number[currency], Number[percent], Date, Text… $ + % is a unit error (=G4+H4 adds 15% as 0.15 dollars). The smell detector compares formulas in R1C1 form to catch copy-paste, off-by-one, magic numbers and double counting." },
    { tab: "deliver", title: "7 · Deliverables: explain, fix, prove",
      text: "Each finding has a cause and a fix; 10 are repaired automatically. The impact table re-evaluates the workbook before and after: revenue was over-stated by 4.1% and the 'best region' by 15%." },
    { tab: "code", title: "8 · Optimizer + code generator",
      text: "Constant folding, dead-cell elimination and CSE run on the IR; the back-end emits straight-line Python with type-specialized fast paths (and deoptimization), or vectorized NumPy for formula columns." },
    { tab: "lab", title: "9 · The syllabus, mechanised",
      text: "The same language gets the textbook treatment: FIRST/FOLLOW sets and a conflict-free LL(1) table (so the grammar is provably LL(1)), an SLR(1) automaton with 79 states, three-address code as quadruples and triples, basic blocks, DAGs and a register-machine code generator." },
    { tab: "ext", title: "10 · Future work, built",
      text: "The proposal's future work is implemented: the same IR compiles to JavaScript and to one SQL query (a CTE per topological level), both verified by running them; a semantic diff compares workbook versions at AST level; and a type-guided fuzzer finds two latent #DIV/0! bugs that no static check can see." },
    { tab: "bench", title: "11 · Compilation pays off",
      text: "Run the benchmark: the same 10,000-formula model recalculated by five engines. Compiled + vectorized code is >1000× faster than re-interpreting formulas — and every engine is verified to give identical values." },
  ];
  function tourGo(i) {
    if (i < 0 || i >= TOUR.length) { S.tour = null; render(); return; }
    const st = TOUR[i];
    S.tour = i; S.tab = st.tab;
    if (st.select) { S.selected = st.select; S.sheet = splitKey(st.select).sheet; S.expanded.clear(); }
    if (st.play) { S.play.formula = st.play; S.play.result = null; }
    if (st.tab === "verify") S.filter = "all";
    render();
    window.scrollTo(0, 0);
  }
  function tourCard() {
    const i = S.tour, st = TOUR[i];
    return h("div", { class: "tour" },
      h("div", { class: "row", style: "margin-bottom:6px" }, h("span", { class: "chip brand" }, `Guided demo ${i + 1}/${TOUR.length}`), h("span", { class: "sg-spacer" }),
        h("button", { class: "btn sm ghost", onclick: () => tourGo(-1), title: "Exit (Esc)" }, "✕")),
      h("div", { class: "tour-t" }, st.title), h("div", { class: "tour-x" }, st.text),
      h("div", { class: "row", style: "margin-top:10px" },
        h("div", { class: "tour-dots" }, TOUR.map((_, k) => h("span", { class: k === i ? "on" : "", onclick: () => tourGo(k) }))), h("span", { class: "sg-spacer" }),
        i > 0 ? h("button", { class: "btn sm", onclick: () => tourGo(i - 1) }, "← Back") : null,
        h("button", { class: "btn sm primary", onclick: () => tourGo(i + 1 < TOUR.length ? i + 1 : -1) }, i + 1 < TOUR.length ? "Next →" : "Finish")));
  }
  document.addEventListener("keydown", (e) => {
    if (S.tour == null || /INPUT|SELECT|TEXTAREA/.test((e.target || {}).tagName)) return;
    if (e.key === "ArrowRight") tourGo(S.tour + 1 < TOUR.length ? S.tour + 1 : S.tour);
    else if (e.key === "ArrowLeft") tourGo(Math.max(0, S.tour - 1));
    else if (e.key === "Escape") tourGo(-1);
  });

  // ------------------------------------------------------------------ shell
  const TABS_WEB = [["overview", "Overview"], ["workbook", "Workbook"], ["structure", "Structure"], ["verify", "Verification"], ["deliver", "Deliverables"], ["code", "Compiler output"], ["bench", "Benchmark"], ["play", "Playground"], ["lab", "Syllabus lab"], ["ext", "Extensions"]];
  const TABS_SIDE = [["structure", "Structure"], ["verify", "Verification"], ["deliver", "Deliverables"]];
  const root = h("div", { class: "sg-app" });
  document.body.append(root);
  const bootMsg = document.getElementById("sg-boot");   // static "Loading…" / error box of the Sheets shells
  if (bootMsg) bootMsg.remove();

  function header() {
    const rep = S.report;
    const top = h("div", { class: "sg-top" }, h("div", { class: "sg-brand" }, logo(), "SheetGuard", MODE === "web" ? h("small", null, "compiler-based spreadsheet verification") : null));
    if (MODE === "web") {
      top.append(rep ? h("span", { class: "chip", title: "current workbook" }, "📄 ", rep.workbook.name) : null, h("span", { class: "sg-spacer" }));
      if (rep) top.append(h("div", { class: "health" }, ring(rep.health.score, rep.health.grade), countChips(rep.counts)));
      top.append(rep ? h("button", { class: "btn sm", onclick: () => tourGo(0), title: "Presenter mode: walk through every compiler phase" }, "▶ Guided demo") : null,
        h("button", { class: "btn sm", onclick: () => { S.source = { demo: true }; S.fixed.clear(); S.before = null; S.selected = null; analyze(); } }, "Load demo"),
        h("button", { class: "btn sm primary", onclick: () => analyze(true), disabled: S.loading }, S.loading ? h("span", { class: "spinner" }) : "↻", " Re-analyze"));
    } else {
      top.append(h("span", { class: "sg-spacer" }));
      if (rep) top.append(ring(rep.health.score, rep.health.grade, 34));
      if (window.innerWidth < 520) top.append(h("button", { class: "btn sm ghost", title: "Open large view (the sidebar is fixed at 300 px by Google Sheets)",
        onclick: () => gs("showLargeView").catch((e) => toast(e.message || String(e), true)) }, "⤢"));
      top.append(h("button", { class: "btn sm ghost", title: "Settings", onclick: () => { S.showSettings = !S.showSettings; render(); } }, "⚙"));
    }
    return top;
  }
  function render() {
    if (MODE === "graph") return renderGraphMode();
    const scrollY = window.scrollY;
    root.innerHTML = "";
    root.append(header());
    if (SNAP && MODE === "web") root.append(h("div", { class: "snap-banner" }, h("b", null, "Hosted demo."),
      ` Recorded output of the SheetGuard compiler (v${SNAP.version}) on the Acme Q3 demo workbook, captured ${SNAP.captured}. Every view is interactive; live compilation of new files runs locally.`));
    const main = h("div", { class: "sg-main" });
    if (MODE === "sidebar") {
      if (S.showSettings || (S.config && !S.config.backend)) main.append(settingsCard());
      main.append(h("div", { class: "row", style: "margin-bottom:10px" },
        h("button", { class: "btn primary", style: "flex:1;justify-content:center", onclick: () => { S.sourceDemo = false; analyze(!!S.report); }, disabled: S.loading || !(S.config && S.config.backend) },
          S.loading ? [h("span", { class: "spinner" }), " Compiling sheet…"] : S.report ? "↻ Re-analyze sheet" : "▶ Analyze this sheet")));
      if (S.report) main.append(h("div", { class: "row", style: "margin:-4px 0 8px" }, countChips(S.report.counts), h("span", { class: "tiny faint" }, `${S.report.elapsed_ms} ms`)));
    }
    if (S.error) main.append(h("div", { class: "card card-b", style: "color:var(--err);margin-bottom:12px" }, "⚠ ", S.error));
    if (!S.report) {
      if (S.loading) main.append(h("div", { class: "empty" }, h("span", { class: "spinner" }), " running the compiler pipeline…"));
      else if (MODE === "sidebar") main.append(sidebarWelcome());
      root.append(main);
      return;
    }
    const tabs = MODE === "web" ? TABS_WEB : TABS_SIDE;
    const tabBar = h("div", { class: "sg-tabs" }, tabs.map(([id, label]) => h("button", { class: "sg-tab" + (S.tab === id ? " on" : ""), onclick: () => { S.tab = id; render(); } },
      label, id === "verify" ? h("span", { class: "n" }, S.report.diagnostics.length) : null)));
    if (MODE === "sidebar") root.append(tabBar); else root.append(tabBar);
    const views = { overview: viewOverview, workbook: viewWorkbook, structure: viewStructure, verify: viewVerify, deliver: viewDeliver, code: codeView, bench: benchView, play: playgroundView, lab: viewLab, ext: viewExt };
    main.append((views[S.tab] || viewVerify)());
    root.append(main);
    if (MODE === "web" && S.tour != null) root.append(tourCard());
    window.scrollTo(0, scrollY);
  }
  function sidebarWelcome() {
    return h("div", { class: "card" }, h("div", { class: "card-b stack" },
      h("div", { style: "font-weight:600" }, "Verify this spreadsheet like source code"),
      h("div", { class: "small muted" }, "SheetGuard sends the formulas of this sheet to its compiler (lexer → parser → symbol table → dependency DAG → type checker → smell detector → optimizer) and shows:"),
      h("ul", { class: "small", style: "margin:0;padding-left:18px" }, h("li", null, h("b", null, "Structure"), " — AST of the selected cell & dependency graph"),
        h("li", null, h("b", null, "Verification"), " — every bug with cause and fix"), h("li", null, h("b", null, "Deliverables"), " — summary, impact, corrected copy")),
      S.config && S.config.backend ? h("button", { class: "btn sm", onclick: () => { S.sourceDemo = true; analyze(); } }, "…or try it on the built-in demo workbook") : null));
  }

  // ------------------------------------------------------------------ graph dialog mode
  function renderGraphMode() {
    const data = window.SG_DATA;
    root.innerHTML = "";
    const rep = { graph: data.graph, symbols: data.symbols, diagnostics: data.diagnostics, grid: data.grid };
    let sel = data.selected;
    const box = h("div");
    const draw = () => {
      box.innerHTML = "";
      box.append(dagView(rep, { selected: sel, all: S.dagAll, onSelect: (id) => { sel = id; draw(); if (IS_GS) google.script.run.jumpTo(id); } }));
    };
    root.append(h("div", { class: "sg-top" }, h("div", { class: "sg-brand" }, logo(), "Dependency DAG"), dagLegend(), h("span", { class: "sg-spacer" }),
      h("label", { class: "small row" }, h("input", { type: "checkbox", onchange: (e) => { S.dagAll = e.target.checked; draw(); } }), "all inputs"),
      h("span", { class: "small faint" }, "scroll = zoom · drag = pan · click = go to cell")), box);
    draw();
  }

  // ------------------------------------------------------------------ boot
  window.SG = { api, render, analyze, state: S };
  if (MODE === "graph") { render(); return; }
  if (MODE === "sidebar") {
    render();
    gs("getConfig").then((c) => { S.config = c; render(); startSelectionSync(); }).catch((e) => { S.config = { backend: "" }; S.error = String(e.message || e); render(); });
  } else {
    S.source = { demo: true };
    analyze();
  }
})();
