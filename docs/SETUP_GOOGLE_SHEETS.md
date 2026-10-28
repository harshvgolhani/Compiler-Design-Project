# Running SheetGuard inside Google Sheets (about 10 minutes)

SheetGuard has two parts:

1. **The compiler backend.** This is the Python code in this folder. It runs on your laptop, or optionally on a free cloud host.
2. **The Sheets add-on.** This is a small Apps Script attached to *one* spreadsheet. It reads the formulas, sends them to the backend and draws the sidebar.

Nothing is published to the Google Workspace Marketplace. The script lives only inside your spreadsheet ("container-bound"), so only you can see or run it.

```
Google Sheet ─► Sidebar (Apps Script) ─► HTTPS ─► SheetGuard backend (Python compiler) ─► results in the sidebar + highlighted cells
```

---

## Step 1: Start the backend on your laptop

Double-click **`START_SheetGuard.bat`**. The local web app opens at <http://localhost:8765>. Leave that window open.

## Step 2: Give it a public URL (Google's servers must be able to reach it)

Google runs Apps Script in its own cloud, so `localhost` won't work. Pick **one** of these options:

**Option A: Cloudflare quick tunnel (free, no account).** Best for the review.

1. Install it once: open PowerShell and run `winget install --id Cloudflare.cloudflared`. If winget isn't available, download `cloudflared-windows-amd64.exe` from the Cloudflare GitHub releases page, rename it to `cloudflared.exe` and put it in this folder.
2. Double-click **`START_TUNNEL.bat`**.
3. Copy the `https://<random-words>.trycloudflare.com` URL it prints.

**Option B: Deploy to Render (free, permanent URL).**
Push this folder to a GitHub repo. Then, on render.com, choose **New + → Blueprint** and select the repo. `render.yaml` sets everything up, including a random `SHEETGUARD_KEY` access key, which you can see under Environment. The free plan sleeps when idle, so open the URL once a minute before your demo to wake it up.

Check it works: opening `<your-url>/api/health` in a browser should show `{"ok": true, ...}`.

## Step 3: Make the demo spreadsheet

1. Go to <https://sheets.new>, then **File → Import → Upload** and choose `demo/Acme_Q3_Sales_buggy.xlsx`. Pick **Replace spreadsheet**.
2. Check that **Data → Named ranges** lists TaxRate, Discount, CommissionRate and FX. The import keeps them.

You can also use any spreadsheet of your own.

## Step 4: Install the add-on in that spreadsheet

1. In the spreadsheet, go to **Extensions → Apps Script**.
2. Replace the contents of `Code.gs` with the file **`addon/Code.gs`**.
3. Click **＋ → HTML**, name it `Sidebar` (exactly), and paste **`addon/Sidebar.html`**.
4. Click **＋ → HTML**, name it `Graph`, and paste **`addon/Graph.html`**.
5. Optional: under **Project Settings**, tick *Show "appsscript.json"* and paste `addon/appsscript.json`.
6. Click **Save** (💾), then go back to the spreadsheet and **reload** the tab.

## Step 5: Use it

1. A new menu, **🛡 SheetGuard → Open SheetGuard**, appears. The first time, Google asks you to authorize the script. Because it is your own unpublished script, you'll see "Google hasn't verified this app": click **Advanced → Go to (unsafe)**. The permissions are: edit this spreadsheet, show a sidebar, and connect to an external service (your backend).
2. In the sidebar, click ⚙, paste the URL from Step 2 (and the key, if you used Render), then click **Save & test**.
3. Click **▶ Analyze this sheet**. Flagged cells are coloured, and hovering over one shows the explanation as a note.

| Sidebar tab | What it shows |
|---|---|
| **Structure** | Click any cell in the sheet to see its token stream, AST, type, precedents and dependents, live. Below that is the dependency DAG (⤢ opens it full-screen). |
| **Verification** | Every issue with its cause and fix. **Go to cell** jumps to the cell; **Apply fix** rewrites it. |
| **Deliverables** | Plain-English summary, the "what the bugs are costing you" table, **Apply all fixes**, **Create corrected copy** (a new annotated spreadsheet in your Drive) and **Download annotated .xlsx**. |

**Clear highlights** in the menu restores the original colours and notes.

---

### Troubleshooting

| Symptom | Fix |
|---|---|
| "Cannot reach backend" | Check that both .bat windows are still open, that `<url>/api/health` works in your browser, and that the URL has no trailing path. A new tunnel run gives a **new** URL, so paste it again. |
| "Backend HTTP 401" | You set `SHEETGUARD_KEY` on the server. Paste the same key in ⚙. |
| The SheetGuard menu doesn't appear | Reload the spreadsheet tab, or run `onOpen` once from the Apps Script editor. |
| Authorization loop | Use a personal Gmail account. Some college Workspace domains block unverified scripts. |
| Sidebar is blank | The HTML file names must be exactly `Sidebar` and `Graph`. |

**Backup plan for a live demo:** the web app at <http://localhost:8765> shows the same three views and more, and works without internet.
