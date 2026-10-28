/**
 * SheetGuard — Google Sheets add-on (container-bound Apps Script)
 * ---------------------------------------------------------------
 * Paste this file as Code.gs, plus Sidebar.html and Graph.html, into
 * Extensions ▸ Apps Script of your spreadsheet. Nothing is published to the
 * Workspace Marketplace: the script lives only inside this one spreadsheet.
 *
 * The sidebar reads the formulas, values, number formats and named ranges of
 * this spreadsheet, sends them as JSON to your SheetGuard backend (the Python
 * compiler), and renders the three views. It can also highlight problem cells,
 * jump to them, apply fixes and create a corrected copy.
 */

var PROP_BACKEND = 'SG_BACKEND';
var PROP_KEY = 'SG_KEY';
var PROP_ORIG = 'SG_ORIGINALS';
var COLORS = { error: '#fecaca', warning: '#fde68a', info: '#dbeafe', fixed: '#bbf7d0' };

// ------------------------------------------------------------------ menu & sidebar
function onOpen(e) {
  SpreadsheetApp.getUi()
    .createMenu('🛡 SheetGuard')
    .addItem('Open SheetGuard', 'showSidebar')
    .addItem('Open large view', 'showLargeView')
    .addSeparator()
    .addItem('Set backend URL…', 'promptBackend')
    .addItem('Clear highlights', 'clearHighlights')
    .addToUi();
}

function onInstall(e) { onOpen(e); }

function showSidebar() {
  var html = HtmlService.createHtmlOutputFromFile('Sidebar')
    .setTitle('SheetGuard')
    .setSandboxMode(HtmlService.SandboxMode.IFRAME);
  SpreadsheetApp.getUi().showSidebar(html);
}

/**
 * Google Sheets fixes every sidebar at 300 px. The large view shows the same UI in a
 * resizable window that floats over the sheet (modeless: you can still click cells,
 * and "Go to cell" / highlights work exactly as in the sidebar).
 */
function showLargeView() {
  var html = HtmlService.createHtmlOutputFromFile('Sidebar')
    .setWidth(1100).setHeight(740)
    .setSandboxMode(HtmlService.SandboxMode.IFRAME);
  SpreadsheetApp.getUi().showModelessDialog(html, 'SheetGuard');
}

function openGraphDialog(dataJson) {
  var t = HtmlService.createTemplateFromFile('Graph');
  t.data = String(dataJson).replace(/<\//g, '<\\/');   // safe inside <script>
  var html = t.evaluate().setWidth(1200).setHeight(780);
  SpreadsheetApp.getUi().showModalDialog(html, 'SheetGuard — dependency graph');
}

// ------------------------------------------------------------------ configuration
function getConfig() {
  var p = PropertiesService.getDocumentProperties();
  return {
    backend: p.getProperty(PROP_BACKEND) || '',
    hasKey: !!p.getProperty(PROP_KEY),
    spreadsheet: SpreadsheetApp.getActive().getName()
  };
}

function saveConfig(url, key) {
  var p = PropertiesService.getDocumentProperties();
  url = String(url || '').trim().replace(/\/+$/, '');
  if (url && !/^https?:\/\//.test(url)) url = 'https://' + url;
  p.setProperty(PROP_BACKEND, url);
  if (key) p.setProperty(PROP_KEY, String(key).trim());
  return getConfig();
}

function promptBackend() {
  var ui = SpreadsheetApp.getUi();
  var cur = getConfig().backend;
  var r = ui.prompt('SheetGuard backend', 'Public URL of your SheetGuard server' +
    (cur ? ' (current: ' + cur + ')' : '') + ':', ui.ButtonSet.OK_CANCEL);
  if (r.getSelectedButton() === ui.Button.OK) {
    saveConfig(r.getResponseText(), '');
    ui.alert('Saved. Open SheetGuard from the menu and press Analyze.');
  }
}

// ------------------------------------------------------------------ reading the workbook
var TZ_ = null;
function serialDate_(d) {
  // spreadsheet serial number (days since 1899-12-30), in the spreadsheet's time zone
  var tz = TZ_ || (TZ_ = SpreadsheetApp.getActive().getSpreadsheetTimeZone());   // one API call
  var s = Utilities.formatDate(d, tz, 'yyyy-MM-dd HH:mm:ss').split(/[- :]/);
  var utc = Date.UTC(+s[0], +s[1] - 1, +s[2], +s[3], +s[4], +s[5]);
  return (utc - Date.UTC(1899, 11, 30)) / 86400000;
}

function quoteSheet_(name) {
  return /^[A-Za-z_][A-Za-z0-9_.]*$/.test(name) && !/^[A-Za-z]{1,3}[0-9]+$/.test(name)
    ? name : "'" + name.replace(/'/g, "''") + "'";
}

function readWorkbook() {
  var ss = SpreadsheetApp.getActive();
  var sheets = ss.getSheets().map(function (sh) {
    var rng = sh.getDataRange();
    var values = rng.getValues().map(function (row) {
      return row.map(function (v) { return v instanceof Date ? serialDate_(v) : v; });
    });
    return {
      name: sh.getName(),
      formulas: rng.getFormulas(),
      values: values,
      formats: rng.getNumberFormats()
    };
  });
  var names = ss.getNamedRanges().map(function (nr) {
    var r = nr.getRange();
    return { name: nr.getName(), range: quoteSheet_(r.getSheet().getName()) + '!' + absolute_(r.getA1Notation()) };
  });
  return { name: ss.getName(), sheets: sheets, namedRanges: names };
}

function absolute_(a1) {
  return a1.split(':').map(function (p) { return p.replace(/^([A-Z]+)(\d+)$/, '$$$1$$$2'); }).join(':');
}

// ------------------------------------------------------------------ backend bridge
/** Called by the sidebar: forwards an API call to the Python backend. */
function sgApi(name, payloadJson) {
  var cfg = PropertiesService.getDocumentProperties();
  var base = cfg.getProperty(PROP_BACKEND);
  if (!base) return JSON.stringify({ error: 'No backend configured. Click ⚙ and paste your SheetGuard server URL.' });
  var payload = JSON.parse(payloadJson || '{}');
  if (payload.useSheet) {
    delete payload.useSheet;
    payload.workbook = readWorkbook();
  }
  var headers = {};
  var key = cfg.getProperty(PROP_KEY);
  if (key) headers['X-SheetGuard-Key'] = key;
  try {
    var res = UrlFetchApp.fetch(base + '/api/' + name, {
      method: 'post',
      contentType: 'application/json',
      payload: JSON.stringify(payload),
      headers: headers,
      muteHttpExceptions: true,
      followRedirects: true
    });
    var code = res.getResponseCode();
    var body = res.getContentText();
    if (code >= 400 && body.charAt(0) !== '{') {
      return JSON.stringify({ error: 'Backend HTTP ' + code + ': ' + body.slice(0, 200) });
    }
    return body;
  } catch (err) {
    return JSON.stringify({ error: 'Cannot reach backend at ' + base + ' — ' + err.message });
  }
}

// ------------------------------------------------------------------ sheet actions
function splitKey_(key) {
  var i = key.lastIndexOf('!');
  return { sheet: key.slice(0, i), a1: key.slice(i + 1) };
}

function rangeOf_(ss, key) {
  var k = splitKey_(key);
  var sh = ss.getSheetByName(k.sheet);
  return sh ? sh.getRange(k.a1) : null;
}

function jumpTo(key) {
  var ss = SpreadsheetApp.getActive();
  var r = rangeOf_(ss, key);
  if (!r) return false;
  r.getSheet().activate();
  r.activate();
  return true;
}

function getActiveCellKey() {
  var r = SpreadsheetApp.getActive().getActiveRange();
  if (!r) return null;
  return r.getSheet().getName() + '!' + r.getCell(1, 1).getA1Notation();
}

function writeCell_(range, value) {
  var v = String(value);
  if (v.charAt(0) === '=') range.setFormula(v);
  else if (v !== '' && !isNaN(Number(v))) range.setValue(Number(v));
  else range.setValue(v);
}

function applyFix(key, after) {
  var ss = SpreadsheetApp.getActive();
  var r = rangeOf_(ss, key);
  if (!r) throw new Error('Cell not found: ' + key);
  writeCell_(r, after);
  r.setBackground(COLORS.fixed);
  r.setNote('SheetGuard fixed this cell.\nNow: ' + after);
  forgetOriginal_(key);
  return true;
}

function applyFixes(fixesJson) {
  var fixes = JSON.parse(fixesJson || '[]');
  fixes.forEach(function (f) { applyFix(f.cell, f.after); });
  return fixes.length;
}

/** Colour flagged cells and attach the explanation as a note (originals are remembered). */
function highlight(itemsJson) {
  clearHighlights();
  var ss = SpreadsheetApp.getActive();
  var items = JSON.parse(itemsJson || '[]');
  var order = { error: 0, warning: 1, info: 2 };
  var byCell = {};
  items.forEach(function (it) {
    var cur = byCell[it.cell];
    if (!cur) byCell[it.cell] = { sev: it.sev, notes: [it.note] };
    else {
      cur.notes.push(it.note);
      if (order[it.sev] < order[cur.sev]) cur.sev = it.sev;
    }
  });
  var originals = {};
  Object.keys(byCell).forEach(function (key) {
    var r = rangeOf_(ss, key);
    if (!r) return;
    originals[key] = { bg: r.getBackground(), note: String(r.getNote() || '').slice(0, 500) };
    r.setBackground(COLORS[byCell[key].sev] || COLORS.info);
    r.setNote(byCell[key].notes.join('\n\n———\n\n').slice(0, 3000));
  });
  saveOriginals_(originals);
  return Object.keys(byCell).length;
}

// Document properties hold at most 9 KB per value, so the originals are stored in chunks.
function saveOriginals_(obj) {
  var p = PropertiesService.getDocumentProperties();
  var old = +(p.getProperty(PROP_ORIG + '_N') || 0);
  for (var i = 0; i < old; i++) p.deleteProperty(PROP_ORIG + '_' + i);
  p.deleteProperty(PROP_ORIG);
  var s = JSON.stringify(obj), n = 0;
  for (var j = 0; j < s.length; j += 8000) p.setProperty(PROP_ORIG + '_' + (n++), s.slice(j, j + 8000));
  p.setProperty(PROP_ORIG + '_N', String(n));
}

function loadOriginals_() {
  var p = PropertiesService.getDocumentProperties();
  var n = +(p.getProperty(PROP_ORIG + '_N') || 0), s = '';
  for (var i = 0; i < n; i++) s += p.getProperty(PROP_ORIG + '_' + i) || '';
  try { return JSON.parse(s || p.getProperty(PROP_ORIG) || '{}'); } catch (e) { return {}; }
}

function clearHighlights() {
  var originals = loadOriginals_();
  var ss = SpreadsheetApp.getActive();
  Object.keys(originals).forEach(function (key) {
    var r = rangeOf_(ss, key);
    if (!r) return;
    r.setBackground(originals[key].bg === '#ffffff' ? null : originals[key].bg);
    r.setNote(originals[key].note || '');
  });
  saveOriginals_({});
  return true;
}

function forgetOriginal_(key) {
  var originals = loadOriginals_();
  if (!(key in originals)) return;
  delete originals[key];
  saveOriginals_(originals);
}

/** Deliverable: a corrected, annotated copy of the whole spreadsheet in the user's Drive. */
function createCorrectedCopy(fixesJson, notesJson) {
  var ss = SpreadsheetApp.getActive();
  var copy = ss.copy(ss.getName() + ' — SheetGuard corrected');
  var fixes = JSON.parse(fixesJson || '[]');
  var notes = JSON.parse(notesJson || '[]');
  var fixed = {};
  fixes.forEach(function (f) {
    var r = rangeOf_(copy, f.cell);
    if (!r) return;
    writeCell_(r, f.after);
    r.setBackground(COLORS.fixed);
    r.setNote('FIXED by SheetGuard (' + f.code + ' ' + f.title + ')\nwas: ' + f.before + '\nnow: ' + f.after);
    fixed[f.cell] = true;
  });
  notes.forEach(function (n) {
    if (fixed[n.cell]) return;
    var r = rangeOf_(copy, n.cell);
    if (!r) return;
    r.setBackground(COLORS[n.sev] || COLORS.info);
    r.setNote(String(n.note).slice(0, 3000));
  });
  // a report sheet at the front
  var title = 'SheetGuard Report', k = 2;
  while (copy.getSheetByName(title)) title = 'SheetGuard Report ' + (k++);
  var rep = copy.insertSheet(title, 0);
  var text = function (v) { v = String(v); return /^[=+\-@]/.test(v) ? "'" + v : v; };   // never a formula
  var rows = [['Cell', 'Severity', 'Note']].concat(notes.map(function (n) { return [text(n.cell), n.sev, text(n.note)]; }));
  rep.getRange(1, 1, rows.length, 3).setValues(rows);
  rep.getRange(1, 1, 1, 3).setFontWeight('bold');
  rep.setColumnWidth(1, 120); rep.setColumnWidth(2, 90); rep.setColumnWidth(3, 700);
  rep.getRange(2, 3, Math.max(rows.length - 1, 1), 1).setWrap(true);
  return copy.getUrl();
}
