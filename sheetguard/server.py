"""SheetGuard HTTP backend: standard library only, no installation needed.

    python -m sheetguard serve                 # http://localhost:8765  (opens the browser)
    python -m sheetguard serve --port 9000 --no-browser

Endpoints (all JSON):
    GET  /api/health                 liveness + capabilities
    GET  /api/demo                   the demo workbook
    POST /api/analyze   {workbook | demo | xlsx_base64}     full compiler report
    POST /api/parse     {formula}                            tokens / AST / type / value
    POST /api/fix       {workbook}                           corrected workbook
    POST /api/export    {workbook}                           annotated .xlsx (base64)
    POST /api/compile   {workbook, numpy?, target?}          generated code
    POST /api/bench     {rows}                               engine benchmark
Everything else is served from ./web (the standalone UI).

Privacy: set the environment variable SHEETGUARD_KEY to require the header
``X-SheetGuard-Key`` on every API call (the Apps Script add-on sends it).
"""
from __future__ import annotations

import json
import mimetypes
import os
import sys
import threading
import time
import traceback
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import __version__
from .api import ROUTES

WEB_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web")
MAX_BODY = 25 * 1024 * 1024


class Handler(BaseHTTPRequestHandler):
    server_version = f"SheetGuard/{__version__}"

    def log_message(self, fmt, *args):  # quieter, one line per request
        sys.stderr.write(f"  {self.command:<4} {self.path[:60]:<60} {args[1] if len(args) > 1 else ''}\n")

    # ------------------------------------------------------------ helpers
    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-SheetGuard-Key")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")

    def _json(self, code: int, obj) -> None:
        try:
            body = json.dumps(obj, ensure_ascii=False, allow_nan=False).encode("utf-8")
        except (TypeError, ValueError):            # NaN / inf / odd types -> still valid JSON
            from .api import jsonable
            body = json.dumps(jsonable(obj), ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def _authorised(self) -> bool:
        key = os.environ.get("SHEETGUARD_KEY")
        if not key:
            return True
        got = self.headers.get("X-SheetGuard-Key") or parse_qs(urlparse(self.path).query).get(
            "key", [""])[0]
        return got == key

    def _api(self, name: str, payload: dict) -> None:
        fn = ROUTES.get(name)
        if fn is None:
            return self._json(404, {"error": f"unknown endpoint /api/{name}"})
        if name != "health" and not self._authorised():
            return self._json(401, {"error": "missing or wrong X-SheetGuard-Key"})
        if not isinstance(payload, dict):
            return self._json(400, {"error": "the request body must be a JSON object"})
        t = time.perf_counter()
        try:
            out = fn(payload)
        except (ValueError, KeyError, TypeError) as e:
            msg = str(e) if isinstance(e, ValueError) else f"bad request: {type(e).__name__}: {e}"
            return self._json(400, {"error": msg})
        except RecursionError:
            return self._json(400, {"error": "a formula is nested too deeply to analyse"})
        except RuntimeError as e:                  # e.g. an optional dependency is missing
            return self._json(503, {"error": str(e)})
        except Exception as e:  # pragma: no cover - surfaced to the UI
            traceback.print_exc()
            return self._json(500, {"error": f"{type(e).__name__}: {e}"})
        if isinstance(out, dict):
            out.setdefault("server_ms", round((time.perf_counter() - t) * 1000, 1))
        self._json(200, out)

    # ------------------------------------------------------------ verbs
    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self):
        url = urlparse(self.path)
        if url.path.startswith("/api/"):
            q = {k: v[0] for k, v in parse_qs(url.query).items()}
            return self._api(url.path[5:].strip("/"), q)
        path = url.path.lstrip("/") or "index.html"
        full = os.path.normpath(os.path.join(WEB_DIR, path))
        inside = os.path.commonpath([full, WEB_DIR]) == WEB_DIR
        if not inside or not os.path.isfile(full):
            full = os.path.join(WEB_DIR, "index.html")
            if not os.path.isfile(full):
                return self._json(404, {"error": "web UI not found"})
        with open(full, "rb") as fh:
            body = fh.read()
        self.send_response(200)
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype.endswith("javascript"):
            ctype += "; charset=utf-8"
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        url = urlparse(self.path)
        if not url.path.startswith("/api/"):
            return self._json(404, {"error": "POST only to /api/*"})
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return self._json(400, {"error": "bad Content-Length"})
        if n < 0 or n > MAX_BODY:
            return self._json(413, {"error": f"request too large (limit {MAX_BODY // 2**20} MB)"})
        try:
            payload = json.loads(self.rfile.read(n) or b"{}")
        except (ValueError, UnicodeDecodeError):
            return self._json(400, {"error": "body must be JSON"})
        self._api(url.path[5:].strip("/"), payload)


class Server(ThreadingHTTPServer):
    daemon_threads = True                 # Ctrl+C never waits for a slow request
    allow_reuse_address = os.name != "nt"  # on Windows this would let two servers share a port


def _is_sheetguard(host: str, port: int) -> bool:
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://{'127.0.0.1' if host == '0.0.0.0' else host}:"
                                    f"{port}/api/health", timeout=1.5) as r:
            return json.loads(r.read()).get("service") == "SheetGuard"
    except Exception:
        return False


def serve(host: str = "127.0.0.1", port: int = 8765, open_browser: bool = True,
          fallback: bool = True) -> None:
    from .stack import enable_deep_threads
    enable_deep_threads()                 # request threads get a big stack (deep formulas)
    httpd = None
    for p in range(port, port + (10 if fallback else 1)):
        try:
            httpd = Server((host, p), Handler)
            break
        except OSError:
            shown = "localhost" if host in ("127.0.0.1", "0.0.0.0") else host
            if _is_sheetguard(host, p):
                print(f"\n  SheetGuard is already running at http://{shown}:{p}/ - "
                      f"{'opening it.' if open_browser else 'use that one (or stop it first).'}")
                if open_browser:
                    webbrowser.open(f"http://{shown}:{p}/")
                return
            print(f"  port {p} is busy, trying {p + 1}…")
    if httpd is None:
        raise RuntimeError(f"no free port between {port} and {port + 9}; use --port")
    port = httpd.server_address[1]
    url = f"http://{'localhost' if host in ('127.0.0.1', '0.0.0.0') else host}:{port}/"
    print(f"\n  SheetGuard {__version__} backend running at {url}")
    print(f"  API:  {url}api/health")
    if os.environ.get("SHEETGUARD_KEY"):
        print("  API key required (SHEETGUARD_KEY is set)")
    print("  Press Ctrl+C to stop.\n")
    if open_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n  stopped.")
    finally:
        httpd.server_close()


def main() -> None:
    """Entry point for cloud hosts (Render / Railway / Fly): binds 0.0.0.0:$PORT."""
    serve(host=os.environ.get("HOST", "0.0.0.0"), port=int(os.environ.get("PORT", 8765)),
          open_browser=False, fallback=False)


if __name__ == "__main__":
    main()
