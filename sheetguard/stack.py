"""Deep recursion support.

The parser, the type checker, the interpreter and the code generators are all
recursive tree walkers, so a formula nested 100 levels deep needs a few
thousand Python frames. The default thread stack (1-2 MB on Windows) is too
small for that, so every entry point (CLI command, HTTP request) runs its work
on a thread with a large stack and a matching recursion limit. Library calls
from the main thread keep Python's defaults and get a clean "nested too
deeply" error instead of a crash.
"""
from __future__ import annotations

import sys
import threading

STACK_BYTES = 256 * 1024 * 1024
RECURSION_LIMIT = 50_000


def enable_deep_threads() -> bool:
    """Make every thread created from now on use a large stack. Returns success."""
    for size in (STACK_BYTES, 64 * 1024 * 1024, 16 * 1024 * 1024):
        try:
            threading.stack_size(size)
            limit = RECURSION_LIMIT if size >= 64 * 1024 * 1024 else 8_000
            sys.setrecursionlimit(max(sys.getrecursionlimit(), limit))
            return True
        except (ValueError, RuntimeError, OverflowError, MemoryError):
            continue
    return False


def run_deep(fn, *args, **kwargs):
    """Run fn(*args, **kwargs) on a big-stack thread; return its result / re-raise its error."""
    if not enable_deep_threads():
        return fn(*args, **kwargs)
    box = {}

    def target():
        try:
            box["result"] = fn(*args, **kwargs)
        except BaseException as e:            # re-raised in the calling thread
            box["error"] = e

    t = threading.Thread(target=target, name="sheetguard-worker", daemon=True)
    t.start()
    while t.is_alive():
        t.join(0.2)                           # short joins keep Ctrl+C responsive
    if "error" in box:
        raise box["error"]
    return box.get("result")
